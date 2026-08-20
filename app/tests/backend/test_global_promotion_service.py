import asyncio
from datetime import datetime, timezone

from backend.chatgpt_plan import AccountPlanResult
from backend.global_promotion_service import GlobalPromotionCheckService
from backend.probe_store import ProxyLease


def result(eligible: bool) -> AccountPlanResult:
    return AccountPlanResult(
        checked_at=datetime.now(timezone.utc), account_id="account", current_plan_type="free",
        subscription_plan="chatgptfreeplan", has_active_subscription=False,
        expires_at=None, renews_at=None, plus_trial_eligible=eligible,
        plus_trial_campaign_id="trial" if eligible else None, elapsed_ms=100,
    )


class Resources:
    def __init__(self) -> None:
        self.pending: str | None = None
        self.stored: dict | None = None

    async def store_global_promotion_pending(self, _account_id: str, message: str) -> None:
        self.pending = message

    async def store_global_promotion_result(self, _account_id: str, **values) -> None:
        self.stored = values


class Proxies:
    def __init__(self, leases: list[ProxyLease]) -> None:
        self.leases = leases
        self.released: list[str] = []

    async def global_promotion_candidates(self, *, limit: int = 5):
        return self.leases[:limit]

    async def acquire_proxy_by_id(self, proxy_id: str, _owner: str, **_kwargs):
        return next((lease for lease in self.leases if lease.id == proxy_id), None)

    async def release_proxy(self, proxy_id: str, _owner: str) -> None:
        self.released.append(proxy_id)

    async def record_proxy_success(self, _proxy_id: str, _latency: int) -> None:
        pass


def test_all_proxy_countries_must_be_eligible(monkeypatch) -> None:
    resources = Resources()
    proxies = Proxies([
        ProxyLease("jp", "jp.test", 1, "", "", country="JP"),
        ProxyLease("us", "us.test", 2, "", "", country="US"),
    ])
    answers = iter([result(True), result(False)])
    monkeypatch.setattr(
        "backend.global_promotion_service.check_account_plan_curl",
        lambda *_args, **_kwargs: next(answers),
    )

    asyncio.run(GlobalPromotionCheckService(resources, proxies)._check({"_id": "a", "accessToken": "AT"}))

    assert resources.stored is not None
    assert resources.stored["status"] == "ineligible"
    assert resources.stored["eligible"] is False
    assert len(resources.stored["results"]) == 2
    assert proxies.released == ["jp", "us"]


def test_five_eligible_exits_mark_account_globally_eligible(monkeypatch) -> None:
    resources = Resources()
    proxies = Proxies([
        ProxyLease(str(index), f"p{index}.test", index, "", "", country=country)
        for index, country in enumerate(("JP", "US", "DE", "BR", "PH"), 1)
    ])
    monkeypatch.setattr(
        "backend.global_promotion_service.check_account_plan_curl",
        lambda *_args, **_kwargs: result(True),
    )

    asyncio.run(GlobalPromotionCheckService(resources, proxies)._check({"_id": "a", "accessToken": "AT"}))

    assert resources.stored is not None
    assert resources.stored["status"] == "eligible"
    assert resources.stored["eligible"] is True
    assert len(resources.stored["results"]) == 5
