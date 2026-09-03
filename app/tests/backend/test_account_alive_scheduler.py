import asyncio

from backend.account_alive_scheduler import AccountAliveScheduler
from backend.resource_models import AccountAliveCheckItem, AccountAliveCheckResult


class FakeResources:
    def __init__(self, account_ids: list[str]) -> None:
        self.account_ids = account_ids
        self.limits: list[int] = []

    async def account_ids_due_for_alive_15m_check(self, *, limit: int) -> list[str]:
        self.limits.append(limit)
        return self.account_ids


class FakeAliveService:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def check_accounts(self, ids: list[str]) -> AccountAliveCheckResult:
        self.calls.append(ids)
        return AccountAliveCheckResult(
            requested=2,
            alive=1,
            dead=0,
            failed=1,
            skipped=0,
            items=[
                AccountAliveCheckItem(id="account-1", status="alive"),
                AccountAliveCheckItem(
                    id="account-2", status="failed", errorCode="no_eligible_proxy"
                ),
            ],
        )


def test_scheduler_checks_due_accounts_in_configured_batches() -> None:
    resources = FakeResources(["account-1", "account-2"])
    service = FakeAliveService()
    scheduler = AccountAliveScheduler(
        service,  # type: ignore[arg-type]
        resources,  # type: ignore[arg-type]
        interval_seconds=900,
        batch_size=25,
    )

    result = asyncio.run(scheduler.run_once())

    assert resources.limits == [25]
    assert service.calls == [["account-1", "account-2"]]
    assert result == {
        "requested": 2,
        "alive": 1,
        "dead": 0,
        "failed": 1,
        "skipped": 0,
    }


def test_scheduler_does_not_call_service_without_due_accounts() -> None:
    resources = FakeResources([])
    service = FakeAliveService()
    scheduler = AccountAliveScheduler(
        service,  # type: ignore[arg-type]
        resources,  # type: ignore[arg-type]
    )

    result = asyncio.run(scheduler.run_once())

    assert result["requested"] == 0
    assert service.calls == []
