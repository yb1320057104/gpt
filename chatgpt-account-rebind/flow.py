from __future__ import annotations

import logging
from typing import Any

try:
    from .storage import Store
except ImportError:  # direct `uvicorn app:app` launch
    from storage import Store


class LoginAdapter:
    """接口适配层；下一步接入纯 HTTP ChatGPT 状态机。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def run_one(self, account_id: int) -> dict[str, Any]:
        account = self.store.account_secret(account_id)
        if not account:
            raise ValueError("account_not_found")
        self.store.update_account(account_id, status="running", last_error="")
        self.store.log("INFO", "login.start", f"account={account['email'][:2]}***")
        # Deliberately leave the external protocol call in one adapter so the
        # UI/state machine is usable while the live endpoint contract is being
        # validated. No guessed destructive request is sent here.
        self.store.update_account(account_id, status="login_failed", last_error="api_adapter_not_connected")
        self.store.log("ERROR", "login.failed", "api_adapter_not_connected")
        return {"ok": False, "code": "api_adapter_not_connected"}
