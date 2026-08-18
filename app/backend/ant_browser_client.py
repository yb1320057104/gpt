from __future__ import annotations

import secrets
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from .browser_automation import IP_CHECK_URL
from .probe_store import ProxyLease
from .roxy_client import (
    MANAGED_BROWSER_PREFIX,
    MANAGED_BROWSER_REMARK,
    RoxyApiError,
    RoxyBrowserRecord,
    RoxyConnectionInfo,
    RoxyOpenResult,
    RoxyWorkspace,
)


ANT_BROWSER_WINDOW_SIZE = "1000,1000"
ANT_BROWSER_LAUNCH_ARGS = (
    "--disable-sync",
    "--no-first-run",
    f"--window-size={ANT_BROWSER_WINDOW_SIZE}",
)


class AntBrowserClient:
    """Compatibility adapter exposing Ant Browser through the existing browser lifecycle."""

    def __init__(
        self,
        port: int,
        token: SecretStr,
        *,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        api_key = token.get_secret_value().strip()
        if api_key:
            headers["X-Ant-Api-Key"] = api_key
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> "AntBrowserClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        started = monotonic()
        request_options: dict[str, Any] = {"json": json_body}
        if timeout_seconds is not None:
            request_options["timeout"] = timeout_seconds
        try:
            response = await self._http.request(
                method,
                path,
                **request_options,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise RoxyApiError(
                "Ant Browser API 返回 HTTP 错误",
                operation=operation,
                http_status=status,
                elapsed_ms=int((monotonic() - started) * 1000),
                retryable=status not in (400, 401, 403, 404, 409),
                error_kind="http",
            ) from None
        except httpx.HTTPError:
            raise RoxyApiError(
                "Ant Browser API 请求失败",
                operation=operation,
                elapsed_ms=int((monotonic() - started) * 1000),
                error_kind="transport",
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise RoxyApiError(
                "Ant Browser API 返回了无效 JSON",
                operation=operation,
                elapsed_ms=int((monotonic() - started) * 1000),
                error_kind="invalid_json",
            ) from None
        if not isinstance(payload, dict):
            raise RoxyApiError(
                "Ant Browser API 返回结构错误",
                operation=operation,
                error_kind="invalid_structure",
            )
        if payload.get("ok") is False:
            raise RoxyApiError(
                "Ant Browser API 调用未成功",
                operation=operation,
                elapsed_ms=int((monotonic() - started) * 1000),
                error_kind="api",
            )
        return payload

    async def health(self) -> None:
        await self._request("GET", "/api/health", operation="health")

    async def workspaces(self, *, timeout_seconds: float | None = None) -> list[RoxyWorkspace]:
        await self._request(
            "GET", "/api/profiles", operation="workspace_list", timeout_seconds=timeout_seconds
        )
        return [RoxyWorkspace(id=0, name="Ant Browser")]

    async def browsers(
        self, _workspace_id: int, *, timeout_seconds: float | None = None
    ) -> list[RoxyBrowserRecord]:
        payload = await self._request(
            "GET", "/api/profiles", operation="browser_list", timeout_seconds=timeout_seconds
        )
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        return [
            RoxyBrowserRecord(
                dir_id=str(item.get("profileId") or ""),
                window_name=str(item.get("profileName") or ""),
                window_remark=MANAGED_BROWSER_REMARK if "autoregister" in [
                    str(tag).lower() for tag in item.get("tags", []) if isinstance(tag, str)
                ] else "",
            )
            for item in items
            if isinstance(item, dict) and item.get("profileId")
        ]

    @staticmethod
    def _proxy_url(proxy: ProxyLease) -> str:
        scheme = "socks5" if str(proxy.scheme).lower() == "socks5h" else str(proxy.scheme or "http").lower()
        auth = ""
        if proxy.username:
            auth = quote(proxy.username, safe="")
            if proxy.password:
                auth += ":" + quote(proxy.password, safe="")
            auth += "@"
        return f"{scheme}://{auth}{proxy.host}:{proxy.port}"

    @staticmethod
    def _fingerprint_args(proxy: ProxyLease) -> list[str]:
        country = str(proxy.country or "US").strip().upper()
        locale, timezone = {
            "BR": ("pt-BR", "America/Sao_Paulo"),
            "DE": ("de-DE", "Europe/Berlin"),
            "FR": ("fr-FR", "Europe/Paris"),
            "GB": ("en-GB", "Europe/London"),
            "HK": ("zh-HK", "Asia/Hong_Kong"),
            "JP": ("ja-JP", "Asia/Tokyo"),
            "KR": ("ko-KR", "Asia/Seoul"),
            "PH": ("en-PH", "Asia/Manila"),
            "SG": ("en-SG", "Asia/Singapore"),
            "TR": ("tr-TR", "Europe/Istanbul"),
            "TW": ("zh-TW", "Asia/Taipei"),
            "US": ("en-US", "America/New_York"),
        }.get(country, ("en-US", "America/New_York"))
        language = locale.split("-", 1)[0]
        fingerprint_seed = secrets.randbelow(2_000_000_000) + 1
        hardware_concurrency = secrets.choice((4, 8, 12, 16))
        return [
            f"--fingerprint={fingerprint_seed}",
            "--fingerprint-brand=Chrome",
            "--fingerprint-platform=windows",
            "--fingerprint-platform-version=10.0.0",
            f"--lang={locale}",
            f"--accept-lang={locale},{language}",
            f"--timezone={timezone}",
            f"--window-size={ANT_BROWSER_WINDOW_SIZE}",
            f"--fingerprint-hardware-concurrency={hardware_concurrency}",
            "--disable-non-proxied-udp",
            "--fingerprinting-canvas-image-data-noise",
            "--fingerprinting-client-rects-noise",
        ]

    async def create_browser(self, _workspace_id: int, probe_id: str, proxy: ProxyLease) -> str:
        payload = await self._request(
            "POST",
            "/api/profiles",
            operation="browser_create",
            json_body={
                "profile": {
                    "profileName": f"{MANAGED_BROWSER_PREFIX}{probe_id[:8]}",
                    "proxyConfig": self._proxy_url(proxy),
                    "fingerprintArgs": self._fingerprint_args(proxy),
                    "launchArgs": list(ANT_BROWSER_LAUNCH_ARGS),
                    "tags": ["autoregister"],
                    "keywords": [probe_id],
                },
                "autoLaunch": False,
            },
        )
        profile_id = payload.get("profileId")
        if not isinstance(profile_id, str) or not profile_id:
            raise RoxyApiError(
                "Ant Browser 创建实例响应缺少 profileId",
                operation="browser_create",
                error_kind="contract",
            )
        return profile_id

    async def open_browser(
        self, _workspace_id: int, dir_id: str, *, headless: bool
    ) -> RoxyOpenResult:
        # Ant treats runtime launchArgs as an override of the profile's saved
        # launchArgs.  Always send the complete saved set so opening a profile
        # cannot silently drop session-stability options.
        launch_args = list(ANT_BROWSER_LAUNCH_ARGS)
        if headless:
            launch_args.append("--headless=new")
        payload = await self._request(
            "POST",
            "/api/runtime/session",
            operation="browser_open",
            json_body={
                "profileId": dir_id,
                "launchArgs": launch_args,
                "startUrls": [IP_CHECK_URL],
                "skipDefaultStartUrls": True,
                "timeoutMs": 45000,
            },
            timeout_seconds=50,
        )
        endpoint = payload.get("directDebugUrl")
        if not payload.get("debugReady") or not isinstance(endpoint, str) or not endpoint:
            raise RoxyApiError(
                "Ant Browser 实例未生成可用 CDP 地址",
                operation="browser_open",
                retryable=True,
                error_kind="contract",
            )
        pid = payload.get("pid")
        return RoxyOpenResult(
            ws=endpoint,
            http=endpoint,
            pid=pid if isinstance(pid, int) else None,
        )

    async def connection_info(
        self, dir_ids: list[str] | None = None, *, timeout_seconds: float | None = None
    ) -> list[RoxyConnectionInfo]:
        results: list[RoxyConnectionInfo] = []
        for profile_id in dir_ids or []:
            try:
                payload = await self._request(
                    "GET",
                    f"/api/profiles/{quote(profile_id, safe='')}/status",
                    operation="browser_connection_info",
                    timeout_seconds=timeout_seconds,
                )
            except RoxyApiError as exc:
                if exc.http_status == 404:
                    continue
                raise
            endpoint = payload.get("directDebugUrl")
            if not payload.get("debugReady") or not isinstance(endpoint, str) or not endpoint:
                continue
            pid = payload.get("pid")
            results.append(
                RoxyConnectionInfo(
                    dir_id=profile_id,
                    ws=endpoint,
                    http=endpoint,
                    pid=pid if isinstance(pid, int) else None,
                    window_name=str(payload.get("profileName") or ""),
                    window_remark=MANAGED_BROWSER_REMARK,
                )
            )
        return results

    async def close_browser(self, dir_id: str) -> None:
        await self._request(
            "POST",
            f"/api/profiles/{quote(dir_id, safe='')}/stop",
            operation="browser_close",
        )

    async def delete_browser(self, _workspace_id: int, dir_id: str) -> None:
        await self._request(
            "DELETE",
            f"/api/profiles/{quote(dir_id, safe='')}",
            operation="browser_delete",
        )
