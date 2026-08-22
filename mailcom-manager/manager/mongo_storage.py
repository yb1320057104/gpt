from __future__ import annotations

import re
import secrets
import threading
from typing import Any
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from .storage import CredentialCipher, utc_now_text


class MongoAccountStore:
    """MailCom account storage backed by the project's server MongoDB."""

    storage_kind = "mongodb-dpapi"

    def __init__(self, uri: str, database: str, cipher: CredentialCipher) -> None:
        self.cipher = cipher
        self._lock = threading.RLock()
        self.client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
        self.client.admin.command("ping")
        db = self.client[database]
        self.accounts = db["mailcom_accounts"]
        self.aliases = db["mailcom_aliases"]
        self.accounts.create_index([("emailNormalized", ASCENDING)], unique=True)
        self.accounts.create_index([("accessKey", ASCENDING)], unique=True)
        self.accounts.create_index([("createdAt", DESCENDING)])
        self.aliases.create_index([("emailNormalized", ASCENDING)], unique=True)
        self.aliases.create_index([("accessKey", ASCENDING)], unique=True)
        self.aliases.create_index([("accountId", ASCENDING), ("createdAt", DESCENDING)])

    def _new_access_key(self) -> str:
        while True:
            value = secrets.token_urlsafe(32)
            if not self.accounts.find_one({"accessKey": value}, {"_id": 1}) and not self.aliases.find_one(
                {"accessKey": value}, {"_id": 1}
            ):
                return value

    def _public(self, row: dict[str, Any], alias_count: int | None = None) -> dict[str, Any]:
        return {
            "id": str(row["_id"]),
            "email": str(row["email"]),
            "status": str(row.get("status") or "unknown"),
            "messageCount": row.get("messageCount"),
            "lastCheckedAt": row.get("lastCheckedAt"),
            "lastError": row.get("lastError"),
            "createdAt": str(row["createdAt"]),
            "updatedAt": str(row["updatedAt"]),
            "aliasCount": int(
                alias_count
                if alias_count is not None
                else self.aliases.count_documents({"accountId": str(row["_id"])})
            ),
        }

    @staticmethod
    def _public_alias(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row["_id"]),
            "accountId": str(row["accountId"]),
            "email": str(row["email"]),
            "label": str(row.get("label") or ""),
            "createdAt": str(row["createdAt"]),
            "updatedAt": str(row["updatedAt"]),
        }

    def import_account(self, email: str, password: str) -> bool:
        normalized = email.strip().casefold()
        now = utc_now_text()
        with self._lock:
            if self.aliases.find_one({"emailNormalized": normalized}, {"_id": 1}):
                return False
            try:
                self.accounts.insert_one(
                    {
                        "_id": str(uuid4()),
                        "email": email.strip(),
                        "emailNormalized": normalized,
                        "passwordEncrypted": self.cipher.encrypt(password),
                        "accessKey": self._new_access_key(),
                        "status": "unknown",
                        "messageCount": None,
                        "lastCheckedAt": None,
                        "lastError": None,
                        "createdAt": now,
                        "updatedAt": now,
                    }
                )
                return True
            except DuplicateKeyError:
                return False

    def list_accounts(self, *, query: str = "", page: int = 1, page_size: int = 50) -> tuple[list[dict[str, Any]], int]:
        selector: dict[str, Any] = {}
        if query.strip():
            selector["emailNormalized"] = {"$regex": re.escape(query.strip().casefold())}
        total = self.accounts.count_documents(selector)
        rows = list(
            self.accounts.find(selector)
            .sort("createdAt", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        ids = [str(row["_id"]) for row in rows]
        counts = {
            str(item["_id"]): int(item["count"])
            for item in self.aliases.aggregate(
                [{"$match": {"accountId": {"$in": ids}}}, {"$group": {"_id": "$accountId", "count": {"$sum": 1}}}]
            )
        }
        return [self._public(row, counts.get(str(row["_id"]), 0)) for row in rows], int(total)

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        row = self.accounts.find_one({"_id": account_id})
        return self._public(row) if row else None

    def underfilled_accounts(self, target_aliases: int) -> list[dict[str, Any]]:
        target = max(0, int(target_aliases))
        rows = [self._public(row) for row in self.accounts.find({})]
        return sorted(
            (row for row in rows if row["aliasCount"] < target),
            key=lambda row: (-row["aliasCount"], row["createdAt"]),
        )

    def _decrypt_account(self, row: dict[str, Any] | None) -> tuple[str, str] | None:
        if row is None:
            return None
        return str(row["email"]), self.cipher.decrypt(bytes(row["passwordEncrypted"]))

    def get_credentials(self, account_id: str) -> tuple[str, str] | None:
        return self._decrypt_account(self.accounts.find_one({"_id": account_id}))

    def get_credentials_by_email(self, email: str) -> tuple[str, str] | None:
        return self._decrypt_account(self.accounts.find_one({"emailNormalized": email.strip().casefold()}))

    def get_mailbox_credentials(self, email: str) -> tuple[str, str, str, bool] | None:
        normalized = email.strip().casefold()
        account = self.accounts.find_one({"emailNormalized": normalized})
        if account:
            return str(account["email"]), str(account["email"]), self.cipher.decrypt(bytes(account["passwordEncrypted"])), False
        alias = self.aliases.find_one({"emailNormalized": normalized})
        if not alias:
            return None
        account = self.accounts.find_one({"_id": str(alias["accountId"])})
        if not account:
            return None
        return str(alias["email"]), str(account["email"]), self.cipher.decrypt(bytes(account["passwordEncrypted"])), True

    def email_for_access_key(self, access_key: str) -> str | None:
        value = str(access_key or "").strip()
        if not value:
            return None
        row = self.accounts.find_one({"accessKey": value}, {"email": 1}) or self.aliases.find_one({"accessKey": value}, {"email": 1})
        return str(row["email"]) if row else None

    def all_emails(self) -> list[str]:
        return [str(row["email"]) for row in self.accounts.find({}, {"email": 1}).sort("createdAt", DESCENDING)]

    def all_mailboxes(self) -> list[str]:
        rows = list(self.accounts.find({}, {"email": 1, "createdAt": 1})) + list(self.aliases.find({}, {"email": 1, "createdAt": 1}))
        rows.sort(key=lambda row: str(row.get("createdAt") or ""), reverse=True)
        return [str(row["email"]) for row in rows]

    def registration_items(self) -> list[dict[str, Any]]:
        accounts = {str(row["_id"]): row for row in self.accounts.find({})}
        items = [
            {"email": str(row["email"]), "accountEmail": str(row["email"]), "isAlias": False, "accessKey": str(row["accessKey"]), "createdAt": str(row["createdAt"])}
            for row in accounts.values()
        ]
        items.extend(
            {"email": str(row["email"]), "accountEmail": str(accounts[str(row["accountId"])]["email"]), "isAlias": True, "accessKey": str(row["accessKey"]), "createdAt": str(row["createdAt"])}
            for row in self.aliases.find({}) if str(row["accountId"]) in accounts
        )
        items.sort(key=lambda row: row["createdAt"], reverse=True)
        for item in items:
            item.pop("createdAt", None)
        return items

    def sync_snapshot(self) -> dict[str, Any]:
        accounts = list(self.accounts.find({}).sort("createdAt", ASCENDING))
        account_emails = {str(row["_id"]): str(row["email"]) for row in accounts}
        return {
            "version": 1,
            "accounts": [{"email": str(row["email"]), "password": self.cipher.decrypt(bytes(row["passwordEncrypted"]))} for row in accounts],
            "aliases": [
                {"email": str(row["email"]), "accountEmail": account_emails[str(row["accountId"])], "label": str(row.get("label") or "")}
                for row in self.aliases.find({}).sort("createdAt", ASCENDING) if str(row["accountId"]) in account_emails
            ],
        }

    def import_alias(self, account_id: str, email: str, label: str = "") -> str:
        normalized = email.strip().casefold()
        now = utc_now_text()
        with self._lock:
            if not self.accounts.find_one({"_id": account_id}, {"_id": 1}):
                return "missing_account"
            if self.accounts.find_one({"emailNormalized": normalized}, {"_id": 1}):
                return "duplicate"
            try:
                self.aliases.insert_one({"_id": str(uuid4()), "accountId": account_id, "email": email.strip(), "emailNormalized": normalized, "label": label.strip()[:200], "accessKey": self._new_access_key(), "createdAt": now, "updatedAt": now})
                return "inserted"
            except DuplicateKeyError:
                return "duplicate"

    def list_aliases(self, account_id: str) -> list[dict[str, Any]]:
        return [self._public_alias(row) for row in self.aliases.find({"accountId": account_id}).sort("createdAt", DESCENDING)]

    def delete_alias(self, alias_id: str) -> bool:
        return self.aliases.delete_one({"_id": alias_id}).deleted_count == 1

    def update_check(self, account_id: str, *, status: str, message_count: int | None, error: str | None) -> None:
        now = utc_now_text()
        self.accounts.update_one({"_id": account_id}, {"$set": {"status": status, "messageCount": message_count, "lastCheckedAt": now, "lastError": error, "updatedAt": now}})

    def delete_account(self, account_id: str) -> bool:
        with self._lock:
            deleted = self.accounts.delete_one({"_id": account_id}).deleted_count == 1
            if deleted:
                self.aliases.delete_many({"accountId": account_id})
            return deleted

    def stats(self) -> dict[str, int]:
        grouped = {str(row["_id"]): int(row["count"]) for row in self.accounts.aggregate([{"$group": {"_id": "$status", "count": {"$sum": 1}}}])}
        return {"total": self.accounts.count_documents({}), "online": grouped.get("online", 0), "failed": grouped.get("failed", 0), "unknown": grouped.get("unknown", 0), "aliases": self.aliases.count_documents({})}
