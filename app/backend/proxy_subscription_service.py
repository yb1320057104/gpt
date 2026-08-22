from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .resource_models import (
    ImportResult,
    ProxySubscriptionImportInput,
    ProxySubscriptionImportResult,
    ProxyTestResult,
)
from .resource_service import ResourceService


class ProxySubscriptionError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


ClientFactory = Callable[..., httpx.AsyncClient]


@dataclass(frozen=True)
class ProxyProbeResult:
    proxy_url: str
    country: str
    latency_ms: int


ProbeCallable = Callable[[str, float], Awaitable[ProxyProbeResult]]


class ProxySubscriptionService:
    """Adapts local subscription managers to the existing proxy importer."""

    def __init__(
        self,
        resources: ResourceService,
        *,
        client_factory: ClientFactory = httpx.AsyncClient,
        probe_proxy: ProbeCallable | None = None,
    ) -> None:
        self.resources = resources
        self.client_factory = client_factory
        self.probe_proxy = probe_proxy or self._probe_proxy

    async def import_subscription(
        self, payload: ProxySubscriptionImportInput
    ) -> ProxySubscriptionImportResult:
        manager_url = self._local_manager_url(payload.managerUrl)
        try:
            if payload.provider == "easy-proxies":
                proxy_lines, node_count = await self._easy_proxies(payload, manager_url)
            else:
                proxy_lines, node_count = await self._resin(payload, manager_url)
        except ProxySubscriptionError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProxySubscriptionError(
                "proxy_subscription_manager_failed",
                f"{payload.provider} 订阅导入失败：{self._safe_error(exc)}",
            ) from exc

        # Imports still need a country so they can be placed in the existing
        # country-partitioned pool. A successful connectivity-only result is
        # useful for health checks, but not sufficient for classification.
        probes = [
            item
            for item in await self._probe_all(
                proxy_lines, payload.probeTimeoutSeconds
            )
            if item.country
        ]
        if not probes:
            raise ProxySubscriptionError(
                "proxy_subscription_no_usable_proxies",
                "订阅节点检测完成，但没有可用且能识别出口国家的代理",
                status_code=422,
            )

        grouped: dict[str, list[ProxyProbeResult]] = defaultdict(list)
        for probe in probes:
            grouped[probe.country].append(probe)

        total = imported_count = duplicate_count = error_count = 0
        countries: list[dict[str, int | str]] = []
        for country in sorted(grouped):
            country_probes = grouped[country]
            result = await self.resources.import_proxies(
                "\n".join(item.proxy_url for item in country_probes),
                country,
                payload.group,
            )
            total += result.total
            imported_count += result.imported
            duplicate_count += result.duplicateCount
            error_count += result.errorCount
            countries.append({
                "country": country,
                "count": len(country_probes),
                "averageLatencyMs": round(
                    sum(item.latency_ms for item in country_probes) / len(country_probes)
                ),
            })
        imported = ImportResult(
            total=total,
            imported=imported_count,
            duplicateCount=duplicate_count,
            errorCount=error_count,
        )
        return ProxySubscriptionImportResult(
            provider=payload.provider,
            subscriptionName=payload.name,
            nodeCount=node_count,
            generatedProxyCount=len(proxy_lines),
            testedProxyCount=len(proxy_lines),
            usableProxyCount=len(probes),
            rejectedProxyCount=len(proxy_lines) - len(probes),
            countries=countries,
            importResult=imported,
        )

    async def _probe_all(
        self, proxy_lines: list[str], timeout_seconds: float
    ) -> list[ProxyProbeResult]:
        semaphore = asyncio.Semaphore(12)

        async def probe_one(proxy_url: str) -> ProxyProbeResult | None:
            async with semaphore:
                try:
                    return await self.probe_proxy(proxy_url, timeout_seconds)
                except (httpx.HTTPError, ValueError, TypeError):
                    return None

        results = await asyncio.gather(*(probe_one(line) for line in proxy_lines))
        return [item for item in results if item is not None]

    async def test_stored_proxies(
        self,
        *,
        country: str | None = None,
        group: str | None = None,
        timeout_seconds: float = 12,
        limit: int | None = None,
    ) -> ProxyTestResult:
        documents = await self.resources.store.proxy_documents_for_test(
            country, group, limit
        )
        # Subscription managers such as Resin multiplex many logical nodes on
        # one local port. A large burst can overload that single entry point
        # and incorrectly quarantine otherwise healthy nodes.
        concurrency = max(
            1, min(12, int(os.getenv("AUTOREGISTER_PROXY_CHECK_CONCURRENCY", "4")))
        )
        attempts = max(
            1, min(5, int(os.getenv("AUTOREGISTER_PROXY_CHECK_ATTEMPTS", "3")))
        )
        semaphore = asyncio.Semaphore(concurrency)

        async def test_one(document: dict[str, object]) -> ProxyProbeResult | None:
            scheme = str(document.get("scheme") or "http").lower()
            host = str(document.get("host") or "")
            port = int(document.get("port") or 0)
            username = quote(str(document.get("username") or ""), safe="")
            password = quote(str(document.get("password") or ""), safe="")
            auth = f"{username}:{password}@" if username or password else ""
            proxy_url = f"{scheme}://{auth}{host}:{port}"
            async with semaphore:
                result: ProxyProbeResult | None = None
                for attempt in range(attempts):
                    try:
                        result = await self.probe_proxy(proxy_url, timeout_seconds)
                        break
                    except (httpx.HTTPError, ValueError, TypeError, OSError):
                        if attempt + 1 < attempts:
                            await asyncio.sleep(0.25 * (attempt + 1))
                if result is None:
                    await self.resources.store.record_proxy_test(
                        str(document["_id"]), available=False
                    )
                    return None
                await self.resources.store.record_proxy_test(
                    str(document["_id"]),
                    available=True,
                    latency_ms=result.latency_ms,
                    country=result.country,
                )
                return result

        results = await asyncio.gather(*(test_one(document) for document in documents))
        usable = [item for item in results if item is not None]
        grouped: dict[str, list[ProxyProbeResult]] = defaultdict(list)
        for item in usable:
            if item.country:
                grouped[item.country].append(item)
        return ProxyTestResult(
            tested=len(documents),
            available=len(usable),
            failed=len(documents) - len(usable),
            averageLatencyMs=(
                round(sum(item.latency_ms for item in usable) / len(usable))
                if usable else None
            ),
            countries=[
                {"country": code, "count": len(items)}
                for code, items in sorted(grouped.items())
            ],
        )

    @staticmethod
    async def _probe_proxy(proxy_url: str, timeout_seconds: float) -> ProxyProbeResult:
        started = perf_counter()
        timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 8))
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=True,
        ) as client:
            # Match Mihomo/Clash Verge's delay-test semantics: availability is
            # determined by a small connectivity endpoint, not by a geo-IP
            # provider. Geo services routinely rate-limit proxy pools and must
            # not turn a successful proxy test into a false negative.
            health_errors: list[Exception] = []
            health_urls = [
                value.strip()
                for value in os.getenv(
                    "AUTOREGISTER_PROXY_TEST_URLS",
                    "https://www.gstatic.com/generate_204,"
                    "https://cp.cloudflare.com/generate_204",
                ).split(",")
                if value.strip()
            ]
            for url in health_urls:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    break
                except httpx.HTTPError as exc:
                    health_errors.append(exc)
            else:
                if health_errors:
                    raise health_errors[-1]
                raise ValueError("no proxy test URL configured")

            latency_ms = max(1, round((perf_counter() - started) * 1000))
            country = ""
            # Country detection is best-effort metadata enrichment after the
            # connectivity test has already succeeded.
            for url, params, response_type in (
                ("https://chatgpt.com/cdn-cgi/trace", None, "trace"),
                ("https://api.country.is/", None, "country-is"),
                (
                    "https://ipwho.is/",
                    {"fields": "success,ip,country_code"},
                    "ipwho",
                ),
            ):
                try:
                    response = await client.get(
                        url, params=params, timeout=min(timeout_seconds, 3)
                    )
                    response.raise_for_status()
                    if response_type == "trace":
                        match = re.search(r"(?m)^loc=([A-Za-z]{2})\s*$", response.text)
                        country = match.group(1).upper() if match else ""
                    else:
                        body = response.json()
                        if response_type == "country-is":
                            country = str(body.get("country") or "").strip().upper()
                        elif body.get("success") is not False:
                            country = str(body.get("country_code") or "").strip().upper()
                    if re.fullmatch(r"[A-Z]{2}", country) and country != "ZZ":
                        break
                except (httpx.HTTPError, ValueError, TypeError):
                    continue
            if not re.fullmatch(r"[A-Z]{2}", country) or country == "ZZ":
                country = ""
        return ProxyProbeResult(
            proxy_url=proxy_url,
            country=country,
            latency_ms=latency_ms,
        )

    async def _easy_proxies(
        self, payload: ProxySubscriptionImportInput, manager_url: str
    ) -> tuple[list[str], int]:
        timeout = httpx.Timeout(120.0, connect=5.0)
        async with self.client_factory(
            base_url=manager_url, timeout=timeout, trust_env=False
        ) as client:
            headers: dict[str, str] = {}
            admin_token = payload.adminToken or os.getenv("AUTOREGISTER_EASY_PROXIES_PASSWORD", "")
            if admin_token:
                login = await client.post(
                    "/api/auth", json={"password": admin_token}
                )
                self._raise_manager_error(login, "Easy Proxies 登录失败")
                token = str(login.json().get("token") or "")
                if token:
                    headers["Authorization"] = f"Bearer {token}"

            current = await client.get("/api/subscription/config", headers=headers)
            self._raise_manager_error(current, "Easy Proxies 配置读取失败")
            body = current.json()
            subscriptions = [
                str(item).strip()
                for item in body.get("subscriptions", [])
                if str(item).strip()
            ]
            if payload.subscriptionUrl not in subscriptions:
                subscriptions.append(payload.subscriptionUrl)
            updated = await client.put(
                "/api/subscription/config",
                headers=headers,
                json={
                    "subscriptions": subscriptions,
                    "enabled": True,
                    "interval": str(body.get("interval") or "24h"),
                    "refresh": True,
                },
            )
            self._raise_manager_error(updated, "Easy Proxies 订阅保存失败")

            parsed = await client.post(
                "/api/import/parse",
                headers=headers,
                json={
                    "mode": "url",
                    "url": payload.subscriptionUrl,
                    "tag_prefix": payload.name,
                },
            )
            self._raise_manager_error(parsed, "Easy Proxies 订阅解析失败")
            parsed_body = parsed.json()
            import_id = str(parsed_body.get("import_id") or "")
            node_ids = [
                str(item.get("id") or "")
                for item in parsed_body.get("nodes", [])
                if isinstance(item, dict) and str(item.get("id") or "")
            ]
            if not import_id or not node_ids:
                raise ProxySubscriptionError(
                    "proxy_subscription_empty",
                    "Easy Proxies 没有从订阅中解析出节点",
                    status_code=422,
                )
            committed = await client.post(
                f"/api/import/{quote(import_id, safe='')}/commit",
                headers=headers,
                json={
                    "node_ids": node_ids,
                    "auto_reload": True,
                    "promote_passed": True,
                },
            )
            self._raise_manager_error(committed, "Easy Proxies 节点测速任务启动失败")
            job_id = str(committed.json().get("job_id") or "")
            if not job_id:
                raise ProxySubscriptionError(
                    "proxy_subscription_invalid_response",
                    "Easy Proxies 未返回测速任务 ID",
                )
            job = await self._wait_easy_job(client, headers, job_id)
            node_count = int(job.get("total") or len(node_ids))

            exported = await client.get("/api/export?scheme=http", headers=headers)
            self._raise_manager_error(exported, "Easy Proxies 代理导出失败")
            lines = [self._ensure_http_credentials(line) for line in self._proxy_lines(exported.text)]
            if not lines:
                raise ProxySubscriptionError(
                    "proxy_subscription_empty",
                    "Easy Proxies 没有导出健康代理，请先检查订阅节点测速结果",
                    status_code=422,
                )
            return lines, node_count or len(lines)

    async def _wait_easy_job(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        job_id: str,
    ) -> dict:
        for _ in range(180):
            response = await client.get(
                f"/api/import/jobs/{quote(job_id, safe='')}", headers=headers
            )
            self._raise_manager_error(response, "Easy Proxies 测速状态读取失败")
            body = response.json()
            state = str(body.get("status") or "").lower()
            if state == "completed":
                return body
            if state in {"failed", "canceled"}:
                detail = str(body.get("error") or body.get("detail") or "测速失败")
                raise ProxySubscriptionError(
                    "proxy_subscription_probe_failed",
                    f"Easy Proxies 节点测速失败：{detail[:240]}",
                    status_code=422,
                )
            await asyncio.sleep(1)
        raise ProxySubscriptionError(
            "proxy_subscription_probe_timeout",
            "Easy Proxies 节点测速超时",
            status_code=504,
        )

    async def _resin(
        self, payload: ProxySubscriptionImportInput, manager_url: str
    ) -> tuple[list[str], int]:
        admin_token = payload.adminToken or os.getenv("AUTOREGISTER_RESIN_ADMIN_TOKEN", "")
        proxy_token = payload.proxyToken or os.getenv("AUTOREGISTER_RESIN_PROXY_TOKEN", "")
        if not admin_token or not proxy_token:
            raise ProxySubscriptionError(
                "proxy_subscription_credentials_required",
                "Resin 需要 Admin Token 和 Proxy Token",
                status_code=422,
            )
        headers = {"Authorization": f"Bearer {admin_token}"}
        timeout = httpx.Timeout(120.0, connect=5.0)
        async with self.client_factory(
            base_url=manager_url, timeout=timeout, trust_env=False
        ) as client:
            listing = await client.get("/api/v1/subscriptions", headers=headers)
            self._raise_manager_error(listing, "Resin 订阅列表读取失败")
            items = listing.json().get("items", [])
            subscription = next(
                (
                    item
                    for item in items
                    if str(item.get("url") or "").strip() == payload.subscriptionUrl
                ),
                None,
            )
            if subscription is None:
                created = await client.post(
                    "/api/v1/subscriptions",
                    headers=headers,
                    json={
                        "name": payload.name,
                        "url": payload.subscriptionUrl,
                        "enabled": True,
                        "incremental_alive_nodes": True,
                    },
                )
                self._raise_manager_error(created, "Resin 订阅创建失败")
                subscription = created.json()

            subscription_id = str(subscription.get("id") or "")
            if not subscription_id:
                raise ProxySubscriptionError(
                    "proxy_subscription_invalid_response", "Resin 未返回订阅 ID"
                )
            refreshed = await client.post(
                f"/api/v1/subscriptions/{quote(subscription_id, safe='')}/actions/refresh",
                headers=headers,
            )
            self._raise_manager_error(refreshed, "Resin 订阅刷新失败")
            details = await client.get(
                f"/api/v1/subscriptions/{quote(subscription_id, safe='')}",
                headers=headers,
            )
            self._raise_manager_error(details, "Resin 订阅状态读取失败")
            detail_body = details.json()
            healthy_node_count = int(detail_body.get("healthy_node_count") or 0)
            total_node_count = int(detail_body.get("node_count") or 0)
            if healthy_node_count <= 0 < total_node_count:
                await self._raise_resin_build_error(client, headers)
            node_count = int(
                healthy_node_count or total_node_count
            )
            if node_count <= 0:
                raise ProxySubscriptionError(
                    "proxy_subscription_empty",
                    "Resin 订阅中没有可用节点，请在 Resin 中检查刷新和健康检测结果",
                    status_code=422,
                )

            endpoint = urlsplit(manager_url)
            if not endpoint.hostname or not endpoint.port:
                raise ProxySubscriptionError(
                    "proxy_subscription_invalid_manager", "Resin 管理地址必须包含端口"
                )
            password = quote(proxy_token, safe="")
            proxy_lines = []
            for index in range(1, min(node_count, 500) + 1):
                username = quote(f"Default.autoregister-{index}", safe="")
                auth = f"{username}:{password}@"
                host = endpoint.hostname
                if ":" in host:
                    host = f"[{host}]"
                proxy_lines.append(f"http://{auth}{host}:{endpoint.port}")
            return proxy_lines, node_count

    async def _raise_resin_build_error(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
    ) -> None:
        """Turn Resin's per-node build-tag errors into an actionable message."""
        try:
            response = await client.get("/api/v1/nodes?limit=200", headers=headers)
            self._raise_manager_error(response, "Resin 节点状态读取失败")
            items = response.json().get("items", [])
        except (ProxySubscriptionError, httpx.HTTPError, ValueError, TypeError):
            return

        errors = "\n".join(
            str(item.get("last_error") or "")
            for item in items
            if isinstance(item, dict)
        ).lower()
        required_tags = [
            tag
            for tag in ("with_utls", "with_quic", "with_wireguard", "with_grpc")
            if tag in errors
        ]
        if not required_tags:
            return
        tags = " ".join(required_tags)
        raise ProxySubscriptionError(
            "proxy_subscription_resin_build_features_missing",
            "Resin 可执行文件缺少订阅节点所需协议能力："
            f"{tags}。请运行 app\\scripts\\build-resin.ps1 重新构建后再导入。",
            status_code=422,
        )

    @staticmethod
    def _local_manager_url(value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "http" or not parsed.hostname or parsed.port is None:
            raise ProxySubscriptionError(
                "proxy_subscription_invalid_manager",
                "管理地址必须是带端口的本机 HTTP 地址",
                status_code=422,
            )
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            if parsed.hostname.lower() != "localhost":
                raise ProxySubscriptionError(
                    "proxy_subscription_invalid_manager",
                    "管理地址仅允许 localhost 或回环 IP",
                    status_code=422,
                ) from exc
        else:
            if not address.is_loopback:
                raise ProxySubscriptionError(
                    "proxy_subscription_invalid_manager",
                    "管理地址仅允许 localhost 或回环 IP",
                    status_code=422,
                )
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")

    @staticmethod
    def _proxy_lines(raw: str) -> list[str]:
        return [
            line.strip()
            for line in raw.splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
            and line.strip().lower().startswith(("http://", "https://", "socks5://", "socks5h://"))
        ]

    @staticmethod
    def _ensure_http_credentials(value: str) -> str:
        parsed = urlsplit(value)
        if parsed.username and parsed.password:
            return value
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"autoregister:local@{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    @staticmethod
    def _raise_manager_error(response: httpx.Response, context: str) -> None:
        if response.is_success:
            return
        message = ""
        try:
            body = response.json()
            message = str(body.get("error") or body.get("message") or body.get("detail") or "")
        except ValueError:
            message = response.text[:200]
        raise ProxySubscriptionError(
            "proxy_subscription_manager_failed",
            f"{context}（HTTP {response.status_code}）{f'：{message}' if message else ''}",
            status_code=502,
        )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, httpx.RequestError):
            return "无法连接本机代理管理服务"
        return str(exc)[:300]
