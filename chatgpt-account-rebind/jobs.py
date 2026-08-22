from __future__ import annotations

import asyncio
import logging
import os
import sys
from urllib.parse import urlparse
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import httpx

try:
    from .mailbox import MailboxCodeClient
    from .storage import Store
except ImportError:  # direct `uvicorn app:app` launch
    from mailbox import MailboxCodeClient
    from storage import Store


@dataclass(slots=True)
class Job:
    id: str
    account_id: int
    action: str
    status: str = "queued"
    error: str = ""


class WorkflowQueue:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.jobs: dict[str, Job] = {}
        self.lock = asyncio.Lock()
        self.log = logging.getLogger("rebind.workflow")

    async def submit(self, account_id: int, action: str = "login") -> Job:
        job = Job(id=f"job-{account_id}-{len(self.jobs)+1}", account_id=account_id, action=action)
        self.jobs[job.id] = job
        asyncio.create_task(self._run(job), name=job.id)
        return job

    async def _run(self, job: Job) -> None:
        account = self.store.account_secret(job.account_id)
        if not account:
            job.status, job.error = "failed", "account_not_found"
            return
        try:
            job.status = "running"
            self.store.update_account(job.account_id, status="running", last_error="")
            masked = str(account["email"])[:2] + "***"
            self.store.log("INFO", "workflow.start", f"job={job.id} account={masked} action={job.action}")
            branch = self._branch(account)
            self.store.log("INFO", "workflow.branch", f"job={job.id} branch={branch}")
            if job.action == "rebind":
                await self._rebind(account, job)
                job.status = "success"
                return
            if branch == "password_then_totp":
                result = await self._pure_http_login(account)
                self.store.update_account(
                    job.account_id,
                    status="ready",
                    at=str(result.get("at") or ""),
                    trial_status="checked" if result.get("trialShape") else "unknown",
                )
                self.store.log("INFO", "workflow.login_success", f"job={job.id} me_shape={result.get('meShape')} trial_shape={result.get('trialShape')}")
                job.status = "success"
                return
            if branch == "password_then_email_code":
                raise RuntimeError("password_email_code_flow_requires_verified_endpoint")
            if branch in {"totp", "email_code"}:
                raise RuntimeError(f"{branch}_flow_requires_verified_auth_contract")
            raise RuntimeError("insufficient_credentials")
        except Exception as exc:
            job.status, job.error = "failed", str(exc)
            failure_status = "rebind_failed" if job.action == "rebind" else "login_failed"
            self.store.update_account(job.account_id, status=failure_status, last_error=job.error[:200])
            self.store.log("ERROR", "workflow.failed", f"job={job.id} code={job.error[:120]}")

    async def _rebind(self, account: dict[str, Any], job: Job) -> None:
        """Execute only an explicitly configured, allow-listed email change contract.

        The browser capture did not contain the account-settings mutation request, so
        this adapter intentionally refuses to guess an endpoint or payload. Configure
        CHATGPT_EMAIL_CHANGE_ENDPOINT and (optionally) CHATGPT_EMAIL_CODE_VERIFY_ENDPOINT
        once a captured request is available.
        """
        endpoint = os.getenv("CHATGPT_EMAIL_CHANGE_ENDPOINT", "").strip()
        verify_endpoint = os.getenv("CHATGPT_EMAIL_CODE_VERIFY_ENDPOINT", "").strip()
        if not endpoint or not verify_endpoint:
            raise RuntimeError("email_change_contract_not_configured")
        for name, value in (("change", endpoint), ("verify", verify_endpoint)):
            parsed = urlparse(value)
            if parsed.scheme != "https" or parsed.hostname not in {"chatgpt.com", "auth.openai.com"}:
                raise RuntimeError(f"untrusted_{name}_endpoint")
        mailbox = self.store.reserve_mailbox(job.account_id)
        if not mailbox:
            raise RuntimeError("mailbox_pool_empty")
        mailbox_id = int(mailbox["id"])
        new_email = str(mailbox["email"])
        try:
            token = str(account.get("at") or "")
            if not token:
                raise RuntimeError("access_token_required_before_rebind")
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            payload = {"email": new_email}
            method = os.getenv("CHATGPT_EMAIL_CHANGE_METHOD", "POST").upper()
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.request(method, endpoint, json=payload, headers=headers)
                if response.status_code >= 400:
                    raise RuntimeError(f"email_change_http_{response.status_code}")
                self.store.log("INFO", "workflow.email_change_requested", f"job={job.id} mailbox={new_email[:2]}*** status={response.status_code}")
                since = None
                code_client = MailboxCodeClient(proxy=os.getenv("CHATGPT_PROXY", ""))
                try:
                    code = await asyncio.to_thread(code_client.wait_for_code, str(mailbox["access_url"]), since=since)
                finally:
                    code_client.close()
                verify_payload = {"email": new_email, "code": code}
                verify = await client.post(verify_endpoint, json=verify_payload, headers=headers)
                if verify.status_code >= 400:
                    raise RuntimeError(f"email_change_verify_http_{verify.status_code}")
            self.store.update_account(job.account_id, rebound_email=new_email, status="rebound")
            self.store.mark_mailbox(mailbox_id, "used", job.account_id)
            self.store.log("INFO", "workflow.rebind_success", f"job={job.id} email={new_email[:2]}***")
        except Exception:
            self.store.mark_mailbox(mailbox_id, "failed", job.account_id)
            raise

    async def _pure_http_login(self, account: dict[str, Any]) -> dict[str, Any]:
        """Run the already observed password+TOTP HTTP chain off the UI thread."""
        root = Path(__file__).resolve().parents[1]
        client_dir = root / "chatgpt-api-login"
        if str(client_dir) not in sys.path:
            sys.path.insert(0, str(client_dir))
        from client import ApiConfig, ChatGPTApiClient  # type: ignore

        config = ApiConfig(
            email=str(account.get("email") or ""),
            password=str(account.get("password") or ""),
            totp_secret=str(account.get("totp") or ""),
            proxy=os.getenv("CHATGPT_PROXY", "http://127.0.0.1:7890"),
            sentinel_token=os.getenv("CHATGPT_SENTINEL_TOKEN", ""),
        )
        client = ChatGPTApiClient(config, self.log)
        try:
            return await asyncio.to_thread(client.login)
        finally:
            client.close()

    @staticmethod
    def _branch(account: dict[str, Any]) -> str:
        password, totp, access = bool(account.get("password")), bool(account.get("totp")), bool(account.get("access_url"))
        if password and totp:
            return "password_then_totp"
        if password and access:
            return "password_then_email_code"
        if totp:
            return "totp"
        if access:
            return "email_code"
        return "insufficient_credentials"

    def snapshot(self) -> list[dict[str, Any]]:
        return [{"id": j.id, "account_id": j.account_id, "action": j.action, "status": j.status, "error": j.error} for j in self.jobs.values()]
