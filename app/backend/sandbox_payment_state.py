"""Deterministic local payment-state reconciliation for the CTF sandbox.

This module never calls a provider and never fabricates a provider approval.
It combines independent observations only for fixture/state-machine testing.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _amount(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def reconcile_zero_gbp(*observations: dict[str, Any]) -> dict[str, Any]:
    """Reconcile sandbox observations for one logical task.

    Approval comes from an ``approved`` observation; the zero amount comes
    from any observation for the same logical task.  A cancelled observation
    is retained as evidence and never treated as approval.
    """
    approved = next(
        (item for item in observations if str(item.get("status", "")).lower() == "approved"),
        None,
    )
    zero = next(
        (
            item for item in observations
            if str(item.get("currency", "")).upper() == "GBP"
            and _amount(item.get("amount")) == Decimal("0.00")
        ),
        None,
    )
    if approved is None:
        return {"status": "cancelled", "approved": False, "zeroGbp": bool(zero), "reason": "no_approval"}
    approved_currency = str(approved.get("currency", "")).upper()
    approved_amount = _amount(approved.get("amount"))
    if zero is None or approved_currency != "GBP" or approved_amount != Decimal("0.00"):
        return {
            "status": "rejected",
            "approved": True,
            "zeroGbp": False,
            "reason": "approved_amount_mismatch",
            "approvedTaskId": approved.get("taskId"),
        }
    return {
        "status": "completed",
        "approved": True,
        "zeroGbp": True,
        "currency": "GBP",
        "amount": "0.00",
        "approvedTaskId": approved.get("taskId"),
        "zeroAmountTaskId": zero.get("taskId"),
        "source": "local-sandbox-reconciliation",
    }
