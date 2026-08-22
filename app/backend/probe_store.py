from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from .errors import MongoUnavailableError
from .mongo_manager import MongoManager


PROBE_LOCK_ID = "browser_probe_controller"
WORKSPACE_LOCK_PREFIX = "browser_probe_workspace:"
DEFAULT_PROXY_GROUP = "默认组"
LOCAL_PROXY_GROUP = "__local_127_0_0_1_7890__"
LOCAL_PROXY_ID_PREFIX = "local-7890:"
MAX_PROXY_COOLDOWN_SECONDS = 10 * 60


def proxy_cooldown_seconds() -> int:
    """Return the shared proxy cooldown, capped at the user-facing 10 minutes."""
    try:
        configured = int(os.getenv("AUTOREGISTER_PROXY_COOLDOWN_SECONDS", "600"))
    except (TypeError, ValueError):
        configured = MAX_PROXY_COOLDOWN_SECONDS
    return max(60, min(MAX_PROXY_COOLDOWN_SECONDS, configured))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ProxyLease:
    id: str
    host: str
    port: int
    username: str
    password: str
    country: str = "ZZ"
    group: str = DEFAULT_PROXY_GROUP
    scheme: str = "http"


class MongoProbeStore:
    def __init__(self, manager: MongoManager) -> None:
        self.manager = manager

    @property
    def locks(self) -> Any:
        return self.manager.database["executor_locks"]

    @property
    def proxies(self) -> Any:
        return self.manager.database["proxies"]

    async def _guard(self, awaitable: Any) -> Any:
        self.manager.require_online()
        try:
            return await awaitable
        except DuplicateKeyError:
            raise
        except (PyMongoError, OSError) as exc:
            self.manager.mark_offline(exc)
            raise MongoUnavailableError("MongoDB 当前不可用") from exc

    async def ensure_indexes(self) -> None:
        await self._guard(
            self.locks.create_index(
                [("leaseUntil", ASCENDING)],
                name="executor_locks_expiry",
            )
        )
        index_name = "proxies_probe_rotation"
        index_keys = [
            ("enabled", ASCENDING),
            ("country", ASCENDING),
            ("group", ASCENDING),
            ("status", ASCENDING),
            ("leaseUntil", ASCENDING),
            ("lastSelectedAt", ASCENDING),
            ("createdAt", ASCENDING),
        ]
        indexes = await self._guard(self.proxies.index_information())
        existing = indexes.get(index_name)
        raw_existing_keys = (existing or {}).get("key", [])
        existing_keys = (
            list(raw_existing_keys.items())
            if hasattr(raw_existing_keys, "items")
            else list(raw_existing_keys)
        )
        if existing is not None and existing_keys != index_keys:
            # Older installations used the same name without the country key.
            # Replace only that index definition; resource documents remain intact.
            await self._guard(self.proxies.drop_index(index_name))
            existing = None
        if existing is None:
            await self._guard(
                self.proxies.create_index(index_keys, name=index_name)
            )
        await self.normalize_proxy_cooldowns()

    async def normalize_proxy_cooldowns(self) -> int:
        """Release expired isolation and cap legacy 30-minute/6-hour blocks."""
        now = utc_now()
        cooldown_ms = proxy_cooldown_seconds() * 1000
        capped_registration = await self._guard(
            self.proxies.update_many(
                {"registrationBlockedUntil": {"$gt": now}},
                [
                    {
                        "$set": {
                            "registrationBlockedUntil": {
                                "$min": [
                                    "$registrationBlockedUntil",
                                    {
                                        "$add": [
                                            {
                                                "$ifNull": [
                                                    "$lastRegistrationFailureAt",
                                                    now,
                                                ]
                                            },
                                            cooldown_ms,
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
            )
        )
        capped_health = await self._guard(
            self.proxies.update_many(
                {"quarantineUntil": {"$gt": now}},
                [
                    {
                        "$set": {
                            "quarantineUntil": {
                                "$min": [
                                    "$quarantineUntil",
                                    {
                                        "$add": [
                                            {"$ifNull": ["$lastCheckedAt", now]},
                                            cooldown_ms,
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
            )
        )
        released = await self._guard(
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
        cleared_registration = await self._guard(
            self.proxies.update_many(
                {"registrationBlockedUntil": {"$lte": now}},
                {"$unset": {"registrationBlockedUntil": ""}},
            )
        )
        return int(
            released.modified_count
            + capped_registration.modified_count
            + capped_health.modified_count
            + cleared_registration.modified_count
        )

    async def clear_expired_probe_leases(self) -> int:
        now = utc_now()
        result = await self._guard(
            self.proxies.update_many(
                {
                    "leaseOwner": {"$regex": "^probe:"},
                    "leaseUntil": {"$lte": now},
                },
                {"$unset": {"leaseOwner": "", "leaseUntil": ""}},
            )
        )
        return int(result.modified_count)

    async def clear_expired_worker_leases(self) -> int:
        result = await self._guard(
            self.proxies.update_many(
                {
                    "leaseOwner": {"$regex": "^run:"},
                    "leaseUntil": {"$lte": utc_now()},
                },
                {"$unset": {"leaseOwner": "", "leaseUntil": ""}},
            )
        )
        return int(result.modified_count)

    async def clear_expired_locks(self) -> int:
        result = await self._guard(
            self.locks.delete_many({"leaseUntil": {"$lte": utc_now()}})
        )
        return int(result.deleted_count)

    async def acquire_probe_lock(
        self,
        owner: str,
        *,
        lease_seconds: int = 90,
    ) -> bool:
        now = utc_now()
        try:
            document = await self._guard(
                self.locks.find_one_and_update(
                    {
                        "_id": PROBE_LOCK_ID,
                        "$or": [
                            {"leaseUntil": {"$lte": now}},
                            {"leaseUntil": {"$exists": False}},
                            {"owner": owner},
                        ],
                    },
                    {
                        "$set": {
                            "owner": owner,
                            "acquiredAt": now,
                            "heartbeatAt": now,
                            "leaseUntil": now + timedelta(seconds=lease_seconds),
                        }
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            )
        except DuplicateKeyError:
            return False
        return document is not None and document.get("owner") == owner

    async def heartbeat_probe_lock(
        self,
        owner: str,
        *,
        lease_seconds: int = 90,
    ) -> bool:
        now = utc_now()
        result = await self._guard(
            self.locks.update_one(
                {"_id": PROBE_LOCK_ID, "owner": owner},
                {
                    "$set": {
                        "heartbeatAt": now,
                        "leaseUntil": now + timedelta(seconds=lease_seconds),
                    }
                },
            )
        )
        return bool(result.modified_count or result.matched_count)

    async def release_probe_lock(self, owner: str) -> None:
        await self._guard(
            self.locks.delete_one({"_id": PROBE_LOCK_ID, "owner": owner})
        )

    async def acquire_workspace(
        self,
        workspace_id: int,
        owner: str,
        *,
        lease_seconds: int = 180,
    ) -> bool:
        now = utc_now()
        lock_id = f"{WORKSPACE_LOCK_PREFIX}{workspace_id}"
        try:
            document = await self._guard(
                self.locks.find_one_and_update(
                    {
                        "_id": lock_id,
                        "$or": [
                            {"leaseUntil": {"$lte": now}},
                            {"leaseUntil": {"$exists": False}},
                            {"owner": owner},
                        ],
                    },
                    {
                        "$set": {
                            "kind": "browser_probe_workspace",
                            "workspaceId": workspace_id,
                            "owner": owner,
                            "acquiredAt": now,
                            "heartbeatAt": now,
                            "leaseUntil": now + timedelta(seconds=lease_seconds),
                        }
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            )
        except DuplicateKeyError:
            return False
        return document is not None and document.get("owner") == owner

    async def heartbeat_workspace(
        self,
        workspace_id: int,
        owner: str,
        *,
        lease_seconds: int = 180,
    ) -> bool:
        now = utc_now()
        result = await self._guard(
            self.locks.update_one(
                {
                    "_id": f"{WORKSPACE_LOCK_PREFIX}{workspace_id}",
                    "owner": owner,
                },
                {
                    "$set": {
                        "heartbeatAt": now,
                        "leaseUntil": now + timedelta(seconds=lease_seconds),
                    }
                },
            )
        )
        return bool(result.modified_count or result.matched_count)

    async def release_workspace(self, workspace_id: int, owner: str) -> None:
        await self._guard(
            self.locks.delete_one(
                {
                    "_id": f"{WORKSPACE_LOCK_PREFIX}{workspace_id}",
                    "owner": owner,
                }
            )
        )

    async def release_workspace_owner(self, owner: str) -> int:
        result = await self._guard(
            self.locks.delete_many(
                {
                    "kind": "browser_probe_workspace",
                    "owner": owner,
                }
            )
        )
        return int(result.deleted_count)

    @staticmethod
    def _available_proxy_filter(
        now: datetime,
        excluded_ids: set[str] | None = None,
        country: str | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "enabled": True,
            "$and": [
                {
                    "$or": [
                        {"status": {"$ne": "quarantined"}},
                        {"quarantineUntil": {"$exists": False}},
                        {"quarantineUntil": None},
                        {"quarantineUntil": {"$lte": now}},
                    ]
                },
                {
                    "$or": [
                        {"registrationBlockedUntil": {"$exists": False}},
                        {"registrationBlockedUntil": None},
                        {"registrationBlockedUntil": {"$lte": now}},
                    ]
                },
            ],
        }
        if excluded_ids:
            query["_id"] = {"$nin": sorted(excluded_ids)}
        filters: list[dict[str, Any]] = [query]
        normalized = str(country or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", normalized):
            escaped = re.escape(normalized)
            inferred_pattern = {
                "$regex": rf"(?:^|[-_.])(?:region|country|res|area|dc|res_sc)-{escaped}(?:[-_.:]|$)",
                "$options": "i",
            }
            filters.append(
                {
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
            )
        normalized_group = " ".join(str(group or "").split())
        if normalized_group:
            if normalized_group == DEFAULT_PROXY_GROUP:
                filters.append(
                    {
                        "$or": [
                            {"group": DEFAULT_PROXY_GROUP},
                            {"group": {"$exists": False}},
                            {"group": None},
                            {"group": ""},
                        ]
                    }
                )
            else:
                filters.append({"group": normalized_group})
        return query if len(filters) == 1 else {"$and": filters}

    async def count_eligible_proxies(
        self, country: str | None = None, group: str | None = None
    ) -> int:
        if group == LOCAL_PROXY_GROUP:
            return 10000
        return int(
            await self._guard(
                self.proxies.count_documents(
                    self._available_proxy_filter(utc_now(), country=country, group=group)
                )
            )
        )

    async def global_promotion_candidates(self, *, limit: int = 5) -> list[ProxyLease]:
        documents = await self._guard(
            self.proxies.find(self._available_proxy_filter(utc_now()))
            .sort([("lastSelectedAt", ASCENDING), ("createdAt", ASCENDING), ("_id", ASCENDING)])
            .limit(200)
            .to_list(length=200)
        )
        selected: list[dict[str, Any]] = []
        used_countries: set[str] = set()
        for document in documents:
            country = str(document.get("country") or "ZZ").upper()
            if country != "ZZ" and country not in used_countries:
                selected.append(document)
                used_countries.add(country)
                if len(selected) >= limit:
                    break
        selected_ids = {str(item["_id"]) for item in selected}
        for document in documents:
            if len(selected) >= limit:
                break
            if str(document["_id"]) not in selected_ids:
                selected.append(document)
                selected_ids.add(str(document["_id"]))
        return [
            ProxyLease(
                id=str(item["_id"]), host=str(item["host"]), port=int(item["port"]),
                username=str(item.get("username") or ""), password=str(item.get("password") or ""),
                country=str(item.get("country") or "ZZ").upper(),
                group=str(item.get("group") or DEFAULT_PROXY_GROUP),
                scheme=str(item.get("scheme") or "http").lower(),
            )
            for item in selected
        ]

    async def all_eligible_proxy_candidates(self) -> list[ProxyLease]:
        documents = await self._guard(
            self.proxies.find(self._available_proxy_filter(utc_now()))
            .sort([("country", ASCENDING), ("createdAt", ASCENDING), ("_id", ASCENDING)])
            .to_list(length=None)
        )
        return [
            ProxyLease(
                id=str(item["_id"]), host=str(item["host"]), port=int(item["port"]),
                username=str(item.get("username") or ""), password=str(item.get("password") or ""),
                country=str(item.get("country") or "ZZ").upper(),
                group=str(item.get("group") or DEFAULT_PROXY_GROUP),
                scheme=str(item.get("scheme") or "http").lower(),
            )
            for item in documents
        ]

    async def acquire_proxy(
        self,
        owner: str,
        *,
        excluded_ids: set[str] | None = None,
        lease_seconds: int = 180,
        country: str | None = None,
        group: str | None = None,
    ) -> ProxyLease | None:
        if group == LOCAL_PROXY_GROUP:
            return ProxyLease(
                id=f"{LOCAL_PROXY_ID_PREFIX}{owner}",
                host="127.0.0.1",
                port=7890,
                username="",
                password="",
                country=str(country or "ZZ").upper(),
                group=LOCAL_PROXY_GROUP,
                scheme="http",
            )
        now = utc_now()
        document = await self._guard(
            self.proxies.find_one_and_update(
                self._available_proxy_filter(now, excluded_ids, country, group),
                {
                    "$set": {
                        "lastSelectedAt": now,
                    },
                    "$addToSet": {"activeLeaseOwners": owner},
                    "$inc": {"activeLeaseCount": 1},
                },
                sort=[
                    ("activeLeaseCount", ASCENDING),
                    ("lastSelectedAt", ASCENDING),
                    ("createdAt", ASCENDING),
                    ("_id", ASCENDING),
                ],
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is None:
            return None
        return ProxyLease(
            id=str(document["_id"]),
            host=str(document["host"]),
            port=int(document["port"]),
            username=str(document.get("username") or ""),
            password=str(document.get("password") or ""),
            country=(
                str(country).upper()
                if str(document.get("country") or "").upper() in {"", "ZZ"}
                and country
                else str(document.get("country") or "ZZ").upper()
            ),
            group=str(document.get("group") or DEFAULT_PROXY_GROUP),
            scheme=str(document.get("scheme") or "http").lower(),
        )

    async def release_proxy(self, proxy_id: str, owner: str) -> None:
        if proxy_id.startswith(LOCAL_PROXY_ID_PREFIX):
            return
        await self._guard(
            self.proxies.update_one(
                {"_id": proxy_id, "activeLeaseOwners": owner},
                {
                    "$pull": {"activeLeaseOwners": owner},
                    "$inc": {"activeLeaseCount": -1},
                    "$unset": {"leaseOwner": "", "leaseUntil": ""},
                },
            )
        )

    async def acquire_proxy_by_id(
        self,
        proxy_id: str,
        owner: str,
        *,
        lease_seconds: int = 180,
        country: str | None = None,
    ) -> ProxyLease | None:
        # `local7890` is a UI sentinel, not a MongoDB proxy document. Treat it
        # exactly like the launch-page local proxy group so every account-pool
        # checker can use the same selection.
        if proxy_id == "local7890" or proxy_id.startswith(LOCAL_PROXY_ID_PREFIX):
            return ProxyLease(
                id=f"{LOCAL_PROXY_ID_PREFIX}{owner}",
                host="127.0.0.1",
                port=7890,
                username="",
                password="",
                country=str(country or "ZZ").upper(),
                group=LOCAL_PROXY_GROUP,
                scheme="http",
            )
        now = utc_now()
        document = await self._guard(
            self.proxies.find_one_and_update(
                {"$and": [self._available_proxy_filter(now), {"_id": proxy_id}]},
                {
                    "$set": {"lastSelectedAt": now},
                    "$addToSet": {"activeLeaseOwners": owner},
                    "$inc": {"activeLeaseCount": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
        )
        if document is None:
            return None
        return ProxyLease(
            id=str(document["_id"]),
            host=str(document["host"]),
            port=int(document["port"]),
            username=str(document.get("username") or ""),
            password=str(document.get("password") or ""),
            country=str(document.get("country") or "ZZ").upper(),
            group=str(document.get("group") or DEFAULT_PROXY_GROUP),
            scheme=str(document.get("scheme") or "http").lower(),
        )

    async def heartbeat_proxy(
        self,
        proxy_id: str,
        owner: str,
        *,
        lease_seconds: int = 180,
    ) -> bool:
        if proxy_id.startswith(LOCAL_PROXY_ID_PREFIX):
            return True
        now = utc_now()
        result = await self._guard(
            self.proxies.update_one(
                {"_id": proxy_id, "activeLeaseOwners": owner},
                {"$set": {"lastLeaseHeartbeatAt": now}},
            )
        )
        return bool(result.modified_count or result.matched_count)

    async def release_proxy_owner(self, owner: str) -> int:
        result = await self._guard(
            self.proxies.update_many(
                {"$or": [{"activeLeaseOwners": owner}, {"leaseOwner": owner}]},
                {
                    "$pull": {"activeLeaseOwners": owner},
                    "$inc": {"activeLeaseCount": -1},
                    "$unset": {"leaseOwner": "", "leaseUntil": ""},
                },
            )
        )
        return int(result.modified_count)

    async def record_proxy_success(self, proxy_id: str, latency_ms: int) -> None:
        if proxy_id.startswith(LOCAL_PROXY_ID_PREFIX):
            return
        await self._guard(
            self.proxies.update_one(
                {"_id": proxy_id},
                {
                    "$set": {
                        "status": "available",
                        "latencyMs": max(0, latency_ms),
                        "lastCheckedAt": utc_now(),
                        "consecutiveFailures": 0,
                    },
                    "$unset": {"quarantineUntil": ""},
                },
            )
        )

    async def record_proxy_registration_rejection(
        self,
        proxy_id: str,
        *,
        code: str,
        observed_country: str | None = None,
        cooldown_seconds: int = MAX_PROXY_COOLDOWN_SECONDS,
    ) -> None:
        if proxy_id.startswith(LOCAL_PROXY_ID_PREFIX):
            return
        now = utc_now()
        effective_cooldown = min(
            proxy_cooldown_seconds(), max(60, int(cooldown_seconds))
        )
        changes: dict[str, Any] = {
            "registrationBlockedUntil": now
            + timedelta(seconds=effective_cooldown),
            "lastRegistrationFailureAt": now,
            "lastRegistrationFailureCode": code,
        }
        normalized_country = str(observed_country or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", normalized_country):
            changes["country"] = normalized_country
        await self._guard(
            self.proxies.update_one(
                {"_id": proxy_id},
                {
                    "$set": changes,
                    "$inc": {"registrationFailureCount": 1},
                },
            )
        )

    async def record_proxy_registration_success(self, proxy_id: str) -> None:
        if proxy_id.startswith(LOCAL_PROXY_ID_PREFIX):
            return
        await self._guard(
            self.proxies.update_one(
                {"_id": proxy_id},
                {
                    "$set": {"lastRegistrationSuccessAt": utc_now()},
                    "$unset": {
                        "registrationBlockedUntil": "",
                        "lastRegistrationFailureCode": "",
                    },
                },
            )
        )
