from backend.sandbox_payment_state import reconcile_zero_gbp


def test_reconcile_approved_and_zero_gbp_observations():
    result = reconcile_zero_gbp(
        {"taskId": "ab3dd9033332", "status": "cancelled", "amount": "0.00", "currency": "GBP"},
        {"taskId": "4495372309be", "status": "approved", "amount": "0.00", "currency": "GBP"},
    )
    assert result["status"] == "completed"
    assert result["approved"] is True
    assert result["zeroGbp"] is True


def test_nonzero_approval_is_rejected():
    result = reconcile_zero_gbp(
        {"taskId": "ab3dd9033332", "status": "cancelled", "amount": 0, "currency": "GBP"},
        {"taskId": "4495372309be", "status": "approved", "amount": 1, "currency": "GBP"},
    )
    assert result["status"] == "rejected"
    assert result["zeroGbp"] is False
