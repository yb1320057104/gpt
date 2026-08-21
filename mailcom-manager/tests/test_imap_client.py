from __future__ import annotations

import pytest
import socks

from manager.imap_client import ImapMailboxService, _http_connect


RAW_MESSAGE = (
    "From: OpenAI <noreply@example.test>\r\n"
    "To: alias@gardener.com\r\n"
    "Delivered-To: alias@gardener.com\r\n"
    "Date: Mon, 17 Aug 2026 08:30:00 +0000\r\n"
    "Subject: Your temporary ChatGPT verification code\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Enter this temporary verification code to continue:\r\n"
    "123456\r\n"
).encode()


class FakeImap:
    def __init__(self) -> None:
        self.fetch_query = ""
        self.readonly = False
        self.logged_out = False

    def login(self, email: str, password: str):
        assert email == "alias@gardener.com"
        assert password == "mail-password"
        return "OK", [b"authenticated"]

    def select(self, folder: str, readonly: bool = False):
        assert folder == "INBOX"
        self.readonly = readonly
        return "OK", [b"1"]

    def search(self, charset, criterion: str):
        assert charset is None
        assert criterion == "ALL"
        return "OK", [b"1"]

    def fetch(self, message_id: bytes, query: str):
        assert message_id == b"1"
        self.fetch_query = query
        return "OK", [(b"1 (BODY[])", RAW_MESSAGE), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logout"]


def test_imap_reader_uses_readonly_peek_and_extracts_recipient_code() -> None:
    fake = FakeImap()
    service = ImapMailboxService(factory=lambda *_args, **_kwargs: fake)

    result = service.test("alias@gardener.com", "mail-password")
    assert result == {"ok": True, "messageCount": 1}

    messages = service.messages(
        "alias@gardener.com", "mail-password", folder="INBOX", limit=20
    )
    assert len(messages) == 1
    assert messages[0].verification_code == "123456"
    assert "alias@gardener.com" in messages[0].recipients
    assert fake.readonly is True
    assert fake.fetch_query == "(BODY.PEEK[])"
    assert fake.logged_out is True


def test_forwarded_endpoint_preserves_mail_host_for_tls(monkeypatch) -> None:
    monkeypatch.setenv("MAILCOM_IMAP_CONNECT_HOST", "127.0.0.1")
    monkeypatch.setenv("MAILCOM_IMAP_CONNECT_PORT", "1993")
    service = ImapMailboxService()

    assert service.host == "imap.mail.com"
    assert service.port == 993
    assert service.connect_host == "127.0.0.1"
    assert service.connect_port == 1993
    assert service.route == "forwarded"


@pytest.mark.parametrize(
    ("url", "route", "port", "proxy_type", "rdns"),
    [
        ("http://127.0.0.1:8080", "http", 8080, 0, False),
        ("https://proxy.example", "https", 443, 0, False),
        ("socks4://127.0.0.1", "socks4", 1080, socks.SOCKS4, False),
        ("socks4a://127.0.0.1", "socks4a", 1080, socks.SOCKS4, True),
        ("socks5://127.0.0.1:7890", "socks5", 7890, socks.SOCKS5, False),
        ("socks5h://127.0.0.1:7891", "socks5h", 7891, socks.SOCKS5, True),
    ],
)
def test_proxy_url_formats(url, route, port, proxy_type, rdns) -> None:
    service = ImapMailboxService(proxy_url=url)

    assert service.route == route
    assert service.proxy_port == port
    assert service.proxy_type == proxy_type
    assert service.proxy_rdns is rdns


def test_proxy_credentials_are_url_decoded() -> None:
    service = ImapMailboxService(
        proxy_url="http://user%40example.test:p%3Ass@proxy.example:3128"
    )

    assert service.proxy_username == "user@example.test"
    assert service.proxy_password == "p:ss"


def test_invalid_proxy_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="must use http"):
        ImapMailboxService(proxy_url="ftp://proxy.example:21")


class FakeProxySocket:
    def __init__(self, response: bytes) -> None:
        self.response = bytearray(response)
        self.sent = b""

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def recv(self, _size: int) -> bytes:
        if not self.response:
            return b""
        return bytes([self.response.pop(0)])


def test_http_connect_sends_basic_auth_without_leaking_it_to_target() -> None:
    connection = FakeProxySocket(b"HTTP/1.1 200 Connection established\r\n\r\n")

    _http_connect(
        connection,
        "imap.mail.com",
        993,
        username="dynamic-user",
        password="dynamic-password",
    )

    request = connection.sent.decode("ascii")
    assert request.startswith("CONNECT imap.mail.com:993 HTTP/1.1\r\n")
    assert "Proxy-Authorization: Basic " in request


def test_http_connect_rejects_proxy_error() -> None:
    connection = FakeProxySocket(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")

    with pytest.raises(OSError, match="407 Proxy Authentication Required"):
        _http_connect(connection, "imap.mail.com", 993, username=None, password=None)
