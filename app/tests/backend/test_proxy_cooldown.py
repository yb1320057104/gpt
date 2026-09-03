from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.probe_store import MongoProbeStore, proxy_cooldown_seconds
from backend.resource_service import usable_proxy_status_filter


class _Collection:
    def __init__(self) -> None:
        self.update: dict | None = None

    async def update_one(self, _query: dict, update: dict):
        self.update = update
        return SimpleNamespace(modified_count=1, matched_count=1)


class _Store(MongoProbeStore):
    def __init__(self, collection: _Collection) -> None:
        self._collection = collection
        self.manager = SimpleNamespace(require_online=lambda: None, mark_offline=lambda _exc: None)

    @property
    def proxies(self):
        return self._collection


def test_proxy_cooldown_is_capped_at_ten_minutes(monkeypatch) -> None:
    monkeypatch.setenv("AUTOREGISTER_PROXY_COOLDOWN_SECONDS", "21600")
    assert proxy_cooldown_seconds() == 600

    collection = _Collection()
    before = datetime.now(timezone.utc)
    asyncio.run(
        _Store(collection).record_proxy_registration_rejection(
            "proxy-1", code="target_challenge_detected", cooldown_seconds=21_600
        )
    )
    assert collection.update is not None
    blocked_until = collection.update["$set"]["registrationBlockedUntil"]
    assert before + timedelta(seconds=590) <= blocked_until
    assert blocked_until <= datetime.now(timezone.utc) + timedelta(seconds=600)


def test_expired_quarantine_is_eligible_across_consumers() -> None:
    now = datetime.now(timezone.utc)
    probe_filter = MongoProbeStore._available_proxy_filter(now)
    resource_filter = usable_proxy_status_filter(now)
    for query in (probe_filter, resource_filter):
        rendered = repr(query)
        assert "quarantineUntil" in rendered
        assert "$lte" in rendered
