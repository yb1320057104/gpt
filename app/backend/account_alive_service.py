from __future__ import annotations

import asyncio
from uuid import uuid4

from .chatgpt_plan import PlanCheckError, check_account_plan_curl
from .plan_check_service import proxy_url, timezone_offset_for_country
from .probe_store import MongoProbeStore
from .resource_models import AccountAliveCheckItem, AccountAliveCheckResult
from .resource_service import MongoResourceStore


DEAD_ACCOUNT_ERRORS = frozenset({
    "access_token_expired",
    "access_token_unauthorized",
})


class AccountAliveCheckService:
    def __init__(
        self,
        resources: MongoResourceStore,
        proxies: MongoProbeStore,
        *,
        max_concurrency: int = 3,
    ) -> None:
        self.resources = resources
        self.proxies = proxies
        self.max_concurrency = max(1, max_concurrency)

    async def check_accounts(
        self, ids: list[str], *, proxy_id: str | None = None
    ) -> AccountAliveCheckResult:
        unique_ids = list(dict.fromkeys(str(value) for value in ids))
        available = await self.proxies.count_eligible_proxies()
        semaphore = asyncio.Semaphore(
            1 if proxy_id else max(1, min(self.max_concurrency, available))
        )

        async def check_one(account_id: str) -> AccountAliveCheckItem:
            async with semaphore:
                return await self._check_one(account_id, proxy_id=proxy_id)

        items = await asyncio.gather(*(check_one(account_id) for account_id in unique_ids))
        return AccountAliveCheckResult(
            requested=len(items),
            alive=sum(item.status == "alive" for item in items),
            dead=sum(item.status == "dead" for item in items),
            failed=sum(item.status == "failed" for item in items),
            skipped=sum(item.status == "skipped" for item in items),
            items=list(items),
        )

    async def _check_one(
        self, account_id: str, *, proxy_id: str | None = None
    ) -> AccountAliveCheckItem:
        source = await self.resources.claim_account_alive_check(account_id)
        if source is None:
            return AccountAliveCheckItem(
                id=account_id,
                status="skipped",
                errorCode="account_missing_token_or_busy",
            )

        owner = f"alive:{uuid4()}"
        country = str(source.get("registrationCountry") or "").upper()
        if proxy_id:
            lease = await self.proxies.acquire_proxy_by_id(
                proxy_id, owner, lease_seconds=120
            )
        else:
            lease = await self.proxies.acquire_proxy(
                owner, lease_seconds=120, country=country or None
            )
        if lease is None:
            error = PlanCheckError("no_eligible_proxy")
            await self.resources.store_account_alive_failure(account_id, error)
            return AccountAliveCheckItem(
                id=account_id, status="failed", errorCode=error.code
            )

        try:
            try:
                result = await asyncio.to_thread(
                    check_account_plan_curl,
                    str(source.get("accessToken") or ""),
                    proxy_url=proxy_url(lease),
                    timezone_offset_min=timezone_offset_for_country(country),
                )
            except PlanCheckError as error:
                if error.http_status is not None:
                    await self.proxies.record_proxy_success(lease.id, error.elapsed_ms)
                if error.code in DEAD_ACCOUNT_ERRORS:
                    await self.resources.store_account_alive_result(
                        account_id,
                        alive=False,
                        error_code=error.code,
                        http_status=error.http_status,
                    )
                    return AccountAliveCheckItem(
                        id=account_id, status="dead", errorCode=error.code
                    )
                await self.resources.store_account_alive_failure(account_id, error)
                return AccountAliveCheckItem(
                    id=account_id, status="failed", errorCode=error.code
                )
            except Exception:
                error = PlanCheckError("alive_request_failed", retryable=True)
                await self.resources.store_account_alive_failure(account_id, error)
                return AccountAliveCheckItem(
                    id=account_id, status="failed", errorCode=error.code
                )

            await self.resources.store_account_alive_result(
                account_id, alive=True, http_status=result.http_status
            )
            await self.proxies.record_proxy_success(lease.id, result.elapsed_ms)
            return AccountAliveCheckItem(id=account_id, status="alive")
        finally:
            await self.proxies.release_proxy(lease.id, owner)
