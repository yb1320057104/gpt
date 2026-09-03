from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from uuid import uuid4

from .chatgpt_plan import PlanCheckError, check_account_plan_curl
from .plan_check_service import proxy_url, timezone_offset_for_country
from .probe_store import MongoProbeStore
from .resource_service import MongoResourceStore


class GlobalPromotionCheckService:
    """Background multi-exit trial eligibility checks for newly created accounts."""

    def __init__(self, resources: MongoResourceStore, proxies: MongoProbeStore) -> None:
        self.resources = resources
        self.proxies = proxies
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="global-promotion-check")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                account = await self.resources.claim_pending_global_promotion_check()
                if account is None:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                    continue
                await self._check(account)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2)

    async def _check(self, account: dict[str, Any]) -> None:
        account_id = str(account["_id"])
        token = str(account.get("accessToken") or "")
        candidates = await self.proxies.global_promotion_candidates(limit=5)
        if len(candidates) < 2:
            await self.resources.store_global_promotion_pending(
                account_id, "可用代理不足 2 个，等待代理池补充后检测"
            )
            await asyncio.sleep(5)
            return

        results: list[dict[str, Any]] = []
        all_eligible = True
        for candidate in candidates:
            owner = f"global-promo:{account_id}:{uuid4()}"
            lease = await self.proxies.acquire_proxy_by_id(candidate.id, owner, lease_seconds=120)
            if lease is None:
                all_eligible = False
                results.append({"proxyId": candidate.id, "country": candidate.country,
                                "eligible": None, "error": "代理当前不可用"})
                continue
            try:
                try:
                    result = await asyncio.to_thread(
                        check_account_plan_curl, token, proxy_url=proxy_url(lease),
                        timezone_offset_min=timezone_offset_for_country(lease.country),
                    )
                    eligible = bool(result.plus_trial_eligible)
                    all_eligible = all_eligible and eligible
                    results.append({
                        "proxyId": lease.id, "country": lease.country, "eligible": eligible,
                        "campaignId": result.plus_trial_campaign_id,
                        "httpStatus": result.http_status, "latencyMs": result.elapsed_ms,
                    })
                    await self.proxies.record_proxy_success(lease.id, result.elapsed_ms)
                except PlanCheckError as exc:
                    all_eligible = False
                    results.append({"proxyId": lease.id, "country": lease.country,
                                    "eligible": None, "error": exc.code,
                                    "httpStatus": exc.http_status})
                except Exception as exc:
                    all_eligible = False
                    results.append({"proxyId": lease.id, "country": lease.country,
                                    "eligible": None, "error": type(exc).__name__})
            finally:
                await self.proxies.release_proxy(lease.id, owner)

        conclusive = all(item.get("eligible") is not None for item in results)
        await self.resources.store_global_promotion_result(
            account_id,
            eligible=all_eligible and conclusive,
            status="eligible" if all_eligible and conclusive else "ineligible" if conclusive else "failed",
            results=results,
        )
