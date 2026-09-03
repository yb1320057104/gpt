from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from bson import Binary
from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient, ReplaceOne


ROOT = Path(__file__).resolve().parents[2]
MAILCOM_ROOT = ROOT / "mailcom-manager"


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotently copy MailCom SQLite records to MongoDB")
    parser.add_argument("--sqlite", type=Path, default=MAILCOM_ROOT / "data" / "mailcom.db")
    args = parser.parse_args()
    load_dotenv(ROOT / "app" / ".env")
    uri = os.getenv("MAILCOM_MONGO_URI") or os.getenv("AUTOREGISTER_MONGO_URI")
    database = os.getenv("MAILCOM_MONGO_DATABASE") or os.getenv("AUTOREGISTER_MONGO_DATABASE") or "autoregister"
    if not uri:
        raise SystemExit("MongoDB URI is not configured")

    connection = sqlite3.connect(args.sqlite)
    connection.row_factory = sqlite3.Row
    accounts = connection.execute("SELECT * FROM accounts").fetchall()
    aliases = connection.execute("SELECT * FROM aliases").fetchall()

    client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
    client.admin.command("ping")
    db = client[database]
    account_collection = db["mailcom_accounts"]
    alias_collection = db["mailcom_aliases"]
    account_collection.create_index([("emailNormalized", ASCENDING)], unique=True)
    account_collection.create_index([("accessKey", ASCENDING)], unique=True)
    account_collection.create_index([("createdAt", DESCENDING)])
    alias_collection.create_index([("emailNormalized", ASCENDING)], unique=True)
    alias_collection.create_index([("accessKey", ASCENDING)], unique=True)
    alias_collection.create_index([("accountId", ASCENDING), ("createdAt", DESCENDING)])

    account_ops = [
        ReplaceOne(
            {"_id": str(row["id"])},
            {
                "_id": str(row["id"]), "email": str(row["email"]),
                "emailNormalized": str(row["email_normalized"]),
                "passwordEncrypted": Binary(bytes(row["password_encrypted"])),
                "accessKey": str(row["access_key"]), "status": str(row["status"]),
                "messageCount": row["message_count"], "lastCheckedAt": row["last_checked_at"],
                "lastError": row["last_error"], "createdAt": str(row["created_at"]),
                "updatedAt": str(row["updated_at"]),
            },
            upsert=True,
        )
        for row in accounts
    ]
    alias_ops = [
        ReplaceOne(
            {"_id": str(row["id"])},
            {
                "_id": str(row["id"]), "accountId": str(row["account_id"]),
                "email": str(row["email"]), "emailNormalized": str(row["email_normalized"]),
                "label": str(row["label"] or ""), "accessKey": str(row["access_key"]),
                "createdAt": str(row["created_at"]), "updatedAt": str(row["updated_at"]),
            },
            upsert=True,
        )
        for row in aliases
    ]
    if account_ops:
        account_collection.bulk_write(account_ops, ordered=False)
    if alias_ops:
        alias_collection.bulk_write(alias_ops, ordered=False)
    remote_accounts = account_collection.count_documents({})
    remote_aliases = alias_collection.count_documents({})
    print(f"migrated accounts={len(accounts)} aliases={len(aliases)}; remote accounts={remote_accounts} aliases={remote_aliases}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
