from __future__ import annotations

import asyncio
import json

import httpx
from pydantic import SecretStr

from backend.ant_browser_client import AntBrowserClient
from backend.probe_store import ProxyLease


def test_ant_profile_lifecycle_uses_direct_debug_url() -> None:
    asyncio.run(_exercise_ant_profile_lifecycle())


def test_ant_fingerprint_tracks_proxy_country() -> None:
    args = AntBrowserClient._fingerprint_args(
        ProxyLease(
            id="proxy-gb",
            host="proxy.test",
            port=8080,
            username="",
            password="",
            country="GB",
            group="default",
            scheme="http",
        )
    )

    assert "--lang=en-GB" in args
    assert "--accept-lang=en-GB,en" in args
    assert "--timezone=Europe/London" in args
    assert "--window-size=1920,1080" in args


async def _exercise_ant_profile_lifecycle() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/profiles" and request.method == "POST":
            body = json.loads(request.content)
            assert body["profile"]["proxyConfig"] == "http://user:p%40ss@proxy.test:8080"
            fingerprint_args = body["profile"]["fingerprintArgs"]
            assert "--lang=en-US" in fingerprint_args
            assert "--timezone=America/New_York" in fingerprint_args
            assert "--window-size=1920,1080" in fingerprint_args
            assert any(value.startswith("--fingerprint=") for value in fingerprint_args)
            return httpx.Response(201, json={"ok": True, "profileId": "profile-1"})
        if request.url.path == "/api/runtime/session":
            body = json.loads(request.content)
            assert "--window-size=1920,1080" in body["launchArgs"]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "profileId": "profile-1",
                    "debugReady": True,
                    "directDebugUrl": "http://127.0.0.1:9333",
                    "pid": 42,
                },
            )
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"ok": True, "stopped": True})
        if request.url.path == "/api/profiles/profile-1" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True, "deleted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with AntBrowserClient(
        19876,
        SecretStr("secret"),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.health()
        profile_id = await client.create_browser(
            0,
            "probe-12345678",
            ProxyLease(
                id="proxy-1",
                host="proxy.test",
                port=8080,
                username="user",
                password="p@ss",
                country="US",
                group="default",
                scheme="http",
            ),
        )
        opened = await client.open_browser(0, profile_id, headless=False)
        assert opened.ws == "http://127.0.0.1:9333"
        assert opened.pid == 42
        await client.close_browser(profile_id)
        await client.delete_browser(0, profile_id)

    assert all(request.headers.get("X-Ant-Api-Key") == "secret" for request in requests)
