"""Render the outgoing RazorpayX UPI request for an existing synthetic order."""
from uuid import UUID


def upi_payout_preview(order):
    if order.state not in {"APPROVED", "PAY_PENDING", "RECONCILING", "PAID"}:
        raise ValueError("Approve the synthetic order before previewing the payout")
    if order.amount_minor < 100:
        raise ValueError("RazorpayX UPI payouts require at least 100 paise")
    provider_key = str(UUID(hex=order.idempotency_key[:32]))
    return {
        "mode": "preview",
        "method": "POST",
        "path": "/v1/payouts",
        "supplier_ref": order.supplier_id,
        "headers": {"X-Payout-Idempotency": provider_key},
        "body": {
            "account_number": "<RazorpayX funding account>",
            "fund_account_id": "<verified supplier VPA fund account>",
            "amount": order.amount_minor,
            "currency": "INR",
            "mode": "UPI",
            "purpose": "vendor bill",
            "queue_if_low_balance": False,
            "reference_id": provider_key,
        },
    }
