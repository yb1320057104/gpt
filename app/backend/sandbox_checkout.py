"""Loopback-only checkout emulator used by the CTF environment."""
from __future__ import annotations

import secrets
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


router = APIRouter(prefix="/api/sandbox", tags=["sandbox-checkout"])
_tasks: dict[str, dict] = {}


class SandboxCheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    country: Literal["GB"] = "GB"
    currency: Literal["GBP"] = "GBP"
    link_type: Literal["paypal"] = "paypal"
    use_promo: bool = True
    promo_campaign: str = "plus-1-month-free"
    token: str = "sandbox-token"
    plan: Literal["plus"] = "plus"
    entry_proxies: list[str] = Field(default_factory=list)
    exit_proxies: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=10, ge=1, le=50)
    use_sen: bool = True
    use_so: bool = True


@router.post("/checkout", status_code=201)
def create_sandbox_checkout(payload: SandboxCheckoutInput) -> dict:
    if not payload.use_promo or payload.promo_campaign != "plus-1-month-free":
        raise HTTPException(409, "sandbox promotion is not active")
    task_id = secrets.token_hex(6)
    ba_token = "BA-SANDBOX-" + secrets.token_hex(12).upper()
    task = {
        "task_id": task_id,
        "status": "approved",
        "stage": "completed",
        "email": payload.email,
        "country": payload.country,
        "currency": payload.currency,
        "amount": str(Decimal("0.00")),
        "use_promo": True,
        "promo_campaign": payload.promo_campaign,
        "ba_token": ba_token,
        "provider_url": (
            "http://127.0.0.1:8000/sandbox/paypal/agreements/approve"
            f"?ba_token={ba_token}"
        ),
        "sandbox": True,
    }
    _tasks[task_id] = task
    return {**task, "job_id": task_id, "queue_position": 0, "internal": True}


@router.get("/checkout-progress")
def sandbox_checkout_progress(job_id: str) -> dict:
    task = _tasks.get(job_id)
    if task is None:
        raise HTTPException(404, "sandbox checkout not found")
    return {
        "job_id": job_id,
        "status": "completed",
        "progress": 100,
        "text": "sandbox promotion applied; PayPal agreement approved",
        "result": task,
    }


@router.get("/checkout/{task_id}")
def get_sandbox_checkout(task_id: str) -> dict:
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "sandbox checkout not found")
    return task


@router.get("/paypal/agreements/approve")
def approve_sandbox_agreement(ba_token: str) -> dict:
    task = next((item for item in _tasks.values() if item["ba_token"] == ba_token), None)
    if task is None:
        raise HTTPException(404, "sandbox BA token not found")
    return {
        "result": "approved",
        "task_id": task["task_id"],
        "amount": task["amount"],
        "currency": task["currency"],
        "sandbox": True,
    }
