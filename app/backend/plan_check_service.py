from __future__ import annotations

import asyncio
from urllib.parse import quote
from uuid import uuid4

from .chatgpt_plan import PlanCheckError, check_account_plan_curl
from .checkout_type import CheckoutTypeCheckError, check_checkout_type_curl
from .oai_iprocket_chain_bridge import ensure_background_server
from .oai_payment_extractor.transport import chain_bridge_proxy_url
from .probe_store import MongoProbeStore, ProxyLease
from .resource_models import AccountCheckoutTypeCheckResult, AccountPlanCheckItem, AccountPlanCheckResult
from .resource_service import MongoResourceStore


CHAIN_BRIDGE_PROXY_SUFFIXES = (
    "ipipbright.net",
    "1024proxy.io",
    "iprocket.io",
    "iprocket.pro",
    "iproyal.net",
    "iproyal.com",
)


def requires_chain_bridge(host: str) -> bool:
    lowered = str(host or "").strip().lower().rstrip(".")
    return any(
        lowered == suffix or lowered.endswith("." + suffix)
        for suffix in CHAIN_BRIDGE_PROXY_SUFFIXES
    )


def proxy_url(lease: ProxyLease) -> str:
    if lease.username and requires_chain_bridge(lease.host):
        ensure_background_server()
        return chain_bridge_proxy_url(
            lease.host,
            lease.port,
            lease.username,
            lease.password,
            "http",
        )
    authority = f"{lease.host}:{lease.port}"
    if lease.username or lease.password:
        credentials = f"{quote(lease.username, safe='')}:{quote(lease.password, safe='')}"
        authority = f"{credentials}@{authority}"
    return f"http://{authority}"


def timezone_offset_for_country(country: str) -> str:
    # API expects JavaScript Date.getTimezoneOffset semantics (UTC - local).
    return {
        "JP": "-540", "KR": "-540", "TR": "-180", "SG": "-480",
        "HK": "-480", "TW": "-480", "GB": "0", "DE": "-60",
        "FR": "-60", "US": "300", "CA": "300", "BR": "180",
        "AU": "-600", "PH": "-480",
    }.get(country, "0")


class AccountPlanCheckService:
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
        self._oaics_tasks: set[asyncio.Task[None]] = set()

    async def start_oaics_scans(self, ids: list[str]) -> dict[str, Any]:
        started = skipped = 0
        for account_id in dict.fromkeys(str(value) for value in ids):
            source = await self.resources.claim_account_oaics_scan(account_id)
            if source is None:
                skipped += 1
                continue
            task = asyncio.create_task(self._run_oaics_scan(account_id, source))
            self._oaics_tasks.add(task)
            task.add_done_callback(self._oaics_tasks.discard)
            started += 1
        return {"requested": len(set(ids)), "started": started, "skipped": skipped}

    async def _run_oaics_scan(self, account_id: str, source: dict[str, Any]) -> None:
        candidates = await self.proxies.all_eligible_proxy_candidates()
        if not candidates:
            await self.resources.store_account_oaics_scan_failure(account_id, "没有可用代理")
            return
        semaphore = asyncio.Semaphore(self.max_concurrency)
        token = str(source.get("accessToken") or "")

        async def check_one(candidate: ProxyLease) -> dict[str, Any]:
            async with semaphore:
                owner = f"oaics-scan:{account_id}:{uuid4()}"
                lease = await self.proxies.acquire_proxy_by_id(candidate.id, owner, lease_seconds=180)
                if lease is None:
                    return {"proxyId": candidate.id, "country": candidate.country,
                            "checkoutType": None, "error": "proxy_unavailable"}
                try:
                    try:
                        result = await asyncio.to_thread(
                            check_checkout_type_curl, token, proxy_url=proxy_url(lease),
                            country=lease.country,
                        )
                        return {"proxyId": lease.id, "country": lease.country,
                                "checkoutType": result.checkout_type,
                                "checkoutDetail": result.checkout_detail}
                    except CheckoutTypeCheckError as exc:
                        return {"proxyId": lease.id, "country": lease.country,
                                "checkoutType": None, "error": exc.code,
                                "httpStatus": exc.http_status}
                    except Exception as exc:
                        return {"proxyId": lease.id, "country": lease.country,
                                "checkoutType": None, "error": type(exc).__name__}
                finally:
                    await self.proxies.release_proxy(lease.id, owner)

        try:
            results = list(await asyncio.gather(*(check_one(item) for item in candidates)))
            grouped: dict[str, dict[str, int]] = {}
            for item in results:
                country = str(item.get("country") or "ZZ")
                stats = grouped.setdefault(country, {"total": 0, "oaics": 0, "cs": 0, "failed": 0})
                stats["total"] += 1
                kind = item.get("checkoutType")
                stats["oaics" if kind == "oaics" else "cs" if kind == "cs" else "failed"] += 1
            country_stats = [
                {"country": country, **stats,
                 "successRate": round(stats["oaics"] * 100 / stats["total"], 2) if stats["total"] else 0}
                for country, stats in grouped.items()
            ]
            country_stats.sort(key=lambda item: (-float(item["successRate"]), -int(item["total"]), str(item["country"])))
            await self.resources.store_account_oaics_scan_result(
                account_id, results=results, country_stats=country_stats
            )
        except Exception as exc:
            await self.resources.store_account_oaics_scan_failure(
                account_id, f"OAICS 检测异常：{type(exc).__name__}"
            )

    async def check_accounts(
        self,
        ids: list[str],
        *,
        proxy_id: str | None = None,
        country: str | None = None,
    ) -> AccountPlanCheckResult:
        unique_ids = list(dict.fromkeys(str(value) for value in ids))
        available = await self.proxies.count_eligible_proxies()
        semaphore = asyncio.Semaphore(
            1 if proxy_id else max(1, min(self.max_concurrency, available))
        )

        async def check_one(account_id: str) -> AccountPlanCheckItem:
            async with semaphore:
                return await self._check_one(
                    account_id, proxy_id=proxy_id, country=country
                )

        items = await asyncio.gather(*(check_one(account_id) for account_id in unique_ids))
        return self._result(list(items))

    async def _check_one(
        self,
        account_id: str,
        *,
        proxy_id: str | None = None,
        country: str | None = None,
    ) -> AccountPlanCheckItem:
        source = await self.resources.claim_account_plan_check(account_id)
        if source is None:
            return AccountPlanCheckItem(
                id=account_id,
                status="skipped",
                errorCode="account_missing_token_or_busy",
            )

        owner = f"plan:{uuid4()}"
        registration_country = str(
            country
            or source.get("rebindProxyCountry")
            or source.get("registrationCountry")
            or ""
        ).upper()
        if proxy_id:
            lease = await self.proxies.acquire_proxy_by_id(
                proxy_id,
                owner,
                lease_seconds=120,
                country=registration_country or None,
            )
        else:
            lease = await self.proxies.acquire_proxy(
                owner,
                lease_seconds=120,
                country=registration_country or None,
            )
        if lease is None:
            error = PlanCheckError("no_eligible_proxy")
            await self.resources.store_account_plan_failure(account_id, error)
            return AccountPlanCheckItem(
                id=account_id, status="failed", errorCode=error.code
            )

        try:
            token = str(source.get("accessToken") or "")
            try:
                result = await asyncio.to_thread(
                    check_account_plan_curl,
                    token,
                    proxy_url=proxy_url(lease),
                    timezone_offset_min=timezone_offset_for_country(
                        registration_country
                    ),
                )
            except PlanCheckError as exc:
                await self.resources.store_account_plan_failure(account_id, exc)
                if exc.http_status is not None:
                    await self.proxies.record_proxy_success(lease.id, exc.elapsed_ms)
                return AccountPlanCheckItem(
                    id=account_id, status="failed", errorCode=exc.code
                )
            except Exception:
                exc = PlanCheckError("plan_request_failed", retryable=True)
                await self.resources.store_account_plan_failure(account_id, exc)
                return AccountPlanCheckItem(
                    id=account_id, status="failed", errorCode=exc.code
                )
            try:
                await self.resources.store_account_plan_result(account_id, result)
            except Exception:
                error = PlanCheckError("plan_result_store_failed")
                try:
                    await self.resources.store_account_plan_failure(
                        account_id, error
                    )
                except Exception:
                    pass
                return AccountPlanCheckItem(
                    id=account_id,
                    status="failed",
                    errorCode=error.code,
                )
            await self.proxies.record_proxy_success(lease.id, result.elapsed_ms)
            return AccountPlanCheckItem(id=account_id, status="success")
        finally:
            await self.proxies.release_proxy(lease.id, owner)

    @staticmethod
    def _result(items: list[AccountPlanCheckItem]) -> AccountPlanCheckResult:
        return AccountPlanCheckResult(
            requested=len(items),
            succeeded=sum(item.status == "success" for item in items),
            failed=sum(item.status == "failed" for item in items),
            skipped=sum(item.status == "skipped" for item in items),
            items=items,
        )

    async def check_checkout_types(
        self, ids: list[str], *, proxy_id: str | None = None
    ) -> AccountCheckoutTypeCheckResult:
        unique_ids = list(dict.fromkeys(str(value) for value in ids))
        semaphore = asyncio.Semaphore(max(1, min(self.max_concurrency, len(unique_ids))))

        async def check_one(account_id: str) -> AccountPlanCheckItem:
            async with semaphore:
                source = await self.resources.claim_account_checkout_type_check(account_id)
                if source is None:
                    return AccountPlanCheckItem(id=account_id, status="skipped", errorCode="account_missing_token_or_busy")
                owner = f"checkout-type:{uuid4()}"
                country = str(source.get("registrationCountry") or "").upper()
                if not country:
                    error = CheckoutTypeCheckError("checkout_type_country_missing")
                    await self.resources.store_account_checkout_type_failure(account_id, error)
                    return AccountPlanCheckItem(id=account_id, status="failed", errorCode=error.code)
                if proxy_id == "local7890":
                    lease = None
                else:
                    lease = await (
                    self.proxies.acquire_proxy_by_id(proxy_id, owner, lease_seconds=120)
                    if proxy_id else self.proxies.acquire_proxy(owner, lease_seconds=120, country=country)
                    )
                if lease is None:
                    if not proxy_id or proxy_id == "local7890":
                        try:
                            result = await asyncio.to_thread(
                                check_checkout_type_curl,
                                str(source.get("accessToken") or ""),
                                proxy_url="http://127.0.0.1:7890",
                                country=country,
                            )
                            await self.resources.store_account_checkout_type(account_id, result)
                            return AccountPlanCheckItem(id=account_id, status="success")
                        except CheckoutTypeCheckError as exc:
                            await self.resources.store_account_checkout_type_failure(account_id, exc)
                            return AccountPlanCheckItem(id=account_id, status="failed", errorCode=exc.code)
                    error = CheckoutTypeCheckError("no_eligible_proxy")
                    await self.resources.store_account_checkout_type_failure(account_id, error)
                    return AccountPlanCheckItem(id=account_id, status="failed", errorCode=error.code)
                try:
                    result = await asyncio.to_thread(
                        check_checkout_type_curl,
                        str(source.get("accessToken") or ""),
                        proxy_url=proxy_url(lease),
                        country=country,
                    )
                    await self.resources.store_account_checkout_type(account_id, result)
                    await self.proxies.record_proxy_success(lease.id, result.elapsed_ms)
                    return AccountPlanCheckItem(id=account_id, status="success")
                except CheckoutTypeCheckError as exc:
                    await self.resources.store_account_checkout_type_failure(account_id, exc)
                    return AccountPlanCheckItem(id=account_id, status="failed", errorCode=exc.code)
                except Exception:
                    error = CheckoutTypeCheckError("checkout_type_request_failed")
                    await self.resources.store_account_checkout_type_failure(account_id, error)
                    return AccountPlanCheckItem(id=account_id, status="failed", errorCode=error.code)
                finally:
                    await self.proxies.release_proxy(lease.id, owner)

        items = list(await asyncio.gather(*(check_one(account_id) for account_id in unique_ids)))
        base = self._result(items)
        return AccountCheckoutTypeCheckResult(**base.model_dump())
