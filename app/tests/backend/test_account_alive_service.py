import asyncio
from datetime import datetime, timedelta, timezone

from backend.account_alive_service import (
    AccountAliveCheckService,
    account_reached_alive_15m_age,
)
from backend.chatgpt_plan import AccountPlanResult, PlanCheckError
from backend.probe_store import ProxyLease


class FakeResources:
    def __init__(self, document):
        self.document = document
        self.results = []
        self.failures = []

    async def claim_account_alive_check(self, account_id):
        return self.document if account_id == "account" else None

    async def store_account_alive_result(self, account_id, **kwargs):
        self.results.append((account_id, kwargs))

    async def store_account_alive_failure(self, account_id, error):
        self.failures.append((account_id, error.code))


class FakeProxies:
    def __init__(self, lease):
        self.lease = lease
        self.released = []
        self.successes = []

    async def count_eligible_proxies(self):
        return int(self.lease is not None)

    async def acquire_proxy(self, _owner, **_kwargs):
        return self.lease

    async def acquire_proxy_by_id(self, _proxy_id, _owner, **_kwargs):
        return self.lease

    async def release_proxy(self, proxy_id, owner):
        self.released.append((proxy_id, owner))

    async def record_proxy_success(self, proxy_id, elapsed_ms):
        self.successes.append((proxy_id, elapsed_ms))


def alive_result():
    return AccountPlanResult(
        checked_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
        account_id="account",
        current_plan_type="free",
        subscription_plan="chatgptfreeplan",
        has_active_subscription=False,
        expires_at=None,
        renews_at=None,
        plus_trial_eligible=False,
        plus_trial_campaign_id=None,
        elapsed_ms=125,
    )


def test_alive_check_marks_success_as_alive(monkeypatch):
    resources = FakeResources({"accessToken": "TEST_AT", "registrationCountry": "JP"})
    proxies = FakeProxies(ProxyLease("proxy", "proxy.test", 1000, "", ""))
    monkeypatch.setattr(
        "backend.account_alive_service.check_account_plan_curl",
        lambda *_args, **_kwargs: alive_result(),
    )

    result = asyncio.run(AccountAliveCheckService(resources, proxies).check_accounts(["account"]))

    assert result.alive == 1
    assert resources.results == [("account", {"alive": True, "http_status": 200})]
    assert proxies.successes == [("proxy", 125)]
    assert len(proxies.released) == 1


def test_alive_check_only_marks_explicit_unauthorized_as_dead(monkeypatch):
    resources = FakeResources({"accessToken": "TEST_AT"})
    proxies = FakeProxies(ProxyLease("proxy", "proxy.test", 1000, "", ""))

    def unauthorized(*_args, **_kwargs):
        raise PlanCheckError("access_token_unauthorized", http_status=401)

    monkeypatch.setattr(
        "backend.account_alive_service.check_account_plan_curl", unauthorized
    )
    result = asyncio.run(AccountAliveCheckService(resources, proxies).check_accounts(["account"]))

    assert result.dead == 1
    assert resources.results[0][1]["alive"] is False


def test_alive_check_keeps_proxy_or_server_errors_unknown(monkeypatch):
    resources = FakeResources({"accessToken": "TEST_AT"})
    proxies = FakeProxies(ProxyLease("proxy", "proxy.test", 1000, "", ""))

    def unavailable(*_args, **_kwargs):
        raise PlanCheckError("plan_http_failed", http_status=503, retryable=True)

    monkeypatch.setattr(
        "backend.account_alive_service.check_account_plan_curl", unavailable
    )
    result = asyncio.run(AccountAliveCheckService(resources, proxies).check_accounts(["account"]))

    assert result.failed == 1
    assert resources.failures == [("account", "plan_http_failed")]


def test_account_reached_alive_15m_age_uses_creation_time() -> None:
    now = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)

    assert account_reached_alive_15m_age(now - timedelta(minutes=15), now=now)
    assert not account_reached_alive_15m_age(
        now - timedelta(minutes=14, seconds=59), now=now
    )


def test_alive_check_marks_accounts_older_than_15_minutes(monkeypatch):
    resources = FakeResources(
        {
            "accessToken": "TEST_AT",
            "registrationCountry": "JP",
            "createdAt": datetime.now(timezone.utc) - timedelta(minutes=16),
        }
    )
    proxies = FakeProxies(ProxyLease("proxy", "proxy.test", 1000, "", ""))
    monkeypatch.setattr(
        "backend.account_alive_service.check_account_plan_curl",
        lambda *_args, **_kwargs: alive_result(),
    )

    result = asyncio.run(
        AccountAliveCheckService(resources, proxies).check_accounts(["account"])
    )

    assert result.alive == 1
    assert resources.results == [
        (
            "account",
            {"alive": True, "http_status": 200, "verified_15m": True},
        )
    ]
