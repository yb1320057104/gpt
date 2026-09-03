from __future__ import annotations

from datetime import datetime, timezone

from manager.imap_client import MailboxError
from manager.proxy_pool_mailbox import MongoProxyPoolMailboxService


def test_expired_quarantine_returns_to_mailcom_rotation() -> None:
    query = MongoProxyPoolMailboxService._pool_filter()
    expiry_clause = next(
        item
        for item in query["$or"]
        if isinstance(item.get("quarantineUntil"), dict)
        and "$lte" in item["quarantineUntil"]
    )
    assert query["enabled"] is True
    assert expiry_clause["quarantineUntil"]["$lte"] <= datetime.now(timezone.utc)


class FakeCursor(list):
    def sort(self, _fields):
        return self


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents
        self.updates = []

    def find(self, _query):
        return FakeCursor(self.documents)

    def count_documents(self, _query):
        return len(self.documents)

    def update_one(self, query, update):
        self.updates.append((query, update))


class FakeAdmin:
    def command(self, name):
        assert name == "ping"


class FakeDatabase:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        assert name == "proxies"
        return self.collection


class FakeClient:
    def __init__(self, documents):
        self.admin = FakeAdmin()
        self.collection = FakeCollection(documents)

    def __getitem__(self, _name):
        return FakeDatabase(self.collection)


class FakeImapService:
    attempts = []

    def __init__(self, *, host=None, port=None, proxy_url=None, **_kwargs):
        self.host = host or "imap.mail.com"
        self.port = port or 993
        self.proxy_url = proxy_url or ""
        self.proxy_host = ""
        self.proxy_port = 0
        self.proxy_scheme = ""
        self.proxy_username = None
        self.proxy_password = None
        self.route = "direct"

    def probe(self):
        self.attempts.append(self.proxy_url)
        if ":24001" in self.proxy_url:
            raise MailboxError("proxy_test_failed", "first proxy failed", retryable=True)
        return {"ok": True, "latencyMs": 7, "route": "http"}

    def test(self, _email, _password):
        self.attempts.append(self.proxy_url)
        raise MailboxError("auth_failed", "bad password")


def make_service():
    FakeImapService.attempts = []
    client = FakeClient(
        [
            {"_id": "one", "host": "127.0.0.1", "port": 24001, "enabled": True},
            {"_id": "two", "host": "127.0.0.1", "port": 24002, "enabled": True},
        ]
    )
    service = MongoProxyPoolMailboxService(
        "mongodb://unused",
        "autoregister",
        proxy_url="",
        client=client,
        service_factory=FakeImapService,
    )
    return service, client


def test_pool_rotates_after_connection_failure_and_records_success():
    service, client = make_service()

    result = service.probe()

    assert result["route"] == "project-pool"
    assert result["attempts"] == 2
    assert FakeImapService.attempts == [
        "http://127.0.0.1:24001",
        "http://127.0.0.1:24002",
    ]
    assert service.proxy_count == 2
    assert any("mailcomLastErrorCode" in update.get("$set", {}) for _, update in client.collection.updates)
    assert any("mailcomLastSuccessAt" in update.get("$set", {}) for _, update in client.collection.updates)
    assert any(update.get("$set", {}).get("mailcomUsable") is False for _, update in client.collection.updates)
    assert any(update.get("$set", {}).get("mailcomUsable") is True for _, update in client.collection.updates)


def test_pool_does_not_rotate_for_invalid_credentials():
    service, _client = make_service()

    try:
        service.test("owner@example.com", "wrong")
    except MailboxError as exc:
        assert exc.code == "auth_failed"
    else:
        raise AssertionError("expected authentication failure")

    assert FakeImapService.attempts == ["http://127.0.0.1:24001"]
