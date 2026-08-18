from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from .proxy_subscription_service import ProxySubscriptionService


logger = logging.getLogger(__name__)


class ProxyHealthScheduler:
    def __init__(self, service: ProxySubscriptionService) -> None:
        self.service = service
        self.interval_seconds = max(
            60, int(os.getenv("AUTOREGISTER_PROXY_CHECK_INTERVAL_SECONDS", "300"))
        )
        self.batch_size = max(
            1, min(1000, int(os.getenv("AUTOREGISTER_PROXY_CHECK_BATCH_SIZE", "120")))
        )
        self.failure_threshold = max(
            2, int(os.getenv("AUTOREGISTER_PROXY_DELETE_AFTER_FAILURES", "3"))
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="proxy-health-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> dict[str, int | None]:
        if self._lock.locked():
            return {"tested": 0, "available": 0, "failed": 0, "deleted": 0}
        async with self._lock:
            result = await self.service.test_stored_proxies(
                timeout_seconds=12,
                limit=self.batch_size,
            )
            deleted = await self.service.resources.store.delete_repeatedly_failed_proxies(
                self.failure_threshold
            )
            summary = {
                "tested": result.tested,
                "available": result.available,
                "failed": result.failed,
                "deleted": deleted,
            }
            logger.info("proxy health cycle completed: %s", summary)
            return summary

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("proxy health cycle failed")
