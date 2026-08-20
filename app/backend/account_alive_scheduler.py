from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from .account_alive_service import AccountAliveCheckService
from .resource_service import MongoResourceStore


logger = logging.getLogger(__name__)


class AccountAliveScheduler:
    """Verify newly created accounts once they have survived for 15 minutes."""

    def __init__(
        self,
        service: AccountAliveCheckService,
        resources: MongoResourceStore,
        *,
        interval_seconds: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        configured_interval = (
            interval_seconds
            if interval_seconds is not None
            else float(os.getenv("AUTOREGISTER_ALIVE_CHECK_INTERVAL_SECONDS", "900"))
        )
        configured_batch = (
            batch_size
            if batch_size is not None
            else int(os.getenv("AUTOREGISTER_ALIVE_CHECK_BATCH_SIZE", "100"))
        )
        self.interval_seconds = max(1.0, configured_interval)
        self.batch_size = max(1, min(1000, configured_batch))
        self.service = service
        self.resources = resources
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(), name="account-alive-15m-scheduler"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> dict[str, int]:
        if self._lock.locked():
            return {
                "requested": 0,
                "alive": 0,
                "dead": 0,
                "failed": 0,
                "skipped": 0,
            }
        async with self._lock:
            account_ids = await self.resources.account_ids_due_for_alive_15m_check(
                limit=self.batch_size
            )
            if not account_ids:
                return {
                    "requested": 0,
                    "alive": 0,
                    "dead": 0,
                    "failed": 0,
                    "skipped": 0,
                }
            result = await self.service.check_accounts(account_ids)
            summary = {
                "requested": result.requested,
                "alive": result.alive,
                "dead": result.dead,
                "failed": result.failed,
                "skipped": result.skipped,
            }
            logger.info("account 15-minute alive cycle completed: %s", summary)
            return summary

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds
                )
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("account 15-minute alive cycle failed")
