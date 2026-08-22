from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

try:
    from .flow import LoginAdapter
    from .parser import parse_account_line, parse_mailbox_line
    from .storage import Store
    from .jobs import WorkflowQueue
except ImportError:  # direct `uvicorn app:app` launch
    from flow import LoginAdapter
    from parser import parse_account_line, parse_mailbox_line
    from storage import Store
    from jobs import WorkflowQueue


ROOT = Path(__file__).resolve().parent
store = Store(ROOT / "data" / "console.db")
flow = LoginAdapter(store)
queue = WorkflowQueue(store)
app = FastAPI(title="ChatGPT Account Rebind Console")


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
async def state() -> dict:
    return {"accounts": store.list_accounts(), "mailboxes": store.list_mailboxes(), "logs": store.list_logs(), "jobs": queue.snapshot()}


@app.get("/api/config")
async def config() -> dict:
    """Expose readiness only; endpoint values and tokens are never returned."""
    return {
        "proxyConfigured": bool(__import__("os").getenv("CHATGPT_PROXY")),
        "sentinelConfigured": bool(__import__("os").getenv("CHATGPT_SENTINEL_TOKEN")),
        "emailChangeConfigured": bool(__import__("os").getenv("CHATGPT_EMAIL_CHANGE_ENDPOINT")),
        "emailCodeVerifyConfigured": bool(__import__("os").getenv("CHATGPT_EMAIL_CODE_VERIFY_ENDPOINT")),
    }


async def read_lines(request: Request) -> list[str]:
    raw = await request.body()
    return [line.strip() for line in raw.decode("utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]


@app.post("/api/import/accounts")
async def import_accounts(request: Request) -> dict:
    accepted, errors = [], []
    for index, line in enumerate(await read_lines(request), 1):
        try:
            accepted.append(parse_account_line(line))
        except ValueError as exc:
            errors.append({"line": index, "error": str(exc)})
    count = store.import_accounts(accepted)
    store.log("INFO", "accounts.import", f"accepted={count} errors={len(errors)}")
    return {"accepted": count, "errors": errors}


@app.post("/api/import/mailboxes")
async def import_mailboxes(request: Request) -> dict:
    accepted, errors = [], []
    for index, line in enumerate(await read_lines(request), 1):
        try:
            accepted.append(parse_mailbox_line(line))
        except ValueError as exc:
            errors.append({"line": index, "error": str(exc)})
    count = store.import_mailboxes(accepted)
    store.log("INFO", "mailboxes.import", f"accepted={count} errors={len(errors)}")
    return {"accepted": count, "errors": errors}


@app.post("/api/accounts/{account_id}/login")
async def login(account_id: int) -> dict:
    job = await queue.submit(account_id, "login")
    return {"ok": True, "job": job.__dict__ if hasattr(job, "__dict__") else {"id": job.id, "status": job.status}}


@app.post("/api/accounts/{account_id}/rebind")
async def rebind(account_id: int) -> dict:
    job = await queue.submit(account_id, "rebind")
    return {"ok": True, "job": {"id": job.id, "status": job.status, "action": job.action}}


@app.get("/api/export/accounts", response_class=PlainTextResponse)
async def export_accounts() -> str:
    rows = []
    for row in store.list_accounts():
        secret = store.account_secret(int(row["id"])) or {}
        values = [secret.get("email", "")]
        if secret.get("password"):
            values.append(secret["password"])
        if secret.get("totp"):
            values.append(secret["totp"])
        elif secret.get("access_url"):
            values.append(secret["access_url"])
        rows.append("----".join(values))
    return "\n".join(rows) + ("\n" if rows else "")


@app.get("/api/export/at", response_class=PlainTextResponse)
async def export_at() -> str:
    return "\n".join(row["at"] for row in store.list_accounts() if row.get("at")) + "\n"
