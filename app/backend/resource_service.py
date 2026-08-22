from __future__ import annotations

import asyncio
import os
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse, urlsplit
from uuid import uuid4

import yaml
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .errors import (
    DuplicateResourceError,
    InsufficientEmailsError,
    MongoUnavailableError,
    ResourceNotFoundError,
)
from .mongo_manager import MongoManager
from .mailbox_client import MAILCOM_WEBMAIL_URL, direct_mailbox_access_url
from .resource_models import (
    AccountCreate,
    AccountExportInput,
    AccountRecord,
    AccountStats,
    DeleteResult,
    EmailExportInput,
    EmailRecord,
    EmailStats,
    FreeStats,
    ImportResult,
    OverviewStats,
    Page,
    PageSize,
    PlusStats,
    ProxyRecord,
    ProxyCountrySummary,
    ProxyGroupSummary,
    ProxyStats,
    TextExport,
)
from .chatgpt_plan import AccountPlanResult, PlanCheckError
from .checkout_type import CheckoutTypeCheckError, CheckoutTypeResult
from .totp import normalize_totp_secret


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PROXY_COUNTRY_PATTERN = re.compile(
    r"(?:^|[-_.])(?:region|country|res|area|dc|res_sc)-([A-Za-z]{2})(?:[-_.:]|$)",
    re.IGNORECASE,
)
SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
DEFAULT_PROXY_GROUP = "默认组"
MAX_PROXY_COOLDOWN_SECONDS = 10 * 60
REGISTRATION_EXCLUDED_EMAIL_DOMAINS = tuple(
    domain.strip().casefold().lstrip("@")
    for domain in os.getenv(
        "AUTOREGISTER_EXCLUDED_EMAIL_DOMAINS", ""
    ).split(",")
    if domain.strip().lstrip("@")
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def proxy_quarantine_seconds() -> int:
    try:
        configured = int(os.getenv("AUTOREGISTER_PROXY_COOLDOWN_SECONDS", "600"))
    except (TypeError, ValueError):
        configured = MAX_PROXY_COOLDOWN_SECONDS
    return max(60, min(MAX_PROXY_COOLDOWN_SECONDS, configured))


def usable_proxy_status_filter(now: datetime) -> dict[str, Any]:
    """Allow normal proxies and automatically released 10-minute quarantines."""
    return {
        "$or": [
            {"status": {"$ne": "quarantined"}},
            {"quarantineUntil": {"$exists": False}},
            {"quarantineUntil": None},
            {"quarantineUntil": {"$lte": now}},
        ]
    }


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_proxy_group(value: Any) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:64] or DEFAULT_PROXY_GROUP


def proxy_group_filter(value: str | None) -> dict[str, Any]:
    group = normalize_proxy_group(value)
    if group == DEFAULT_PROXY_GROUP:
        return {
            "$or": [
                {"group": DEFAULT_PROXY_GROUP},
                {"group": {"$exists": False}},
                {"group": None},
                {"group": ""},
            ]
        }
    return {"group": group}


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def email_source_filter(source: str | None) -> dict[str, Any]:
    normalized = str(source or "all").strip().casefold()
    if normalized == "mailcom_alias":
        return {"sourceType": "mailcom_alias"}
    if normalized == "standard":
        return {
            "$or": [
                {"sourceType": {"$exists": False}},
                {"sourceType": {"$ne": "mailcom_alias"}},
            ]
        }
    return {}


def registration_email_filter(source: str | None = "all") -> dict[str, Any]:
    query: dict[str, Any] = {"status": "available"}
    query.update(email_source_filter(source))
    if REGISTRATION_EXCLUDED_EMAIL_DOMAINS:
        query["$nor"] = [
            {
                "emailNormalized": {
                    "$regex": rf"@{re.escape(domain)}$",
                    "$options": "i",
                }
            }
            for domain in REGISTRATION_EXCLUDED_EMAIL_DOMAINS
        ]
    return query


def interleave_email_parent_groups(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep queue priority while taking at most one alias per parent each round."""
    grouped: dict[str, deque[dict[str, Any]]] = {}
    for document in documents:
        group_key = str(
            document.get("parentEmail")
            or document.get("emailNormalized")
            or document.get("_id")
            or ""
        ).strip().casefold()
        grouped.setdefault(group_key, deque()).append(document)

    result: list[dict[str, Any]] = []
    queues = list(grouped.values())
    while queues:
        next_round: list[deque[dict[str, Any]]] = []
        for queue in queues:
            result.append(queue.popleft())
            if queue:
                next_round.append(queue)
        queues = next_round
    return result


def export_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def normalize_country_code(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    return normalized if re.fullmatch(r"[A-Z]{2}", normalized) else "ZZ"


def infer_proxy_country(username: str, host: str = "") -> str:
    for candidate in (username, host):
        match = PROXY_COUNTRY_PATTERN.search(str(candidate or ""))
        if match is not None:
            return match.group(1).upper()
    return "ZZ"


def proxy_country_filter(country: str) -> dict[str, Any]:
    normalized = normalize_country_code(country)
    escaped = re.escape(normalized)
    inferred_pattern = {
        "$regex": rf"(?:^|[-_.])(?:region|country|res|area|dc|res_sc)-{escaped}(?:[-_.:]|$)",
        "$options": "i",
    }
    return {
        "$or": [
            {"country": normalized},
            {
                "$and": [
                    {
                        "$or": [
                            {"country": {"$exists": False}},
                            {"country": None},
                            {"country": "ZZ"},
                        ]
                    },
                    {
                        "$or": [
                            {"username": inferred_pattern},
                            {"host": inferred_pattern},
                        ]
                    },
                ]
            },
        ]
    }


class MongoResourceStore:
    def __init__(self, manager: MongoManager) -> None:
        self.manager = manager

    @property
    def accounts(self) -> Any:
        return self.manager.database["accounts"]

    @property
    def emails(self) -> Any:
        return self.manager.database["emails"]

    @property
    def proxies(self) -> Any:
        return self.manager.database["proxies"]

    @property
    def rebind_logs(self) -> Any:
        return self.manager.database["account_rebind_logs"]

    @property
    def rebind_tasks(self) -> Any:
        return self.manager.database["account_rebind_tasks"]

    async def _guard(self, awaitable: Any) -> Any:
        self.manager.require_online()
        try:
            return await awaitable
        except DuplicateKeyError:
            raise
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 当前不可用，请检查本机服务") from exc

    async def ensure_indexes(self) -> None:
        self.manager.require_online()
        try:
            await self.accounts.create_index(
                [("emailNormalized", ASCENDING)], unique=True, name="accounts_email_unique"
            )
            await self.accounts.create_index(
                [("createdAt", DESCENDING)], name="accounts_created_desc"
            )
            await self.accounts.create_index(
                [("accountType", ASCENDING)], name="accounts_type"
            )
            await self.accounts.create_index(
                [("registrationCountry", ASCENDING), ("createdAt", DESCENDING)],
                name="accounts_registration_country_created",
            )
            await self.accounts.create_index(
                [("sourceEmailId", ASCENDING)],
                unique=True,
                sparse=True,
                name="accounts_source_email_unique",
            )
            await self.emails.create_index(
                [("emailNormalized", ASCENDING)], unique=True, name="emails_email_unique"
            )
            await self.emails.create_index(
                [("status", ASCENDING), ("importedAt", DESCENDING)],
                name="emails_status_imported",
            )
            await self.emails.create_index(
                [
                    ("status", ASCENDING),
                    ("lastAttemptAt", ASCENDING),
                    ("importedAt", DESCENDING),
                ],
                name="emails_status_attempt_imported",
            )
            await self.proxies.create_index(
                [
                    ("host", ASCENDING),
                    ("port", ASCENDING),
                    ("username", ASCENDING),
                    ("password", ASCENDING),
                ],
                unique=True,
                name="proxies_identity_unique",
            )
            await self.proxies.create_index(
                [("country", ASCENDING), ("enabled", ASCENDING)],
                name="proxies_country_enabled",
            )
            await self.rebind_logs.create_index(
                [("createdAt", DESCENDING)],
                name="account_rebind_logs_created_desc",
            )
            await self.rebind_logs.create_index(
                [("createdAt", ASCENDING)],
                expireAfterSeconds=30 * 24 * 60 * 60,
                name="account_rebind_logs_ttl_30d",
            )
            await self.rebind_tasks.create_index(
                [("taskId", ASCENDING)],
                unique=True,
                name="account_rebind_tasks_id_unique",
            )
            await self.rebind_tasks.create_index(
                [("updatedAt", DESCENDING)],
                name="account_rebind_tasks_updated_desc",
            )
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 索引初始化失败") from exc

    async def release_orphaned_reservations(self, active_run_ids: list[str]) -> int:
        # Registration-run cleanup must never release durable rebind leases.
        query: dict[str, Any] = {
            "status": "reserved",
            "usagePurpose": {"$ne": "rebind"},
        }
        if active_run_ids:
            query["reservedBy"] = {"$nin": active_run_ids}
        result = await self._guard(
            self.emails.update_many(
                query,
                {
                    "$set": {"status": "available"},
                    "$unset": {"reservedBy": "", "reservedAt": ""},
                },
            )
        )
        return int(result.modified_count)

    async def reconcile_run_reservations(self, run_id: str) -> tuple[int, int]:
        """Finalize already-created accounts, then release every other reservation."""
        cursor = self.emails.find({"status": "reserved", "reservedBy": run_id})
        documents = await self._guard(cursor.to_list(length=None))
        consumed = released = 0
        for email in documents:
            account = await self._guard(
                self.accounts.find_one(
                    {
                        "$or": [
                            {"sourceEmailId": email["_id"]},
                            {"emailNormalized": email["emailNormalized"]},
                        ]
                    }
                )
            )
            if account is not None:
                result = await self._guard(
                    self.emails.delete_one(
                        {"_id": email["_id"], "reservedBy": run_id}
                    )
                )
                consumed += int(result.deleted_count)
            else:
                result = await self._guard(
                    self.emails.update_one(
                        {"_id": email["_id"], "reservedBy": run_id},
                        {
                            "$set": {"status": "available"},
                            "$unset": {"reservedBy": "", "reservedAt": ""},
                        },
                    )
                )
                released += int(result.modified_count)
        return consumed, released

    async def list_accounts(
        self,
        page: int,
        page_size: PageSize,
        query: str,
        promotion: str = "",
        country: str = "",
        alive: str = "",
        global_promotion: str = "",
        rebind: str = "",
        rebind_country: str = "",
    ) -> Page[AccountRecord]:
        mongo_query: dict[str, Any] = {}
        if query.strip():
            mongo_query["emailNormalized"] = {
                "$regex": re.escape(query.strip().lower()),
            }
        if country.strip():
            normalized_country = normalize_country_code(country)
            if normalized_country == "ZZ":
                mongo_query.setdefault("$and", []).append(
                    {"$or": [
                        {"registrationCountry": None},
                        {"registrationCountry": {"$exists": False}},
                    ]}
                )
            else:
                mongo_query["registrationCountry"] = normalized_country
        if promotion == "untried_plus":
            mongo_query.update({"accountType": "free", "promotionEligible": True})
        elif promotion == "ineligible":
            mongo_query["promotionEligible"] = False
        elif promotion == "unchecked":
            mongo_query.setdefault("$and", []).append(
                {"$or": [
                    {"promotionEligible": None},
                    {"promotionEligible": {"$exists": False}},
                ]}
            )
        if alive in {"alive", "dead", "unknown"}:
            mongo_query["aliveStatus"] = alive
        elif alive == "unchecked":
            mongo_query.setdefault("$and", []).append(
                {"$or": [
                    {"aliveStatus": None},
                    {"aliveStatus": {"$exists": False}},
                ]}
            )
        if global_promotion in {"eligible", "ineligible", "pending", "failed"}:
            mongo_query["globalPromotionStatus"] = global_promotion
        if rebind == "rebound":
            mongo_query["rebindStatus"] = {
                "$in": ["success", "email_changed_token_pending"]
            }
        elif rebind == "ready":
            mongo_query.setdefault("$and", []).append({
                "$or": [
                    {
                        "rebindStatus": {
                            "$nin": ["success", "email_changed_token_pending"]
                        }
                    },
                    {"rebindStatus": {"$exists": False}},
                ]
            })
        if rebind_country.strip():
            normalized_rebind_country = normalize_country_code(rebind_country)
            if normalized_rebind_country == "ZZ":
                mongo_query.setdefault("$and", []).append({
                    "$or": [
                        {"rebindProxyCountry": None},
                        {"rebindProxyCountry": ""},
                        {"rebindProxyCountry": {"$exists": False}},
                    ]
                })
            else:
                mongo_query["rebindProxyCountry"] = normalized_rebind_country
        total = await self._guard(self.accounts.count_documents(mongo_query))
        cursor = (
            self.accounts.find(mongo_query, {"accessToken": 0})
            .sort("createdAt", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        documents = await self._guard(cursor.to_list(length=page_size))
        return Page[AccountRecord](
            items=[self._account_record(item) for item in documents],
            total=total,
            page=page,
            pageSize=page_size,
        )

    async def create_account(self, incoming: AccountCreate) -> AccountRecord:
        now = utc_now()
        document = {
            "_id": str(uuid4()),
            "email": incoming.email,
            "emailNormalized": normalize_email(incoming.email),
            "chatgptPassword": incoming.chatgptPassword,
            "totpSecret": incoming.totpSecret,
            "emailAccessUrl": incoming.emailAccessUrl,
            "createdAt": now,
            "accountType": incoming.accountType,
            "phoneBound": incoming.phoneBound,
            "promotionEligible": incoming.promotionEligible,
            "registrationCountry": (
                normalize_country_code(incoming.registrationCountry)
                if incoming.registrationCountry
                else None
            ),
            "accessTokenConfigured": False,
            "accessTokenExpiresAt": None,
            "accessTokenUpdatedAt": None,
            "globalPromotionStatus": "pending",
            "globalPromotionEligible": None,
            "globalPromotionMessage": "等待 Access Token 和至少 2 个可用代理",
        }
        if incoming.sourceEmailId:
            document["sourceEmailId"] = incoming.sourceEmailId
        try:
            await self._guard(self.accounts.insert_one(document))
        except DuplicateKeyError as exc:
            raise DuplicateResourceError(f"账号已存在：{incoming.email}") from exc
        if incoming.sourceEmailId:
            await self._guard(
                self.emails.delete_one(
                    {
                        "_id": incoming.sourceEmailId,
                        "emailNormalized": document["emailNormalized"],
                    }
                )
            )
        return self._account_record(document)

    async def delete_accounts(self, ids: list[str]) -> DeleteResult:
        result = await self._guard(self.accounts.delete_many({"_id": {"$in": ids}}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def accounts_for_export(self, ids: list[str] | None) -> list[AccountRecord]:
        query = {} if ids is None else {"_id": {"$in": ids}}
        cursor = self.accounts.find(query, {"accessToken": 0}).sort(
            "createdAt", DESCENDING
        )
        documents = await self._guard(cursor.to_list(length=None))
        return [self._account_record(item) for item in documents]

    async def access_tokens_for_export(
        self,
        ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        query = {} if ids is None else {"_id": {"$in": ids}}
        cursor = self.accounts.find(
            query,
            {
                "accessToken": 1,
                "accessTokenConfigured": 1,
                "accessTokenExpiresAt": 1,
                "createdAt": 1,
            },
        ).sort("createdAt", DESCENDING)
        return await self._guard(cursor.to_list(length=None))

    async def payment_extractor_accounts(self) -> list[dict[str, Any]]:
        """Return selectable account metadata without exposing Access Tokens."""
        now = utc_now()
        cursor = self.accounts.find(
            {
                "accessTokenConfigured": True,
                "accessToken": {"$type": "string", "$ne": ""},
                "accessTokenExpiresAt": {"$gt": now},
            },
            {
                "email": 1,
                "registrationCountry": 1,
                "accessTokenExpiresAt": 1,
                "accountType": 1,
            },
        ).sort("createdAt", DESCENDING)
        documents = await self._guard(cursor.to_list(length=None))
        return [
            {
                "id": str(item["_id"]),
                "email": str(item.get("email") or ""),
                "registrationCountry": item.get("registrationCountry"),
                "accessTokenExpiresAt": item.get("accessTokenExpiresAt"),
                "accountType": item.get("accountType"),
            }
            for item in documents
        ]

    async def payment_extractor_access_token(self, account_id: str) -> str:
        document = await self._guard(
            self.accounts.find_one(
                {"_id": account_id},
                {"accessToken": 1, "accessTokenConfigured": 1, "accessTokenExpiresAt": 1},
            )
        )
        if not document or not document.get("accessTokenConfigured"):
            raise ResourceNotFoundError("账号没有可用 Access Token")
        expires_at = document.get("accessTokenExpiresAt")
        if not isinstance(expires_at, datetime) or expires_at <= utc_now():
            raise ResourceNotFoundError("账号 Access Token 已过期")
        token = str(document.get("accessToken") or "").strip()
        if not token:
            raise ResourceNotFoundError("账号没有可用 Access Token")
        return token

    async def store_account_access_token(
        self,
        account_id: str,
        access_token: str,
        expires_at: datetime,
    ) -> datetime:
        if expires_at.tzinfo is None:
            raise ValueError("AccessToken 过期时间必须包含时区")
        updated_at = utc_now()
        result = await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "accessToken": access_token,
                        "accessTokenConfigured": True,
                        "accessTokenExpiresAt": expires_at.astimezone(timezone.utc),
                        "accessTokenUpdatedAt": updated_at,
                    }
                },
            )
        )
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("AccessToken 对应账号不存在")
        return updated_at

    async def store_account_totp(
        self,
        account_id: str,
        secret: str,
        access_token: str,
        expires_at: datetime,
        activated_at: datetime,
    ) -> datetime:
        normalized_secret = normalize_totp_secret(secret)
        if expires_at.tzinfo is None or activated_at.tzinfo is None:
            raise ValueError("2FA 时间字段必须包含时区")
        updated_at = utc_now()
        result = await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "totpSecret": normalized_secret,
                        "totpActivatedAt": activated_at.astimezone(timezone.utc),
                        "totpStatus": "enabled",
                        "totpError": None,
                        "accessToken": access_token,
                        "accessTokenConfigured": True,
                        "accessTokenExpiresAt": expires_at.astimezone(timezone.utc),
                        "accessTokenUpdatedAt": updated_at,
                    }
                },
            )
        )
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("2FA 对应账号不存在")
        return updated_at

    async def store_account_password(
        self,
        account_id: str,
        password: str,
        configured_at: datetime,
    ) -> datetime:
        if not password or configured_at.tzinfo is None:
            raise ValueError("账号密码或配置时间无效")
        updated_at = utc_now()
        result = await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "chatgptPassword": password,
                        "passwordStatus": "enabled",
                        "passwordConfiguredAt": configured_at.astimezone(timezone.utc),
                        "passwordError": None,
                        "updatedAt": updated_at,
                    }
                },
            )
        )
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("密码对应账号不存在")
        return updated_at

    async def claim_pending_global_promotion_check(self) -> dict[str, Any] | None:
        now = utc_now()
        return await self._guard(
            self.accounts.find_one_and_update(
                {
                    "globalPromotionStatus": "pending",
                    "accessTokenConfigured": True,
                    "accessToken": {"$type": "string", "$ne": ""},
                    "accessTokenExpiresAt": {"$gt": now},
                },
                {"$set": {
                    "globalPromotionStatus": "running",
                    "globalPromotionStartedAt": now,
                    "globalPromotionMessage": "正在通过多个国家的代理检测试用资格",
                }},
                projection={"accessToken": 1},
                sort=[("createdAt", ASCENDING)],
                return_document=ReturnDocument.AFTER,
            )
        )

    async def queue_global_promotion_checks(self, ids: list[str]) -> dict[str, int]:
        unique_ids = list(dict.fromkeys(str(value) for value in ids if str(value)))
        if not unique_ids:
            return {"requested": 0, "queued": 0, "skipped": 0}
        result = await self._guard(self.accounts.update_many(
            {
                "_id": {"$in": unique_ids},
                "accessTokenConfigured": True,
                "accessToken": {"$type": "string", "$ne": ""},
            },
            {"$set": {
                "globalPromotionStatus": "pending",
                "globalPromotionEligible": None,
                "globalPromotionMessage": "已手动加入全局试用资格检测队列",
            }},
        ))
        queued = int(result.modified_count)
        return {"requested": len(unique_ids), "queued": queued, "skipped": len(unique_ids) - queued}

    async def store_global_promotion_pending(self, account_id: str, message: str) -> None:
        await self._guard(self.accounts.update_one({"_id": account_id}, {"$set": {
            "globalPromotionStatus": "pending",
            "globalPromotionEligible": None,
            "globalPromotionMessage": message,
        }}))

    async def store_global_promotion_result(
        self,
        account_id: str,
        *,
        eligible: bool,
        status: str,
        results: list[dict[str, Any]],
    ) -> None:
        countries = list(dict.fromkeys(str(item.get("country") or "ZZ") for item in results))
        await self._guard(self.accounts.update_one({"_id": account_id}, {"$set": {
            "globalPromotionStatus": status,
            "globalPromotionEligible": eligible,
            "globalPromotionCheckedAt": utc_now(),
            "globalPromotionProxyCount": len(results),
            "globalPromotionCountries": countries,
            "globalPromotionResults": results,
            "globalPromotionMessage": (
                "所有代理出口均检测到试用资格" if eligible
                else "并非所有代理出口都检测到试用资格" if status == "ineligible"
                else "部分代理检测异常，请稍后重新检测"
            ),
        }}))

    async def claim_account_plan_check(self, account_id: str) -> dict[str, Any] | None:
        now = utc_now()
        return await self._guard(
            self.accounts.find_one_and_update(
                {
                    "_id": account_id,
                    "accessTokenConfigured": True,
                    "accessToken": {"$type": "string", "$ne": ""},
                    "$or": [
                        {"planCheckStatus": {"$ne": "running"}},
                        {
                            "planCheckStartedAt": {
                                "$lte": now - timedelta(minutes=5)
                            }
                        },
                    ],
                },
                {
                    "$set": {
                        "planCheckStatus": "running",
                        "planCheckStartedAt": now,
                        "planCheckErrorCode": None,
                        "planCheckHttpStatus": None,
                    }
                },
                projection={
                    "accessToken": 1,
                    "accessTokenExpiresAt": 1,
                    "registrationCountry": 1,
                    "rebindProxyCountry": 1,
                },
                return_document=ReturnDocument.AFTER,
            )
        )

    async def claim_account_alive_check(self, account_id: str) -> dict[str, Any] | None:
        now = utc_now()
        return await self._guard(
            self.accounts.find_one_and_update(
                {
                    "_id": account_id,
                    "accessToken": {"$type": "string", "$ne": ""},
                    "$or": [
                        {"aliveStatus": {"$ne": "running"}},
                        {"aliveCheckStartedAt": {"$lte": now - timedelta(minutes=5)}},
                    ],
                },
                {
                    "$set": {
                        "aliveStatus": "running",
                        "aliveCheckStartedAt": now,
                        "aliveErrorCode": None,
                        "aliveHttpStatus": None,
                    }
                },
                projection={
                    "accessToken": 1,
                    "registrationCountry": 1,
                    "createdAt": 1,
                },
                return_document=ReturnDocument.AFTER,
            )
        )

    async def account_ids_due_for_alive_15m_check(
        self,
        *,
        limit: int = 100,
    ) -> list[str]:
        cutoff = utc_now() - timedelta(minutes=15)
        cursor = (
            self.accounts.find(
                {
                    "createdAt": {"$lte": cutoff},
                    "accessToken": {"$type": "string", "$ne": ""},
                    "aliveStatus": {"$ne": "dead"},
                    "$or": [
                        {"alive15mVerifiedAt": None},
                        {"alive15mVerifiedAt": {"$exists": False}},
                    ],
                },
                {"_id": 1},
            )
            .sort("createdAt", ASCENDING)
            .limit(max(1, min(1000, limit)))
        )
        documents = await self._guard(cursor.to_list(length=limit))
        return [str(document["_id"]) for document in documents]

    async def claim_account_checkout_type_check(self, account_id: str) -> dict[str, Any] | None:
        now = utc_now()
        return await self._guard(
            self.accounts.find_one_and_update(
                {
                    "_id": account_id,
                    "accessTokenConfigured": True,
                    "accessToken": {"$type": "string", "$ne": ""},
                    "$or": [
                        {"checkoutTypeCheckStatus": {"$ne": "running"}},
                        {"checkoutTypeCheckStartedAt": {"$lte": now - timedelta(minutes=5)}},
                    ],
                },
                {"$set": {
                    "checkoutTypeCheckStatus": "running",
                    "checkoutTypeCheckStartedAt": now,
                    "checkoutTypeErrorCode": None,
                    "checkoutTypeHttpStatus": None,
                }},
                projection={"accessToken": 1, "registrationCountry": 1},
                return_document=ReturnDocument.AFTER,
            )
        )

    async def claim_account_oaics_scan(self, account_id: str) -> dict[str, Any] | None:
        now = utc_now()
        return await self._guard(self.accounts.find_one_and_update(
            {
                "_id": account_id,
                "accessTokenConfigured": True,
                "accessToken": {"$type": "string", "$ne": ""},
                "$or": [
                    {"oaicsScanStatus": {"$ne": "running"}},
                    {"oaicsScanStartedAt": {"$lte": now - timedelta(minutes=30)}},
                ],
            },
            {"$set": {
                "oaicsScanStatus": "running", "oaicsScanStartedAt": now,
                "oaicsScanMessage": "正在通过全部可用代理检测 OAICS",
                "oaicsScanTotal": 0, "oaicsScanSuccess": 0,
                "oaicsScanCountryStats": [], "oaicsScanResults": [],
            }},
            projection={"accessToken": 1}, return_document=ReturnDocument.AFTER,
        ))

    async def store_account_oaics_scan_result(
        self, account_id: str, *, results: list[dict[str, Any]], country_stats: list[dict[str, Any]]
    ) -> None:
        successes = sum(item.get("checkoutType") == "oaics" for item in results)
        await self._guard(self.accounts.update_one({"_id": account_id}, {"$set": {
            "oaicsScanStatus": "completed", "oaicsScanCheckedAt": utc_now(),
            "oaicsScanTotal": len(results), "oaicsScanSuccess": successes,
            "oaicsScanCountryStats": country_stats, "oaicsScanResults": results,
            "oaicsScanMessage": f"全部代理检测完成，OAICS {successes}/{len(results)}",
        }}))

    async def store_account_oaics_scan_failure(self, account_id: str, message: str) -> None:
        await self._guard(self.accounts.update_one({"_id": account_id}, {"$set": {
            "oaicsScanStatus": "failed", "oaicsScanCheckedAt": utc_now(),
            "oaicsScanMessage": message,
        }}))

    async def store_account_alive_result(
        self,
        account_id: str,
        *,
        alive: bool,
        error_code: str | None = None,
        http_status: int | None = None,
        verified_15m: bool = False,
    ) -> None:
        now = utc_now()
        updates: dict[str, Any] = {
            "aliveStatus": "alive" if alive else "dead",
            "aliveCheckedAt": now,
            "aliveErrorCode": error_code,
            "aliveHttpStatus": http_status,
        }
        if alive and verified_15m:
            updates["alive15mVerifiedAt"] = now
        await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {"$set": updates},
            )
        )

    async def store_account_alive_failure(
        self,
        account_id: str,
        error: PlanCheckError,
    ) -> None:
        await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {"$set": {
                    "aliveStatus": "unknown",
                    "aliveCheckedAt": utc_now(),
                    "aliveErrorCode": error.code,
                    "aliveHttpStatus": error.http_status,
                }},
            )
        )

    async def store_account_plan_result(
        self,
        account_id: str,
        result: AccountPlanResult,
    ) -> None:
        updates: dict[str, Any] = {
            "planCheckStatus": "success",
            "planCheckedAt": result.checked_at,
            "planCheckErrorCode": None,
            "planCheckHttpStatus": result.http_status,
            "planAccountId": result.account_id,
            "subscriptionPlan": result.subscription_plan,
            "hasActiveSubscription": result.has_active_subscription,
            "planExpiresAt": result.expires_at,
            "planRenewsAt": result.renews_at,
            "promotionEligible": result.plus_trial_eligible,
            "promotionKind": result.plus_promotion_kind,
            "promotionCampaignId": result.plus_trial_campaign_id,
        }
        normalized_plan = (result.current_plan_type or "").casefold()
        if normalized_plan in {"free", "plus"}:
            updates["accountType"] = normalized_plan
        result_update = await self._guard(
            self.accounts.update_one({"_id": account_id}, {"$set": updates})
        )
        if int(result_update.matched_count) != 1:
            raise ResourceNotFoundError("套餐查询对应账号不存在")

    async def store_account_plan_failure(
        self,
        account_id: str,
        error: PlanCheckError,
    ) -> None:
        updates: dict[str, Any] = {
            "planCheckStatus": "failed",
            "planCheckedAt": utc_now(),
            "planCheckErrorCode": error.code,
            "planCheckHttpStatus": error.http_status,
        }
        if error.code in {"access_token_expired", "access_token_unauthorized"}:
            updates["accessTokenConfigured"] = False
        await self._guard(
            self.accounts.update_one({"_id": account_id}, {"$set": updates})
        )

    async def store_account_checkout_type(
        self,
        account_id: str,
        result: CheckoutTypeResult,
    ) -> None:
        update = await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {
                    "$set": {
                        "checkoutType": result.checkout_type,
                        "checkoutTypeDetail": result.checkout_detail,
                        "checkoutTypeCheckedAt": result.checked_at,
                        "checkoutTypeErrorCode": None,
                        "checkoutTypeHttpStatus": None,
                        "checkoutTypeCheckStatus": "success",
                    }
                },
            )
        )
        if int(update.matched_count) != 1:
            raise ResourceNotFoundError("结账类型对应账号不存在")

    async def store_account_checkout_type_failure(
        self,
        account_id: str,
        error: CheckoutTypeCheckError,
    ) -> None:
        await self._guard(
            self.accounts.update_one(
                {"_id": account_id},
                {"$set": {
                    "checkoutTypeErrorCode": error.code,
                    "checkoutTypeHttpStatus": error.http_status,
                    "checkoutTypeCheckedAt": utc_now(),
                    "checkoutTypeCheckStatus": "failed",
                }},
            )
        )

    async def list_emails(
        self, page: int, page_size: PageSize, query: str, source: str = "all"
    ) -> Page[EmailRecord]:
        mongo_query: dict[str, Any] = {"status": "available"}
        mongo_query.update(email_source_filter(source))
        if query.strip():
            mongo_query["emailNormalized"] = {
                "$regex": re.escape(query.strip().lower()),
            }
        mongo_query = await self._exclude_registered_accounts(mongo_query)
        total = await self._guard(self.emails.count_documents(mongo_query))
        cursor = (
            self.emails.find(mongo_query)
            .sort("importedAt", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        documents = await self._guard(cursor.to_list(length=page_size))
        return Page[EmailRecord](
            items=[self._email_record(item) for item in documents],
            total=total,
            page=page,
            pageSize=page_size,
        )

    async def upsert_email(
        self,
        email: str,
        access_url: str,
        *,
        mailbox_kind: str = "url",
        mailbox_password: str | None = None,
        source_type: str = "manual",
        parent_email: str | None = None,
    ) -> bool:
        normalized_email = normalize_email(email)
        registered_account = await self._guard(
            self.accounts.find_one(
                {"emailNormalized": normalized_email},
                {"_id": 1},
            )
        )
        if registered_account is not None:
            return False
        document = {
            "_id": str(uuid4()),
            "email": email,
            "emailNormalized": normalized_email,
            "accessUrl": access_url,
            "importedAt": utc_now(),
            "status": "available",
            "mailboxKind": mailbox_kind,
            "sourceType": (
                "mailcom_alias" if source_type == "mailcom_alias" else "manual"
            ),
        }
        if source_type == "mailcom_alias" and parent_email:
            document["parentEmail"] = normalize_email(parent_email)
        if mailbox_kind == "mailcom_imap" and mailbox_password:
            document["mailboxPassword"] = mailbox_password
        set_on_insert = dict(document)
        update: dict[str, Any] = {"$setOnInsert": set_on_insert}
        if source_type == "mailcom_alias":
            mutable_fields = {
                "accessUrl": access_url,
                "sourceType": "mailcom_alias",
                "parentEmail": normalize_email(parent_email or ""),
            }
            for field in mutable_fields:
                set_on_insert.pop(field, None)
            update["$set"] = mutable_fields
        result = await self._guard(
            self.emails.update_one(
                {"emailNormalized": document["emailNormalized"]},
                update,
                upsert=True,
            )
        )
        return result.upserted_id is not None

    async def delete_emails(self, ids: list[str]) -> DeleteResult:
        result = await self._guard(self.emails.delete_many({"_id": {"$in": ids}}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def emails_for_export(self, ids: list[str] | None) -> list[EmailRecord]:
        query: dict[str, Any] = {"status": "available"}
        if ids is not None:
            query["_id"] = {"$in": ids}
        query = await self._exclude_registered_accounts(query)
        cursor = self.emails.find(query).sort("importedAt", DESCENDING)
        documents = await self._guard(cursor.to_list(length=None))
        return [self._email_record(item) for item in documents]

    async def reserve_emails(
        self,
        count: int,
        run_id: str,
        email_source: str = "all",
    ) -> list[dict[str, Any]]:
        reserved: list[dict[str, Any]] = []
        query = await self._exclude_registered_accounts(
            registration_email_filter(email_source)
        )
        candidate_cursor = self.emails.find(
            query,
            {
                "_id": 1,
                "emailNormalized": 1,
                "parentEmail": 1,
                "lastAttemptAt": 1,
                "importedAt": 1,
            },
        ).sort([("lastAttemptAt", ASCENDING), ("importedAt", DESCENDING)])
        candidates = await self._guard(candidate_cursor.to_list(length=None))
        for candidate in interleave_email_parent_groups(candidates):
            if len(reserved) >= count:
                break
            document = await self._guard(
                self.emails.find_one_and_update(
                    {"$and": [query, {"_id": candidate["_id"]}]},
                    {
                        "$set": {
                            "status": "reserved",
                            "reservedBy": run_id,
                            "reservedAt": utc_now(),
                        }
                    },
                    return_document=ReturnDocument.AFTER,
                )
            )
            if document is not None:
                reserved.append(document)
        if len(reserved) < count:
            await self.release_run_reservations(run_id)
            raise InsufficientEmailsError("可用邮箱数量不足")
        return reserved

    async def get_reserved_email(
        self,
        email_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        document = await self._guard(
            self.emails.find_one(
                {
                    "_id": email_id,
                    "status": "reserved",
                    "reservedBy": run_id,
                }
            )
        )
        if document is None:
            raise ResourceNotFoundError("预留邮箱不存在或不属于当前真实探测任务")
        return document

    async def release_email(self, email_id: str, run_id: str) -> None:
        await self._guard(
            self.emails.update_one(
                {"_id": email_id, "reservedBy": run_id},
                {
                    "$set": {
                        "status": "available",
                        "lastAttemptAt": utc_now(),
                    },
                    "$unset": {"reservedBy": "", "reservedAt": ""},
                },
            )
        )

    async def reserve_rebind_email(
        self,
        run_id: str,
        exclude_email: str = "",
        source: str = "standard",
    ) -> dict[str, Any] | None:
        """Reserve one shared-pool email exclusively for this rebind task."""
        query: dict[str, Any] = {
            "status": "available",
            "usagePurpose": {"$ne": "rebind"},
        }
        query.update(email_source_filter(source))
        normalized_exclusion = exclude_email.strip().lower()
        if normalized_exclusion:
            exclusions = [
                {"emailNormalized": {"$ne": normalized_exclusion}},
                {"email": {"$ne": normalized_exclusion}},
            ]
            if "$or" in query:
                source_filter = {"$or": query.pop("$or")}
                query["$and"] = [source_filter, *exclusions]
            else:
                query["$and"] = exclusions
        result = await self._guard(self.emails.find_one_and_update(
            query,
            {"$set": {"status": "reserved", "usagePurpose": "rebind", "rebindReservedBy": run_id, "reservedAt": utc_now()}},
            sort=[("importedAt", DESCENDING)],
            return_document=ReturnDocument.AFTER,
        ))
        return result

    async def release_rebind_email(self, email_id: str, run_id: str) -> bool:
        result = await self._guard(self.emails.update_one(
            {"_id": email_id, "rebindReservedBy": run_id},
            {"$set": {"status": "available", "usagePurpose": "registration"}, "$unset": {"rebindReservedBy": "", "reservedAt": ""}},
        ))
        return bool(result.modified_count)

    async def reserve_specific_rebind_email(
        self,
        email_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Reacquire a task's previously selected mailbox when it is still free."""
        return await self._guard(self.emails.find_one_and_update(
            {
                "_id": email_id,
                "$or": [
                    {"status": "available"},
                    {"status": "reserved", "rebindReservedBy": run_id},
                ],
            },
            {
                "$set": {
                    "status": "reserved",
                    "usagePurpose": "rebind",
                    "rebindReservedBy": run_id,
                    "reservedAt": utc_now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        ))

    async def remember_rebind_mailbox(
        self,
        account_id: str,
        mailbox: dict[str, Any],
        run_id: str,
    ) -> None:
        await self._guard(self.accounts.update_one(
            {"_id": account_id},
            {
                "$set": {
                    "rebindMailboxId": str(mailbox.get("_id") or ""),
                    "rebindTargetEmail": str(mailbox.get("email") or "").strip().lower(),
                    "rebindRunId": run_id,
                    "rebindMailboxSource": str(mailbox.get("sourceType") or "standard"),
                    "rebindStatus": "in_progress",
                    "updatedAt": utc_now(),
                }
            },
        ))

    async def mark_rebind_email_changed(
        self,
        account_id: str,
        old_email: str,
        new_email: str,
        new_email_access_url: str,
        proxy: str = "",
        proxy_country: str = "",
    ) -> None:
        """Persist the irreversible remote email change before AT confirmation."""
        now = utc_now()
        normalized_email = new_email.strip().lower()
        result = await self._guard(self.accounts.update_one(
            {"_id": account_id},
            {
                "$set": {
                    "email": normalized_email,
                    "emailNormalized": normalized_email,
                    "emailAccessUrl": new_email_access_url.strip(),
                    "rebindStatus": "email_changed_token_pending",
                    "previousEmail": old_email.strip().lower(),
                    "reboundEmail": normalized_email,
                    "rebindProxy": proxy,
                    "rebindProxyCountry": normalize_country_code(proxy_country) if proxy_country else None,
                    "reboundAt": now,
                    "updatedAt": now,
                },
                "$unset": {"rebindError": ""},
            },
        ))
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("换绑账号不存在")

    async def update_account_access_token(
        self,
        account_id: str,
        access_token: str,
        access_token_expires_at: datetime | None,
        *,
        rebind_success: bool = False,
    ) -> None:
        now = utc_now()
        updates: dict[str, Any] = {
            "accessToken": access_token,
            "accessTokenConfigured": bool(access_token),
            "accessTokenExpiresAt": access_token_expires_at,
            "accessTokenUpdatedAt": now,
            "aliveStatus": "unknown",
            "updatedAt": now,
        }
        if rebind_success:
            updates["rebindStatus"] = "success"
        result = await self._guard(self.accounts.update_one(
            {"_id": account_id},
            {"$set": updates, "$unset": {"rebindError": "", "aliveError": ""}},
        ))
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("账号不存在")

    async def clear_rebind_retry_state(self, account_id: str) -> None:
        await self._guard(self.accounts.update_one(
            {"_id": account_id, "rebindStatus": {"$in": ["failed", "in_progress"]}},
            {
                "$unset": {
                    "rebindStatus": "",
                    "rebindError": "",
                    "rebindMailboxId": "",
                    "rebindTargetEmail": "",
                    "rebindRunId": "",
                    "rebindMailboxSource": "",
                },
                "$set": {"updatedAt": utc_now()},
            },
        ))

    async def append_rebind_log(self, entry: dict[str, Any]) -> None:
        document = dict(entry)
        document["createdAt"] = utc_now()
        await self._guard(self.rebind_logs.insert_one(document))

    async def list_rebind_logs(self, limit: int = 300) -> list[dict[str, Any]]:
        documents = await self._guard(
            self.rebind_logs.find({}, {"_id": 0, "createdAt": 0})
            .sort("createdAt", DESCENDING)
            .limit(max(1, min(1000, limit)))
            .to_list(length=max(1, min(1000, limit)))
        )
        documents.reverse()
        return documents

    async def save_rebind_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("taskId") or "")
        if not task_id:
            return
        document = dict(task)
        document["updatedAt"] = utc_now()
        try:
            await self._guard(self.rebind_tasks.update_one(
                {"taskId": task_id, "deletedAt": {"$exists": False}},
                {"$set": document},
                upsert=True,
            ))
        except DuplicateKeyError:
            # A cancellation tombstone wins over a late progress write.
            return

    async def list_rebind_tasks(self, limit: int = 500) -> list[dict[str, Any]]:
        return await self._guard(
            self.rebind_tasks.find({"deletedAt": {"$exists": False}}, {"_id": 0})
            .sort("updatedAt", DESCENDING)
            .limit(max(1, min(1000, limit)))
            .to_list(length=max(1, min(1000, limit)))
        )

    async def delete_rebind_task(self, task_id: str) -> None:
        await self._guard(self.rebind_tasks.update_one(
            {"taskId": task_id},
            {
                "$set": {"deletedAt": utc_now(), "updatedAt": utc_now()},
                "$unset": {"items": ""},
            },
            upsert=True,
        ))

    async def mark_rebind_success(
        self,
        account_id: str,
        old_email: str,
        new_email: str,
        new_email_access_url: str,
        access_token: str,
        access_token_expires_at: datetime | None,
        proxy: str = "",
        proxy_country: str = "",
    ) -> None:
        now = utc_now()
        normalized_email = new_email.strip().lower()
        result = await self._guard(self.accounts.update_one(
            {"_id": account_id},
            {
                "$set": {
                    "email": normalized_email,
                    "emailNormalized": normalized_email,
                    "emailAccessUrl": new_email_access_url.strip(),
                    "accessToken": access_token,
                    "accessTokenConfigured": bool(access_token),
                    "accessTokenExpiresAt": access_token_expires_at,
                    "accessTokenUpdatedAt": now,
                    "rebindStatus": "success",
                    "previousEmail": old_email.strip().lower(),
                    "reboundEmail": normalized_email,
                    "rebindProxy": proxy,
                    "rebindProxyCountry": normalize_country_code(proxy_country) if proxy_country else None,
                    "reboundAt": now,
                    "updatedAt": now,
                },
                "$unset": {"rebindError": ""},
            },
        ))
        if int(result.matched_count) != 1:
            raise ResourceNotFoundError("换绑账号不存在")

    async def consume_rebind_email(self, email_id: str, run_id: str) -> bool:
        result = await self._guard(self.emails.update_one(
            {"_id": email_id, "rebindReservedBy": run_id},
            {"$set": {"status": "used", "usagePurpose": "rebind"}, "$unset": {"rebindReservedBy": "", "reservedAt": ""}},
        ))
        return bool(result.modified_count)

    async def discard_reserved_email(self, email_id: str, run_id: str) -> bool:
        result = await self._guard(
            self.emails.delete_one({"_id": email_id, "reservedBy": run_id})
        )
        return bool(result.deleted_count)

    async def release_run_reservations(self, run_id: str) -> None:
        await self._guard(
            self.emails.update_many(
                {"reservedBy": run_id},
                {
                    "$set": {"status": "available"},
                    "$unset": {"reservedBy": "", "reservedAt": ""},
                },
            )
        )

    async def complete_mock_success(
        self,
        source: dict[str, Any],
        run_id: str,
        promotion_eligible: bool,
    ) -> AccountRecord:
        fragment = re.sub(r"[^a-zA-Z0-9]", "", source["email"].split("@")[0])[:12]
        fragment = fragment or "account"
        document = {
            "_id": str(uuid4()),
            "email": source["email"],
            "emailNormalized": normalize_email(source["email"]),
            "chatgptPassword": f"Mock!{fragment}#2026",
            "totpSecret": (f"MOCK{fragment.upper()}" + "X" * 20)[:20],
            "emailAccessUrl": source["accessUrl"],
            "createdAt": utc_now(),
            "accountType": "free",
            "phoneBound": None,
            "promotionEligible": promotion_eligible,
            "sourceEmailId": source["_id"],
        }
        if source.get("mailboxKind") == "mailcom_imap":
            document["mailboxKind"] = "mailcom_imap"
            document["mailboxPassword"] = source.get("mailboxPassword", "")
        await self._guard(
            self.accounts.update_one(
                {"emailNormalized": document["emailNormalized"]},
                {"$setOnInsert": document},
                upsert=True,
            )
        )
        await self._guard(
            self.emails.delete_one({"_id": source["_id"], "reservedBy": run_id})
        )
        stored = await self._guard(
            self.accounts.find_one({"emailNormalized": document["emailNormalized"]})
        )
        if stored is None:
            raise ResourceNotFoundError("账号写入后无法读取")
        return self._account_record(stored)

    async def complete_probe_profile_success(
        self,
        source: dict[str, Any],
        run_id: str,
        chatgpt_password: str = "",
        registration_country: str | None = None,
        registration_proxy_group: str | None = None,
    ) -> AccountRecord:
        document = {
            "_id": str(uuid4()),
            "email": source["email"],
            "emailNormalized": normalize_email(source["email"]),
            "chatgptPassword": chatgpt_password,
            "totpSecret": "",
            "emailAccessUrl": source["accessUrl"],
            "createdAt": utc_now(),
            "accountType": "free",
            "phoneBound": None,
            "promotionEligible": None,
            "registrationCountry": (
                normalize_country_code(registration_country)
                if registration_country
                else None
            ),
            "registrationProxyGroup": (
                " ".join(str(registration_proxy_group or "").split()) or None
            ),
            "sourceEmailId": source["_id"],
            "accessTokenConfigured": False,
            "accessTokenExpiresAt": None,
            "accessTokenUpdatedAt": None,
        }
        if source.get("mailboxKind") == "mailcom_imap":
            document["mailboxKind"] = "mailcom_imap"
            document["mailboxPassword"] = source.get("mailboxPassword", "")
        await self._guard(
            self.accounts.update_one(
                {"emailNormalized": document["emailNormalized"]},
                {"$setOnInsert": document},
                upsert=True,
            )
        )
        await self._guard(
            self.emails.delete_one(
                {
                    "_id": source["_id"],
                    "emailNormalized": document["emailNormalized"],
                    "reservedBy": run_id,
                }
            )
        )
        stored = await self._guard(
            self.accounts.find_one(
                {"emailNormalized": document["emailNormalized"]}
            )
        )
        if stored is None:
            raise ResourceNotFoundError("账号资料完成后无法读取账号记录")
        return self._account_record(stored)

    async def release_expired_proxy_quarantines(self) -> int:
        """Make expired/legacy quarantines visible and selectable again."""
        now = utc_now()
        result = await self._guard(
            self.proxies.update_many(
                {
                    "status": "quarantined",
                    "$or": [
                        {"quarantineUntil": {"$exists": False}},
                        {"quarantineUntil": None},
                        {"quarantineUntil": {"$lte": now}},
                    ],
                },
                {
                    "$set": {"status": "unknown", "consecutiveFailures": 0},
                    "$unset": {"quarantineUntil": ""},
                },
            )
        )
        return int(result.modified_count)

    async def list_proxies(
        self, page: int, page_size: PageSize, query: str, country: str = ""
    ) -> Page[ProxyRecord]:
        await self.release_expired_proxy_quarantines()
        mongo_query: dict[str, Any] = {}
        if query.strip():
            escaped = re.escape(query.strip())
            mongo_query["$or"] = [
                {"host": {"$regex": escaped, "$options": "i"}},
                {"username": {"$regex": escaped, "$options": "i"}},
            ]
        if country.strip():
            country_query = proxy_country_filter(country)
            if "$or" in mongo_query:
                mongo_query = {"$and": [mongo_query, country_query]}
            else:
                mongo_query.update(country_query)
        total = await self._guard(self.proxies.count_documents(mongo_query))
        cursor = (
            self.proxies.find(mongo_query)
            .sort("createdAt", DESCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        documents = await self._guard(cursor.to_list(length=page_size))
        return Page[ProxyRecord](
            items=[self._proxy_record(item) for item in documents],
            total=total,
            page=page,
            pageSize=page_size,
        )

    async def proxy_documents_for_test(
        self,
        country: str | None = None,
        group: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        now = utc_now()
        query: dict[str, Any] = {
            "$and": [
                usable_proxy_status_filter(now),
                {
                    "$or": [
                        {"activeLeaseOwners": {"$exists": False}},
                        {"activeLeaseOwners": {"$size": 0}},
                        {"activeLeaseCount": {"$lte": 0}},
                    ]
                },
                {
                    "$or": [
                        {"leaseUntil": {"$exists": False}},
                        {"leaseUntil": None},
                        {"leaseUntil": {"$lte": now}},
                    ]
                },
            ]
        }
        if country:
            query = {"$and": [query, proxy_country_filter(country)]}
        if group:
            group_filter = {"group": normalize_proxy_group(group)}
            query = {"$and": [query, group_filter]}
        cursor = self.proxies.find(query).sort("lastCheckedAt", ASCENDING)
        if limit is not None:
            cursor = cursor.limit(max(1, limit))
        return await self._guard(cursor.to_list(length=limit))

    async def payment_extractor_proxy_pool(self, country: str, group: str) -> list[str]:
        await self.release_expired_proxy_quarantines()
        query: dict[str, Any] = {
            "$and": [{"enabled": True}, usable_proxy_status_filter(utc_now())]
        }
        if country:
            query = {"$and": [query, proxy_country_filter(country)]}
        if group:
            query = {"$and": [query, {"group": normalize_proxy_group(group)}]}
        documents = await self._guard(
            self.proxies.find(query).sort([("latencyMs", ASCENDING), ("createdAt", DESCENDING)]).to_list(length=None)
        )
        lines: list[str] = []
        for item in documents:
            scheme = str(item.get("scheme") or "http").lower()
            if scheme not in SUPPORTED_PROXY_SCHEMES:
                scheme = "http"
            host = str(item.get("host") or "").strip()
            port = int(item.get("port") or 0)
            if not host or not port:
                continue
            username = str(item.get("username") or "")
            password = str(item.get("password") or "")
            auth = ""
            if username or password:
                auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
            lines.append(f"{scheme}://{auth}{host}:{port}")
        return lines

    async def record_proxy_test(
        self,
        proxy_id: str,
        *,
        available: bool,
        latency_ms: int | None = None,
        country: str | None = None,
    ) -> None:
        changes: dict[str, Any] = {
            "latencyMs": latency_ms if available else None,
            "lastCheckedAt": utc_now(),
        }
        if available and country:
            changes["country"] = normalize_country_code(country)
        if available:
            changes["status"] = "available"
            changes["consecutiveFailures"] = 0
            update: dict[str, Any] | list[dict[str, Any]] = {
                "$set": changes,
                "$unset": {"quarantineUntil": ""},
            }
        else:
            # A single timeout is not enough evidence to disable a proxy.
            # Keep a previously healthy proxy selectable until repeated
            # failures reach the configured quarantine threshold.
            threshold = max(
                2, int(os.getenv("AUTOREGISTER_PROXY_DELETE_AFTER_FAILURES", "3"))
            )
            next_failures = {"$add": [{"$ifNull": ["$consecutiveFailures", 0]}, 1]}
            quarantine_until = utc_now() + timedelta(
                seconds=proxy_quarantine_seconds()
            )
            update = [
                {
                    "$set": {
                        **changes,
                        "consecutiveFailures": next_failures,
                        "status": {
                            "$cond": [
                                {"$gte": [next_failures, threshold]},
                                "quarantined",
                                {
                                    "$cond": [
                                        {"$eq": ["$status", "available"]},
                                        "available",
                                        "unknown",
                                    ]
                                },
                            ]
                        },
                        "quarantineUntil": {
                            "$cond": [
                                {"$gte": [next_failures, threshold]},
                                quarantine_until,
                                "$quarantineUntil",
                            ]
                        },
                    }
                }
            ]
        await self._guard(self.proxies.update_one({"_id": proxy_id}, update))

    async def delete_repeatedly_failed_proxies(self, threshold: int) -> int:
        """Deprecated safety shim: automatic health cleanup never deletes proxies."""
        _ = threshold
        return 0

    async def upsert_proxy(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        scheme: str = "http",
        country: str | None = None,
        group: str | None = None,
    ) -> bool:
        identity = {"host": host, "port": port, "username": username, "password": password}
        document = {
            "_id": str(uuid4()),
            **identity,
            "enabled": True,
            "status": "unknown",
            "latencyMs": None,
            "lastCheckedAt": None,
            "country": normalize_country_code(
                country or infer_proxy_country(username, host)
            ),
            "group": normalize_proxy_group(group),
            "scheme": scheme if scheme in SUPPORTED_PROXY_SCHEMES else "http",
            "createdAt": utc_now(),
        }
        updates = {"scheme": scheme if scheme in SUPPORTED_PROXY_SCHEMES else "http"}
        if group is not None:
            updates["group"] = normalize_proxy_group(group)
        if country is not None:
            updates["country"] = normalize_country_code(country)
        # MongoDB rejects an upsert when the same path appears in both
        # $setOnInsert and $set. Keep mutable fields exclusively in $set so a
        # repeated import can update the protocol/country without conflicting
        # with a first-time insert.
        insert_only = {
            key: value for key, value in document.items() if key not in updates
        }
        update: dict[str, Any] = {"$setOnInsert": insert_only, "$set": updates}
        result = await self._guard(self.proxies.update_one(identity, update, upsert=True))
        return result.upserted_id is not None or bool(getattr(result, "modified_count", 0))

    async def update_proxy(
        self,
        proxy_id: str,
        *,
        enabled: bool | None = None,
        country: str | None = None,
        group: str | None = None,
    ) -> ProxyRecord:
        changes: dict[str, Any] = {}
        if enabled is not None:
            changes["enabled"] = enabled
        if country is not None:
            changes["country"] = normalize_country_code(country)
        if group is not None:
            changes["group"] = normalize_proxy_group(group)
        document = await self._guard(
            self.proxies.find_one_and_update(
                {"_id": proxy_id},
                {"$set": changes},
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is None:
            raise ResourceNotFoundError("代理不存在")
        return self._proxy_record(document)

    async def proxy_country_summaries(self) -> list[ProxyCountrySummary]:
        await self.release_expired_proxy_quarantines()
        cursor = self.proxies.find({}, {"country": 1, "username": 1, "host": 1, "enabled": 1})
        documents = await self._guard(cursor.to_list(length=None))
        counts: dict[str, list[int]] = {}
        for document in documents:
            country = normalize_country_code(document.get("country"))
            if country == "ZZ":
                country = infer_proxy_country(
                    str(document.get("username") or ""),
                    str(document.get("host") or ""),
                )
            values = counts.setdefault(country, [0, 0])
            values[0] += 1
            if bool(document.get("enabled", False)):
                values[1] += 1
        return [
            ProxyCountrySummary(country=country, total=values[0], enabled=values[1])
            for country, values in sorted(counts.items())
        ]

    async def proxy_group_summaries(self) -> list[ProxyGroupSummary]:
        await self.release_expired_proxy_quarantines()
        cursor = self.proxies.find(
            {},
            {
                "country": 1,
                "group": 1,
                "username": 1,
                "host": 1,
                "enabled": 1,
                "status": 1,
                "scheme": 1,
            },
        )
        documents = await self._guard(cursor.to_list(length=None))
        counts: dict[tuple[str, str], dict[str, Any]] = {}
        for document in documents:
            country = normalize_country_code(document.get("country"))
            if country == "ZZ":
                country = infer_proxy_country(
                    str(document.get("username") or ""),
                    str(document.get("host") or ""),
                )
            group = normalize_proxy_group(document.get("group"))
            values = counts.setdefault(
                (country, group),
                {"total": 0, "enabled": 0, "available": 0, "quarantined": 0, "schemes": set()},
            )
            values["total"] += 1
            if bool(document.get("enabled", False)):
                values["enabled"] += 1
            status = str(document.get("status") or "unknown")
            if status == "available":
                values["available"] += 1
            elif status == "quarantined":
                values["quarantined"] += 1
            scheme = str(document.get("scheme") or "http").lower()
            values["schemes"].add(
                scheme if scheme in SUPPORTED_PROXY_SCHEMES else "http"
            )
        return [
            ProxyGroupSummary(
                country=country,
                group=group,
                total=values["total"],
                enabled=values["enabled"],
                available=values["available"],
                quarantined=values["quarantined"],
                schemes=sorted(values["schemes"]),
            )
            for (country, group), values in sorted(counts.items())
        ]

    async def update_proxy_group(
        self,
        country: str,
        group: str,
        *,
        new_country: str | None = None,
        new_group: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, int]:
        query = {"$and": [proxy_country_filter(country), proxy_group_filter(group)]}
        changes: dict[str, Any] = {}
        if new_country is not None:
            changes["country"] = normalize_country_code(new_country)
        if new_group is not None:
            changes["group"] = normalize_proxy_group(new_group)
        if enabled is not None:
            changes["enabled"] = enabled
        result = await self._guard(self.proxies.update_many(query, {"$set": changes}))
        return {"matched": int(result.matched_count), "modified": int(result.modified_count)}

    async def delete_proxy_group(self, country: str, group: str) -> DeleteResult:
        query = {"$and": [proxy_country_filter(country), proxy_group_filter(group)]}
        result = await self._guard(self.proxies.delete_many(query))
        return DeleteResult(deleted=int(result.deleted_count))

    async def enabled_proxy_urls(
        self,
        country: str,
        *,
        group: str | None = None,
        scheme: str | None = None,
    ) -> list[str]:
        normalized = normalize_country_code(country)
        if normalized == "ZZ":
            return []
        await self.release_expired_proxy_quarantines()
        filters: list[dict[str, Any]] = [
            proxy_country_filter(normalized),
            {"enabled": True},
            usable_proxy_status_filter(utc_now()),
        ]
        if group:
            filters.append(proxy_group_filter(group))
        query = {"$and": filters}
        cursor = self.proxies.find(query).sort("createdAt", ASCENDING)
        documents = await self._guard(cursor.to_list(length=None))
        result: list[str] = []
        for document in documents:
            host = str(document.get("host") or "").strip()
            port = int(document.get("port") or 0)
            username = quote(str(document.get("username") or ""), safe="")
            password = quote(str(document.get("password") or ""), safe="")
            if not host or port < 1:
                continue
            credentials = f"{username}:{password}@" if username or password else ""
            proxy_scheme = str(scheme or document.get("scheme") or "http").lower()
            if proxy_scheme not in SUPPORTED_PROXY_SCHEMES:
                proxy_scheme = "http"
            result.append(f"{proxy_scheme}://{credentials}{host}:{port}")
        return result

    async def delete_proxy(self, proxy_id: str) -> DeleteResult:
        result = await self._guard(self.proxies.delete_one({"_id": proxy_id}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def delete_proxies(self, ids: list[str]) -> DeleteResult:
        result = await self._guard(self.proxies.delete_many({"_id": {"$in": ids}}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def clear_proxies(self) -> DeleteResult:
        result = await self._guard(self.proxies.delete_many({}))
        return DeleteResult(deleted=int(result.deleted_count))

    async def overview_stats(self) -> OverviewStats:
        await self.release_expired_proxy_quarantines()
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        available_email_query = await self._exclude_registered_accounts(
            registration_email_filter()
        )
        available_alias_query = await self._exclude_registered_accounts(
            registration_email_filter("mailcom_alias")
        )
        (
            accounts_total,
            accounts_today,
            totp_complete,
            plus_total,
            plus_bound,
            free_total,
            free_eligible,
            emails_available,
            email_aliases,
            proxies_total,
            proxies_enabled,
            proxies_available,
            proxies_quarantined,
        ) = await asyncio.gather(
            self._guard(self.accounts.count_documents({})),
            self._guard(self.accounts.count_documents({"createdAt": {"$gte": today}})),
            self._guard(self.accounts.count_documents({"totpSecret": {"$nin": ["", None]}})),
            self._guard(self.accounts.count_documents({"accountType": "plus"})),
            self._guard(
                self.accounts.count_documents({"accountType": "plus", "phoneBound": True})
            ),
            self._guard(self.accounts.count_documents({"accountType": "free"})),
            self._guard(
                self.accounts.count_documents(
                    {"accountType": "free", "promotionEligible": True}
                )
            ),
            self._guard(self.emails.count_documents(available_email_query)),
            self._guard(self.emails.count_documents(available_alias_query)),
            self._guard(self.proxies.count_documents({})),
            self._guard(self.proxies.count_documents({"enabled": True})),
            self._guard(self.proxies.count_documents({"status": "available"})),
            self._guard(self.proxies.count_documents({"status": "quarantined"})),
        )
        return OverviewStats(
            accounts=AccountStats(
                total=accounts_total,
                today=accounts_today,
                totpComplete=totp_complete,
                plus=PlusStats(
                    total=plus_total,
                    bound=plus_bound,
                    unbound=plus_total - plus_bound,
                ),
                free=FreeStats(
                    total=free_total,
                    eligible=free_eligible,
                    ineligible=free_total - free_eligible,
                ),
            ),
            emails=EmailStats(available=emails_available, aliases=email_aliases),
            proxies=ProxyStats(
                total=proxies_total,
                enabled=proxies_enabled,
                available=proxies_available,
                quarantined=proxies_quarantined,
            ),
        )

    @staticmethod
    def _account_record(document: dict[str, Any]) -> AccountRecord:
        campaign_id = str(document.get("promotionCampaignId") or "")
        campaign_key = campaign_id.casefold()
        stored_kind = document.get("promotionKind")
        derived_kind = stored_kind or (
            "discount" if any(marker in campaign_key for marker in ("50-pct", "50_pct", "discount", "half-price")) else None
        )
        derived_eligible = document.get("promotionEligible")
        if derived_kind == "discount":
            derived_eligible = False
        return AccountRecord(
            id=str(document["_id"]),
            email=document["email"],
            chatgptPassword=str(document.get("chatgptPassword") or ""),
            totpSecret=str(document.get("totpSecret") or ""),
            emailAccessUrl=direct_mailbox_access_url(
                str(document.get("emailAccessUrl") or ""), document["email"]
            ),
            createdAt=document["createdAt"],
            accountType=document["accountType"],
            phoneBound=document.get("phoneBound"),
            promotionEligible=derived_eligible,
            accessTokenConfigured=bool(document.get("accessTokenConfigured", False)),
            accessTokenExpiresAt=document.get("accessTokenExpiresAt"),
            accessTokenUpdatedAt=document.get("accessTokenUpdatedAt"),
            planCheckStatus=document.get("planCheckStatus"),
            planCheckedAt=document.get("planCheckedAt"),
            planCheckErrorCode=document.get("planCheckErrorCode"),
            planCheckHttpStatus=document.get("planCheckHttpStatus"),
            planAccountId=document.get("planAccountId"),
            subscriptionPlan=document.get("subscriptionPlan"),
            hasActiveSubscription=document.get("hasActiveSubscription"),
            planExpiresAt=document.get("planExpiresAt"),
            planRenewsAt=document.get("planRenewsAt"),
            promotionCampaignId=document.get("promotionCampaignId"),
            promotionKind=derived_kind,
            checkoutType=document.get("checkoutType"),
            checkoutTypeDetail=document.get("checkoutTypeDetail"),
            checkoutTypeCheckedAt=document.get("checkoutTypeCheckedAt"),
            checkoutTypeErrorCode=document.get("checkoutTypeErrorCode"),
            checkoutTypeHttpStatus=document.get("checkoutTypeHttpStatus"),
            checkoutTypeCheckStatus=document.get("checkoutTypeCheckStatus"),
            registrationCountry=document.get("registrationCountry"),
            rebindStatus=document.get("rebindStatus"),
            previousEmail=document.get("previousEmail"),
            reboundEmail=document.get("reboundEmail"),
            rebindProxy=document.get("rebindProxy"),
            rebindProxyCountry=document.get("rebindProxyCountry"),
            aliveStatus=document.get("aliveStatus"),
            aliveCheckedAt=document.get("aliveCheckedAt"),
            aliveErrorCode=document.get("aliveErrorCode"),
            aliveHttpStatus=document.get("aliveHttpStatus"),
            alive15mVerifiedAt=document.get("alive15mVerifiedAt"),
            globalPromotionStatus=document.get("globalPromotionStatus"),
            globalPromotionEligible=document.get("globalPromotionEligible"),
            globalPromotionCheckedAt=document.get("globalPromotionCheckedAt"),
            globalPromotionProxyCount=int(document.get("globalPromotionProxyCount") or 0),
            globalPromotionCountries=list(document.get("globalPromotionCountries") or []),
            globalPromotionResults=list(document.get("globalPromotionResults") or []),
            globalPromotionMessage=document.get("globalPromotionMessage"),
            oaicsScanStatus=document.get("oaicsScanStatus"),
            oaicsScanCheckedAt=document.get("oaicsScanCheckedAt"),
            oaicsScanTotal=int(document.get("oaicsScanTotal") or 0),
            oaicsScanSuccess=int(document.get("oaicsScanSuccess") or 0),
            oaicsScanCountryStats=list(document.get("oaicsScanCountryStats") or []),
            oaicsScanResults=list(document.get("oaicsScanResults") or []),
            oaicsScanMessage=document.get("oaicsScanMessage"),
        )

    @staticmethod
    def _email_record(document: dict[str, Any]) -> EmailRecord:
        return EmailRecord(
            id=str(document["_id"]),
            email=document["email"],
            accessUrl=direct_mailbox_access_url(
                document["accessUrl"], document["email"]
            ),
            importedAt=document["importedAt"],
            sourceType=(
                "mailcom_alias"
                if document.get("sourceType") == "mailcom_alias"
                else "manual"
            ),
            parentEmail=(
                str(document.get("parentEmail") or "") or None
            ),
            mailboxKind=str(document.get("mailboxKind") or "url"),
            mailboxPassword=(str(document.get("mailboxPassword") or "") or None),
            usagePurpose=str(document.get("usagePurpose") or "registration"),
            rebindReservedBy=(str(document.get("rebindReservedBy") or "") or None),
        )

    async def _exclude_registered_accounts(
        self,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        registered = await self._guard(
            self.accounts.distinct(
                "emailNormalized",
                {"emailNormalized": {"$type": "string"}},
            )
        )
        normalized = [value for value in registered if isinstance(value, str) and value]
        if not normalized:
            return query
        return {
            "$and": [
                query,
                {"emailNormalized": {"$nin": normalized}},
            ]
        }

    @staticmethod
    def _proxy_record(document: dict[str, Any]) -> ProxyRecord:
        return ProxyRecord(
            id=str(document["_id"]),
            host=document["host"],
            port=document["port"],
            username=document["username"],
            password=document["password"],
            enabled=document["enabled"],
            status=document["status"],
            latencyMs=document.get("latencyMs"),
            lastCheckedAt=document.get("lastCheckedAt"),
            country=(
                normalize_country_code(document.get("country"))
                if normalize_country_code(document.get("country")) != "ZZ"
                else infer_proxy_country(
                    str(document.get("username") or ""),
                    str(document.get("host") or ""),
                )
            ),
            group=normalize_proxy_group(document.get("group")),
            scheme=(
                str(document.get("scheme") or "http").lower()
                if str(document.get("scheme") or "http").lower()
                in SUPPORTED_PROXY_SCHEMES
                else "http"
            ),
        )


class ResourceService:
    def __init__(self, store: MongoResourceStore) -> None:
        self.store = store

    async def import_accounts(self, raw_text: str) -> ImportResult:
        total = imported = duplicates = errors = 0
        seen: set[str] = set()
        for raw_line in raw_text.lstrip("\ufeff").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            total += 1
            parts = [part.strip() for part in line.split("----")]
            if len(parts) not in {2, 3}:
                errors += 1
                continue
            email = parts[0]
            key = normalize_email(email)
            if not EMAIL_PATTERN.fullmatch(email):
                errors += 1
                continue
            if key in seen:
                duplicates += 1
                continue

            password = ""
            totp_secret = ""
            access_url = ""
            second = parts[1]
            third = parts[2] if len(parts) == 3 else ""
            try:
                if len(parts) == 2:
                    if not valid_url(second):
                        raise ValueError("两段格式的第二段必须是接码地址")
                    access_url = second
                elif valid_url(second):
                    access_url = second
                    totp_secret = normalize_totp_secret(third)
                elif valid_url(third):
                    if not second or len(second) > 1024:
                        raise ValueError("密码为空或过长")
                    password = second
                    access_url = third
                else:
                    if not second or len(second) > 1024:
                        raise ValueError("密码为空或过长")
                    password = second
                    totp_secret = normalize_totp_secret(third)
            except ValueError:
                errors += 1
                continue

            seen.add(key)
            try:
                await self.create_account(AccountCreate(
                    email=key,
                    chatgptPassword=password,
                    totpSecret=totp_secret,
                    emailAccessUrl=access_url,
                ))
                imported += 1
            except DuplicateResourceError:
                duplicates += 1
            except (ValueError, TypeError):
                errors += 1
        return ImportResult(
            total=total,
            imported=imported,
            duplicateCount=duplicates,
            errorCount=errors,
        )

    async def import_emails(self, raw_text: str) -> ImportResult:
        total = imported = duplicates = errors = 0
        seen: set[str] = set()
        for raw_line in raw_text.lstrip("\ufeff").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            total += 1
            parts = line.split("----", 1)
            if len(parts) != 2:
                errors += 1
                continue
            email, credential = (part.strip() for part in parts)
            key = normalize_email(email)
            is_url = valid_url(credential)
            is_mailcom_password = (
                bool(credential)
                and "://" not in credential
                and len(credential) <= 1024
            )
            if (
                not EMAIL_PATTERN.fullmatch(email)
                or not (is_url or is_mailcom_password)
            ):
                errors += 1
                continue
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if is_url:
                inserted = await self.store.upsert_email(key, credential)
            else:
                inserted = await self.store.upsert_email(
                    key,
                    MAILCOM_WEBMAIL_URL,
                    mailbox_kind="mailcom_imap",
                    mailbox_password=credential,
                )
            if inserted:
                imported += 1
            else:
                duplicates += 1
        return ImportResult(
            total=total,
            imported=imported,
            duplicateCount=duplicates,
            errorCount=errors,
        )

    async def sync_mailcom_aliases(self, items: list[dict[str, Any]]) -> ImportResult:
        total = imported = duplicates = errors = 0
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("isAlias") is not True:
                continue
            total += 1
            email = str(item.get("email") or "").strip()
            parent_email = str(item.get("accountEmail") or "").strip()
            access_url = str(item.get("accessUrl") or "").strip()
            key = normalize_email(email)
            try:
                parsed = urlsplit(access_url)
                valid_local_base = (
                    parsed.scheme.casefold() == "http"
                    and (parsed.hostname or "").casefold() in {"127.0.0.1", "localhost"}
                    and (parsed.port or 80) == 3211
                    and parsed.username is None
                    and parsed.password is None
                )
                legacy_url = (
                    valid_local_base
                    and parsed.path.rstrip("/").casefold() == "/api/mail/latest"
                )
                capability_url = (
                    valid_local_base
                    and re.fullmatch(r"/code/[A-Za-z0-9_-]{32,128}", parsed.path)
                    is not None
                    and not parsed.query
                    and not parsed.fragment
                )
                valid_local_url = legacy_url or capability_url
            except ValueError:
                legacy_url = False
                capability_url = False
                valid_local_url = False
            if (
                not EMAIL_PATTERN.fullmatch(email)
                or not EMAIL_PATTERN.fullmatch(parent_email)
                or not valid_local_url
            ):
                errors += 1
                continue
            expected_url = access_url if capability_url else (
                "http://127.0.0.1:3211/api/mail/latest?"
                f"{urlencode({'email': email})}"
            )
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            inserted = await self.store.upsert_email(
                key,
                expected_url,
                source_type="mailcom_alias",
                parent_email=parent_email,
            )
            if inserted:
                imported += 1
            else:
                duplicates += 1
        return ImportResult(
            total=total,
            imported=imported,
            duplicateCount=duplicates,
            errorCount=errors,
        )

    async def import_proxies(
        self,
        raw_text: str,
        country: str | None = None,
        group: str | None = None,
    ) -> ImportResult:
        total = imported = duplicates = errors = 0
        seen: set[tuple[str, int, str, str, str]] = set()
        cleaned = raw_text.lstrip("\ufeff").strip()
        entries: list[tuple[str, str | None, str | None]] = []
        if re.search(r"(?m)^\s*proxies\s*:", cleaned):
            try:
                document = yaml.safe_load(cleaned)
            except yaml.YAMLError:
                document = None
            nodes = document.get("proxies") if isinstance(document, dict) else None
            if isinstance(nodes, list):
                for node in nodes:
                    if not isinstance(node, dict):
                        entries.append(("", None, None))
                        continue
                    scheme = str(node.get("type") or node.get("scheme") or "http").lower()
                    if scheme == "socks":
                        scheme = "socks5"
                    host = str(node.get("server") or node.get("host") or "").strip()
                    port = node.get("port")
                    username = quote(str(node.get("username") or node.get("user") or ""), safe="")
                    password = quote(str(node.get("password") or node.get("pass") or ""), safe="")
                    entries.append((
                        f"{scheme}://{username}:{password}@{host}:{port}",
                        str(node.get("country") or node.get("country_code") or "").strip() or None,
                        str(node.get("group") or "").strip() or None,
                    ))
            else:
                entries.append(("", None, None))
        else:
            entries = [(line, None, None) for line in re.split(r"\s+", cleaned)]

        for line, entry_country, entry_group in entries:
            if not line:
                total += 1
                errors += 1
                continue
            total += 1
            scheme = "http"
            if "://" in line or "@" in line or re.fullmatch(r"\[.*\]:\d+", line):
                try:
                    parsed = urlsplit(line if "://" in line else f"http://{line}")
                    scheme = parsed.scheme.lower()
                    host = str(parsed.hostname or "").strip()
                    port = int(parsed.port or 0)
                    username = unquote(parsed.username or "").strip()
                    password = unquote(parsed.password or "").strip()
                except (TypeError, ValueError):
                    errors += 1
                    continue
            else:
                parts = line.split(":")
                if len(parts) == 2:
                    host, port_text = (part.strip() for part in parts)
                    username = password = ""
                elif len(parts) >= 4 and parts[1].isdigit():
                    host, port_text, username, password = (
                        part.strip() for part in line.split(":", 3)
                    )
                elif len(parts) == 4 and parts[3].isdigit():
                    username, password, host, port_text = (
                        part.strip() for part in parts
                    )
                else:
                    errors += 1
                    continue
                try:
                    port = int(port_text)
                except ValueError:
                    errors += 1
                    continue
            if (
                not host
                or bool(username) != bool(password)
                or port < 1
                or port > 65535
            ):
                errors += 1
                continue
            if scheme not in SUPPORTED_PROXY_SCHEMES:
                errors += 1
                continue
            key = (host.lower(), port, username, password, scheme)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if await self.store.upsert_proxy(
                key[0], key[1], key[2], key[3], scheme=key[4],
                country=entry_country or country,
                group=entry_group or group,
            ):
                imported += 1
            else:
                duplicates += 1
        return ImportResult(
            total=total,
            imported=imported,
            duplicateCount=duplicates,
            errorCount=errors,
        )

    async def create_account(self, incoming: AccountCreate) -> AccountRecord:
        if not EMAIL_PATTERN.fullmatch(incoming.email) or (
            incoming.emailAccessUrl and not valid_url(incoming.emailAccessUrl)
        ):
            raise ValueError("邮箱或接码地址格式无效")
        return await self.store.create_account(incoming)

    async def export_accounts(self, incoming: AccountExportInput) -> TextExport:
        ids = None if incoming.scope == "all" else incoming.ids
        if incoming.format == "access-tokens":
            documents = await self.store.access_tokens_for_export(ids)
            now = utc_now()
            tokens: list[str] = []
            missing = 0
            expired = 0
            for document in documents:
                token = document.get("accessToken")
                if (
                    not document.get("accessTokenConfigured")
                    or not isinstance(token, str)
                    or not token
                ):
                    missing += 1
                    continue
                expires_at = document.get("accessTokenExpiresAt")
                if (
                    not isinstance(expires_at, datetime)
                    or expires_at.tzinfo is None
                    or expires_at.astimezone(timezone.utc) <= now
                ):
                    expired += 1
                    continue
                tokens.append(token)
            return TextExport(
                content="\n".join(tokens),
                filename=(
                    f"accounts-{len(tokens)}-access-tokens-"
                    f"{export_timestamp()}.txt"
                ),
                count=len(tokens),
                format=incoming.format,
                skippedMissingCount=missing,
                skippedExpiredCount=expired,
            )
        records = await self.store.accounts_for_export(ids)
        if incoming.format == "credentials":
            content = "\n".join(
                f"{item.email}----{item.chatgptPassword}----{item.totpSecret}"
                for item in records
            )
            suffix = "credentials"
        elif incoming.format == "password-mail-links":
            content = "\n".join(
                f"{item.email}----{item.chatgptPassword}----{item.emailAccessUrl}"
                for item in records
            )
            suffix = "password-mail-links"
        elif incoming.format == "mail-links-totp":
            content = "\n".join(
                f"{item.email}----{item.emailAccessUrl}----{item.totpSecret}"
                for item in records
            )
            suffix = "mail-links-totp"
        else:
            content = "\n".join(
                f"{item.email}----{item.emailAccessUrl}" for item in records
            )
            suffix = "mail-links"
        return TextExport(
            content=content,
            filename=f"accounts-{len(records)}-{suffix}-{export_timestamp()}.txt",
            count=len(records),
            format=incoming.format,
        )

    async def export_emails(self, incoming: EmailExportInput) -> TextExport:
        ids = None if incoming.scope == "all" else incoming.ids
        records = await self.store.emails_for_export(ids)
        content = "\n".join(f"{item.email}----{item.accessUrl}" for item in records)
        return TextExport(
            content=content,
            filename=f"emails-{len(records)}-mail-links-{export_timestamp()}.txt",
            count=len(records),
        )
