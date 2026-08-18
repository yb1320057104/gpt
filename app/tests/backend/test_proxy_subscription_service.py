from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.proxy_subscription_service import (
    ProxyProbeResult,
    ProxySubscriptionError,
    ProxySubscriptionService,
)
from backend.resource_models import ProxySubscriptionImportInput
from backend.resource_service import ResourceService


class ImportStore:
    def __init__(self) -> None:
        self.proxies: list[tuple[str, int, str, str, str, str | None, str | None]] = []

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
        value = (host, port, username, password, scheme, country, group)
        if value in self.proxies:
            return False
        self.proxies.append(value)
        return True


class HealthStore:
    def __init__(self) -> None:
        self.records: list[tuple[str, bool, int | None, str | None]] = []

    async def proxy_documents_for_test(self, country=None, group=None, limit=None):
        documents = [
            {"_id": "good", "scheme": "http", "host": "good.test", "port": 8000},
            {"_id": "bad", "scheme": "socks5", "host": "bad.test", "port": 9000},
        ]
        return documents[:limit] if limit else documents

    async def record_proxy_test(
        self, proxy_id, *, available, latency_ms=None, country=None
    ):
        self.records.append((proxy_id, available, latency_ms, country))


def test_stored_proxy_health_updates_success_and_failure() -> None:
    store = HealthStore()

    async def probe(proxy_url: str, _timeout: float) -> ProxyProbeResult:
        if "bad.test" in proxy_url:
            raise httpx.ConnectError("offline")
        return ProxyProbeResult(proxy_url, "JP", 88)

    service = ProxySubscriptionService(
        ResourceService(store),  # type: ignore[arg-type]
        probe_proxy=probe,
    )
    result = asyncio.run(service.test_stored_proxies(limit=2))

    assert result.tested == 2
    assert result.available == 1
    assert result.failed == 1
    assert result.averageLatencyMs == 88
    assert store.records == [
        ("good", True, 88, "JP"),
        ("bad", False, None, None),
    ]


def service_with(handler: httpx.MockTransport) -> tuple[ProxySubscriptionService, ImportStore]:
    store = ImportStore()

    def factory(**kwargs):
        return httpx.AsyncClient(transport=handler, **kwargs)

    async def probe(proxy_url: str, _timeout: float) -> ProxyProbeResult:
        country = "JP" if "24000" in proxy_url or "autoregister-1" in proxy_url else "US"
        return ProxyProbeResult(proxy_url, country, 120 if country == "JP" else 240)

    return (
        ProxySubscriptionService(
            ResourceService(store),  # type: ignore[arg-type]
            client_factory=factory,
            probe_proxy=probe,
        ),
        store,
    )


def test_easy_proxies_subscription_exports_into_existing_importer() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/auth":
            return httpx.Response(200, json={"token": "session"})
        if request.url.path == "/api/subscription/config" and request.method == "GET":
            return httpx.Response(200, json={"subscriptions": [], "interval": "24h0m0s"})
        if request.url.path == "/api/subscription/config":
            body = json.loads(request.content)
            assert body["subscriptions"] == ["https://example.test/sub"]
            return httpx.Response(200, json=body)
        if request.url.path == "/api/import/parse":
            return httpx.Response(200, json={
                "import_id": "import-1",
                "nodes": [{"id": "node-1"}, {"id": "node-2"}],
            })
        if request.url.path == "/api/import/import-1/commit":
            return httpx.Response(200, json={"job_id": "job-1"})
        if request.url.path == "/api/import/jobs/job-1":
            return httpx.Response(200, json={"status": "completed", "total": 2})
        if request.url.path == "/api/export":
            return httpx.Response(
                200,
                text="# nodes\nhttp://127.0.0.1:24000\nhttp://127.0.0.1:24001\n",
            )
        raise AssertionError(request.url)

    service, store = service_with(httpx.MockTransport(handler))
    result = asyncio.run(
        service.import_subscription(
            ProxySubscriptionImportInput(
                provider="easy-proxies",
                subscriptionUrl="https://example.test/sub",
                managerUrl="http://127.0.0.1:9091",
                adminToken="secret",
                group="Easy",
            )
        )
    )

    assert result.nodeCount == 2
    assert result.generatedProxyCount == 2
    assert result.importResult.imported == 2
    assert [item[1] for item in store.proxies] == [24000, 24001]
    assert all(item[2:4] == ("autoregister", "local") for item in store.proxies)
    assert [item[5] for item in store.proxies] == ["JP", "US"]
    assert all(item[6] == "Easy" for item in store.proxies)
    assert result.usableProxyCount == 2
    assert result.rejectedProxyCount == 0
    assert result.countries == [
        {"country": "JP", "count": 1, "averageLatencyMs": 120},
        {"country": "US", "count": 1, "averageLatencyMs": 240},
    ]
    assert ("POST", "/api/import/parse") in requests
    assert ("POST", "/api/import/import-1/commit") in requests


def test_resin_subscription_generates_independent_pool_identities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer admin-secret"
        if request.url.path == "/api/v1/subscriptions" and request.method == "GET":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/api/v1/subscriptions" and request.method == "POST":
            return httpx.Response(201, json={"id": "sub-id", "node_count": 0})
        if request.url.path.endswith("/actions/refresh"):
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/subscriptions/sub-id":
            return httpx.Response(200, json={"id": "sub-id", "healthy_node_count": 3})
        raise AssertionError(request.url)

    service, store = service_with(httpx.MockTransport(handler))
    result = asyncio.run(
        service.import_subscription(
            ProxySubscriptionImportInput(
                provider="resin",
                subscriptionUrl="https://example.test/resin",
                managerUrl="http://localhost:2260",
                adminToken="admin-secret",
                proxyToken="proxy-secret",
                group="Resin",
            )
        )
    )

    assert result.nodeCount == 3
    assert result.importResult.imported == 3
    assert [item[2] for item in store.proxies] == [
        "Default.autoregister-1",
        "Default.autoregister-2",
        "Default.autoregister-3",
    ]
    assert all(item[3] == "proxy-secret" for item in store.proxies)


def test_subscription_manager_rejects_non_loopback_addresses() -> None:
    service, _store = service_with(httpx.MockTransport(lambda _request: httpx.Response(500)))

    with pytest.raises(ProxySubscriptionError) as exc_info:
        asyncio.run(
            service.import_subscription(
                ProxySubscriptionImportInput(
                provider="easy-proxies",
                subscriptionUrl="https://example.test/sub",
                managerUrl="http://192.168.1.10:9091",
                )
            )
        )

    assert exc_info.value.code == "proxy_subscription_invalid_manager"
