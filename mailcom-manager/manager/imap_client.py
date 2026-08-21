from __future__ import annotations

import imaplib
import os
import re
import socket
import ssl
import threading
from base64 import b64encode
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import socks
from urllib3.util.ssltransport import SSLTransport


IMAP_HOST = "imap.mail.com"
IMAP_PORT = 993
CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
VERIFICATION_PATTERN = re.compile(
    r"chatgpt|openai|verification\s+code|temporary\s+code|"
    r"验证码|驗證碼|認証コード|確認コード|doğrulama\s+kodu",
    re.IGNORECASE,
)
ALLOWED_FOLDERS = {"INBOX", "Spam", "Junk"}


PROXY_TYPES = {
    "socks4": (socks.SOCKS4, False),
    "socks4a": (socks.SOCKS4, True),
    "socks5": (socks.SOCKS5, False),
    "socks5h": (socks.SOCKS5, True),
}


class _SocksImap4Ssl(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        proxy_type: int,
        proxy_host: str,
        proxy_port: int,
        proxy_username: str | None,
        proxy_password: str | None,
        proxy_rdns: bool,
        timeout: float,
    ) -> None:
        self._proxy_type = proxy_type
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._proxy_username = proxy_username
        self._proxy_password = proxy_password
        self._proxy_rdns = proxy_rdns
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout: float | None):
        connection = socks.socksocket()
        connection.set_proxy(
            self._proxy_type,
            addr=self._proxy_host,
            port=self._proxy_port,
            rdns=self._proxy_rdns,
            username=self._proxy_username,
            password=self._proxy_password,
        )
        connection.settimeout(timeout)
        connection.connect((self.host, self.port))
        return self.ssl_context.wrap_socket(connection, server_hostname=self.host)


def _http_connect(
    connection: Any,
    host: str,
    port: int,
    *,
    username: str | None,
    password: str | None,
) -> None:
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    headers = [
        f"CONNECT {authority} HTTP/1.1",
        f"Host: {authority}",
        "Proxy-Connection: Keep-Alive",
    ]
    if username is not None:
        credentials = b64encode(f"{username}:{password or ''}".encode()).decode("ascii")
        headers.append(f"Proxy-Authorization: Basic {credentials}")
    connection.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))

    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = connection.recv(1)
        if not chunk:
            raise OSError("HTTP proxy closed the CONNECT tunnel")
        response.extend(chunk)
        if len(response) > 64 * 1024:
            raise OSError("HTTP proxy returned oversized headers")
    status_line = bytes(response).split(b"\r\n", 1)[0].decode("iso-8859-1")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) // 100 != 2:
        raise OSError(f"HTTP proxy CONNECT failed: {status_line}")


class _HttpProxyImap4Ssl(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        proxy_host: str,
        proxy_port: int,
        proxy_username: str | None,
        proxy_password: str | None,
        proxy_tls: bool,
        timeout: float,
    ) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._proxy_username = proxy_username
        self._proxy_password = proxy_password
        self._proxy_tls = proxy_tls
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout: float | None):
        connection: Any = socket.create_connection(
            (self._proxy_host, self._proxy_port), timeout=timeout
        )
        try:
            if self._proxy_tls:
                proxy_context = ssl.create_default_context()
                connection = proxy_context.wrap_socket(
                    connection, server_hostname=self._proxy_host
                )
            _http_connect(
                connection,
                self.host,
                self.port,
                username=self._proxy_username,
                password=self._proxy_password,
            )
            if self._proxy_tls:
                return SSLTransport(
                    connection, self.ssl_context, server_hostname=self.host
                )
            return self.ssl_context.wrap_socket(
                connection, server_hostname=self.host
            )
        except Exception:
            connection.close()
            raise


class _ForwardedImap4Ssl(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_host: str,
        connect_port: int,
        timeout: float,
    ) -> None:
        self._connect_host = connect_host
        self._connect_port = connect_port
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout: float | None):
        connection = socket.create_connection(
            (self._connect_host, self._connect_port),
            timeout=timeout,
        )
        return self.ssl_context.wrap_socket(connection, server_hostname=self.host)


class MailboxError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag.casefold() in {"script", "style", "noscript"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        normalized = " ".join(data.split())
        if normalized:
            self.texts.append(normalized)


def _html_text(value: str) -> str:
    parser = _TextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return ""
    return "\n".join(parser.texts)


def _message_text(message: Any) -> str:
    values: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            raw = part.get_payload(decode=True)
            if not isinstance(raw, bytes):
                continue
            content = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        if not isinstance(content, str):
            continue
        values.append(_html_text(content) if content_type == "text/html" else content)
    return "\n".join(values)


def _received_at(message: Any) -> str | None:
    try:
        parsed = parsedate_to_datetime(str(message.get("Date") or ""))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class MailSummary:
    uid: str
    folder: str
    subject: str
    sender: str
    recipients: str
    received_at: str | None
    verification_code: str | None
    preview: str

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "uid": data["uid"],
            "folder": data["folder"],
            "subject": data["subject"],
            "sender": data["sender"],
            "recipients": data["recipients"],
            "receivedAt": data["received_at"],
            "verificationCode": data["verification_code"],
            "preview": data["preview"],
        }


class ImapMailboxService:
    def __init__(
        self,
        *,
        factory: Callable[..., Any] | None = None,
        timeout_seconds: float = 15,
        max_message_bytes: int = 2 * 1024 * 1024,
        proxy_url: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self._config_lock = threading.RLock()
        self.host = (host or os.getenv("MAILCOM_IMAP_HOST", IMAP_HOST)).strip()
        self.port = int(port or os.getenv("MAILCOM_IMAP_PORT", str(IMAP_PORT)))
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("MailCom IMAP endpoint is invalid")
        self.connect_host = os.getenv("MAILCOM_IMAP_CONNECT_HOST", "").strip()
        self.connect_port = int(
            os.getenv("MAILCOM_IMAP_CONNECT_PORT", str(self.port))
        )
        if self.connect_host and not 1 <= self.connect_port <= 65535:
            raise ValueError("MailCom IMAP forwarded endpoint is invalid")
        self.proxy_url = (
            os.getenv("MAILCOM_IMAP_PROXY", "") if proxy_url is None else proxy_url
        ).strip()
        self.proxy_host = ""
        self.proxy_port = 0
        self.proxy_scheme = ""
        self.proxy_username: str | None = None
        self.proxy_password: str | None = None
        self.proxy_type = 0
        self.proxy_rdns = False
        if self.proxy_url:
            parsed = urlsplit(self.proxy_url)
            self.proxy_scheme = parsed.scheme.casefold()
            if self.proxy_scheme not in {*PROXY_TYPES, "http", "https"} or not parsed.hostname:
                raise ValueError(
                    "MAILCOM_IMAP_PROXY must use http, https, socks4, socks4a, "
                    "socks5, or socks5h"
                )
            self.proxy_host = parsed.hostname
            default_port = 443 if self.proxy_scheme == "https" else (
                8080 if self.proxy_scheme == "http" else 1080
            )
            try:
                self.proxy_port = parsed.port or default_port
            except ValueError as exc:
                raise ValueError("MAILCOM_IMAP_PROXY port is invalid") from exc
            self.proxy_username = unquote(parsed.username) if parsed.username else None
            self.proxy_password = unquote(parsed.password) if parsed.password else None
            if self.proxy_scheme in PROXY_TYPES:
                self.proxy_type, self.proxy_rdns = PROXY_TYPES[self.proxy_scheme]
        self.factory = factory or self._default_factory
        self.timeout_seconds = timeout_seconds
        self.max_message_bytes = max_message_bytes

    def reconfigure(
        self,
        *,
        host: str,
        port: int,
        proxy_url: str,
    ) -> None:
        configured = ImapMailboxService(
            timeout_seconds=self.timeout_seconds,
            max_message_bytes=self.max_message_bytes,
            proxy_url=proxy_url,
            host=host,
            port=port,
        )
        names = (
            "host",
            "port",
            "connect_host",
            "connect_port",
            "proxy_url",
            "proxy_host",
            "proxy_port",
            "proxy_scheme",
            "proxy_username",
            "proxy_password",
            "proxy_type",
            "proxy_rdns",
        )
        with self._config_lock:
            for name in names:
                setattr(self, name, getattr(configured, name))

    @property
    def route(self) -> str:
        if self.proxy_host:
            return self.proxy_scheme
        if self.connect_host:
            return "forwarded"
        return "direct"

    def _default_factory(self, host: str, port: int, *, timeout: float) -> Any:
        with self._config_lock:
            proxy_host = self.proxy_host
            proxy_port = self.proxy_port
            proxy_scheme = self.proxy_scheme
            proxy_username = self.proxy_username
            proxy_password = self.proxy_password
            proxy_type = self.proxy_type
            proxy_rdns = self.proxy_rdns
            connect_host = self.connect_host
            connect_port = self.connect_port
        if proxy_host:
            if proxy_scheme in {"http", "https"}:
                return _HttpProxyImap4Ssl(
                    host,
                    port,
                    proxy_host=proxy_host,
                    proxy_port=proxy_port,
                    proxy_username=proxy_username,
                    proxy_password=proxy_password,
                    proxy_tls=proxy_scheme == "https",
                    timeout=timeout,
                )
            return _SocksImap4Ssl(
                host,
                port,
                proxy_type=proxy_type,
                proxy_host=proxy_host,
                proxy_port=proxy_port,
                proxy_username=proxy_username,
                proxy_password=proxy_password,
                proxy_rdns=proxy_rdns,
                timeout=timeout,
            )
        if connect_host:
            return _ForwardedImap4Ssl(
                host,
                port,
                connect_host=connect_host,
                connect_port=connect_port,
                timeout=timeout,
            )
        return imaplib.IMAP4_SSL(host, port, timeout=timeout)

    def _connect(self, email: str, password: str) -> Any:
        client: Any | None = None
        try:
            client = self.factory(self.host, self.port, timeout=self.timeout_seconds)
            status, _ = client.login(email, password)
            if str(status).upper() != "OK":
                raise MailboxError("auth_failed", "邮箱或密码错误，或者 IMAP 未启用")
            return client
        except MailboxError:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            raise
        except imaplib.IMAP4.error:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            raise MailboxError("auth_failed", "邮箱或密码错误，或者 IMAP 未启用") from None
        except (OSError, socket.timeout, TimeoutError):
            raise MailboxError(
                "connection_failed", "mail.com IMAP 连接失败", retryable=True
            ) from None

    @staticmethod
    def _logout(client: Any) -> None:
        try:
            client.logout()
        except Exception:
            pass

    def test(self, email: str, password: str) -> dict[str, Any]:
        client = self._connect(email, password)
        try:
            status, data = client.select("INBOX", readonly=True)
            if str(status).upper() != "OK":
                raise MailboxError("inbox_failed", "收件箱读取失败", retryable=True)
            count = int(data[0]) if data and bytes(data[0]).isdigit() else 0
            return {"ok": True, "messageCount": count}
        except MailboxError:
            raise
        except (imaplib.IMAP4.error, OSError, socket.timeout, TimeoutError):
            raise MailboxError("inbox_failed", "收件箱读取失败", retryable=True) from None
        finally:
            self._logout(client)

    def messages(
        self,
        email: str,
        password: str,
        *,
        folder: str = "INBOX",
        limit: int = 20,
    ) -> list[MailSummary]:
        if folder not in ALLOWED_FOLDERS:
            raise MailboxError("folder_invalid", "邮箱文件夹无效")
        client = self._connect(email, password)
        try:
            status, _ = client.select(folder, readonly=True)
            if str(status).upper() != "OK":
                return []
            status, data = client.search(None, "ALL")
            if str(status).upper() != "OK" or not data or not isinstance(data[0], bytes):
                return []
            ids = data[0].split()[-max(1, min(limit, 100)) :]
            results: list[MailSummary] = []
            for message_id in reversed(ids):
                fetch_status, fetch_data = client.fetch(message_id, "(BODY.PEEK[])")
                if str(fetch_status).upper() != "OK" or not fetch_data:
                    continue
                payload = next(
                    (
                        item[1]
                        for item in fetch_data
                        if isinstance(item, tuple)
                        and len(item) >= 2
                        and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if payload is None or len(payload) > self.max_message_bytes:
                    continue
                try:
                    message = BytesParser(policy=policy.default).parsebytes(payload)
                except Exception:
                    continue
                subject = str(message.get("Subject") or "(无主题)")[:500]
                sender = str(message.get("From") or "")[:500]
                recipients = " | ".join(
                    str(message.get(name) or "")
                    for name in ("To", "Delivered-To", "X-Original-To", "Envelope-To")
                    if message.get(name)
                )[:1000]
                body = _message_text(message)
                normalized = " ".join(body.split())
                candidate_text = f"{subject}\n{body}"
                code_match = (
                    CODE_PATTERN.search(candidate_text)
                    if VERIFICATION_PATTERN.search(candidate_text)
                    else None
                )
                results.append(
                    MailSummary(
                        uid=message_id.decode("ascii", errors="replace"),
                        folder=folder,
                        subject=subject,
                        sender=sender,
                        recipients=recipients,
                        received_at=_received_at(message),
                        verification_code=code_match.group(1) if code_match else None,
                        preview=normalized[:500],
                    )
                )
            return results
        except MailboxError:
            raise
        except (imaplib.IMAP4.error, OSError, socket.timeout, TimeoutError):
            raise MailboxError("inbox_failed", "邮件读取失败", retryable=True) from None
        finally:
            self._logout(client)
