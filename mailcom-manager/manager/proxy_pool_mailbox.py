from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar
from urllib.parse import quote

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError

from .imap_client import ImapMailboxService, MailboxError


T = TypeVar("T")
SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _proxy_url(document: dict[str, Any]) -> str:
    scheme = str(document.get("scheme") or "http").strip().casefold()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        scheme = "http"
    host = str(document.get("host") or "").strip()
    port = int(document.get("port") or 0)
    if not host or not 1 <= port <= 65535:
        raise ValueError("代理地址或端口无效")
    username = str(document.get("username") or "")
    password = str(document.get("password") or "")
    authentication = ""
    if username or password:
        authentication = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{authentication}{address}:{port}"


class MongoProxyPoolMailboxService:
    """Run MailCom IMAP operations through every enabled project proxy in rotation."""

    def __init__(
        self,
        uri: str,
        database: str,
        *,
        host: str | None = None,
        port: int | None = None,
        proxy_url: str | None = None,
        timeout_seconds: float = 15,
        max_message_bytes: int = 2 * 1024 * 1024,
        client: Any | None = None,
        service_factory: Callable[..., ImapMailboxService] = ImapMailboxService,
    ) -> None:
        self._lock = threading.RLock()
        self._client = client or MongoClient(uri, serverSelectionTimeoutMS=10_000)
        self._client.admin.command("ping")
        self._proxies = self._client[database]["proxies"]
        self._service_factory = service_factory
        self.timeout_seconds = timeout_seconds
        self.max_message_bytes = max_message_bytes
        self._manual = self._new_service(host=host, port=port, proxy_url=proxy_url)
        self.host = self._manual.host
        self.port = self._manual.port
        self._last_proxy: dict[str, Any] | None = None

    def _new_service(
        self, *, host: str | None = None, port: int | None = None, proxy_url: str | None = None
    ) -> ImapMailboxService:
        return self._service_factory(
            host=host,
            port=port,
            proxy_url=proxy_url,
            timeout_seconds=self.timeout_seconds,
            max_message_bytes=self.max_message_bytes,
        )

    @property
    def proxy_pool_enabled(self) -> bool:
        return not bool(self._manual.proxy_url)

    @property
    def proxy_url(self) -> str:
        return self._manual.proxy_url

    @property
    def proxy_host(self) -> str:
        return self._manual.proxy_host

    @property
    def proxy_port(self) -> int:
        return self._manual.proxy_port

    @property
    def proxy_scheme(self) -> str:
        return self._manual.proxy_scheme

    @property
    def proxy_username(self) -> str | None:
        return self._manual.proxy_username

    @property
    def proxy_password(self) -> str | None:
        return self._manual.proxy_password

    @property
    def route(self) -> str:
        return "project-pool" if self.proxy_pool_enabled else self._manual.route

    @property
    def proxy_count(self) -> int:
        if not self.proxy_pool_enabled:
            return 1
        try:
            return int(self._proxies.count_documents(self._pool_filter()))
        except (PyMongoError, OSError):
            return 0

    @property
    def last_proxy(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_proxy) if self._last_proxy else None

    @staticmethod
    def _pool_filter() -> dict[str, Any]:
        now = _utc_now()
        return {
            "enabled": True,
            "$or": [
                {"status": {"$ne": "quarantined"}},
                {"quarantineUntil": {"$exists": False}},
                {"quarantineUntil": None},
                {"quarantineUntil": {"$lte": now}},
            ],
        }

    def _candidates(self) -> list[dict[str, Any]]:
        try:
            return list(
                self._proxies.find(self._pool_filter()).sort(
                    [
                        # Reuse exits already proven to reach mail.com before
                        # spending a 15-second IMAP timeout on failing nodes.
                        ("mailcomUsable", DESCENDING),
                        ("mailcomFailureCount", ASCENDING),
                        ("mailcomLastSelectedAt", ASCENDING),
                        ("lastSelectedAt", ASCENDING),
                        ("latencyMs", ASCENDING),
                        ("createdAt", ASCENDING),
                        ("_id", ASCENDING),
                    ]
                )
            )
        except (PyMongoError, OSError) as exc:
            raise MailboxError(
                "proxy_pool_unavailable", "项目 MongoDB 代理池当前不可用", retryable=True
            ) from exc

    def _record_attempt(self, document: dict[str, Any]) -> None:
        now = _utc_now()
        with self._lock:
            self._last_proxy = {
                "id": str(document.get("_id") or ""),
                "host": str(document.get("host") or ""),
                "port": int(document.get("port") or 0),
                "country": str(document.get("country") or "ZZ"),
                "group": str(document.get("group") or "默认分组"),
            }
        try:
            self._proxies.update_one(
                {"_id": document["_id"]}, {"$set": {"mailcomLastSelectedAt": now}}
            )
        except (PyMongoError, OSError):
            pass

    def _record_success(self, document: dict[str, Any], latency_ms: int) -> None:
        try:
            self._proxies.update_one(
                {"_id": document["_id"]},
                {
                    "$set": {
                        "mailcomUsable": True,
                        "mailcomLastSuccessAt": _utc_now(),
                        "mailcomLatencyMs": max(0, latency_ms),
                        "mailcomFailureCount": 0,
                    },
                    "$unset": {"mailcomLastErrorCode": "", "mailcomLastError": ""},
                },
            )
        except (PyMongoError, OSError):
            pass

    def _record_failure(self, document: dict[str, Any], exc: Exception) -> None:
        code = exc.code if isinstance(exc, MailboxError) else "proxy_configuration_invalid"
        message = exc.message if isinstance(exc, MailboxError) else str(exc)
        password = str(document.get("password") or "")
        if password:
            message = message.replace(password, "***")
        try:
            self._proxies.update_one(
                {"_id": document["_id"]},
                {
                    "$set": {
                        "mailcomUsable": False,
                        "mailcomLastFailureAt": _utc_now(),
                        "mailcomLastErrorCode": code,
                        "mailcomLastError": message[:300],
                    },
                    "$inc": {"mailcomFailureCount": 1},
                },
            )
        except (PyMongoError, OSError):
            pass

    def reconfigure(self, *, host: str, port: int, proxy_url: str) -> None:
        configured = self._new_service(host=host, port=port, proxy_url=proxy_url)
        with self._lock:
            self._manual = configured
            self.host = configured.host
            self.port = configured.port

    def _run(self, operation: Callable[[ImapMailboxService], T]) -> T:
        if not self.proxy_pool_enabled:
            return operation(self._manual)

        candidates = self._candidates()
        if not candidates:
            raise MailboxError(
                "proxy_pool_empty", "项目代理池没有已启用且可用的代理", retryable=True
            )

        last_error: MailboxError | None = None
        attempted = 0
        for document in candidates:
            self._record_attempt(document)
            started = time.perf_counter()
            try:
                service = self._new_service(
                    host=self.host, port=self.port, proxy_url=_proxy_url(document)
                )
                attempted += 1
                result = operation(service)
                self._record_success(
                    document, max(1, round((time.perf_counter() - started) * 1000))
                )
                if isinstance(result, dict) and "route" in result:
                    result = dict(result)
                    result.update(
                        {
                            "route": "project-pool",
                            "attempts": attempted,
                            "proxyCountry": str(document.get("country") or "ZZ"),
                            "proxyGroup": str(document.get("group") or "默认分组"),
                        }
                    )
                return result
            except ValueError as exc:
                self._record_failure(document, exc)
                continue
            except MailboxError as exc:
                # Authentication errors prove that the IMAP transport worked. Do not
                # waste every proxy or hide an invalid mailbox password.
                if not exc.retryable:
                    self._record_success(
                        document, max(1, round((time.perf_counter() - started) * 1000))
                    )
                    raise
                self._record_failure(document, exc)
                last_error = exc

        detail = last_error.message if last_error else "代理配置均无效"
        raise MailboxError(
            "proxy_pool_exhausted",
            f"项目代理池 {attempted or len(candidates)} 条代理均无法连接 mail.com IMAP；最后错误：{detail}",
            retryable=True,
        ) from None

    def probe(self) -> dict[str, Any]:
        return self._run(lambda service: service.probe())

    def test(self, email: str, password: str) -> dict[str, Any]:
        return self._run(lambda service: service.test(email, password))

    def messages(
        self, email: str, password: str, *, folder: str = "INBOX", limit: int = 20
    ) -> Any:
        return self._run(
            lambda service: service.messages(email, password, folder=folder, limit=limit)
        )
