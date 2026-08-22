from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.proxy_health_scheduler import ProxyHealthScheduler


def test_health_scheduler_never_deletes_failed_proxy_records(monkeypatch) -> None:
    monkeypatch.setenv("AUTOREGISTER_PROXY_PURGE_QUARANTINED", "true")
    store = SimpleNamespace(
        release_expired_proxy_quarantines=AsyncMock(return_value=3),
        delete_repeatedly_failed_proxies=AsyncMock(
            side_effect=AssertionError("automatic proxy deletion must stay disabled")
        )
    )
    service = SimpleNamespace(
        resources=SimpleNamespace(store=store),
        test_stored_proxies=AsyncMock(
            return_value=SimpleNamespace(tested=12, available=4, failed=8)
        ),
    )

    result = asyncio.run(ProxyHealthScheduler(service).run_once())

    assert result == {"tested": 12, "available": 4, "failed": 8, "deleted": 0}
    store.release_expired_proxy_quarantines.assert_awaited_once_with()
    store.delete_repeatedly_failed_proxies.assert_not_awaited()
