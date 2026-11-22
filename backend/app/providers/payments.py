"""Contract for the workflow's payment provider and its settled receipts."""
from typing import Protocol, TypedDict


class PaymentReceipt(TypedDict):
    key: str
    amount: int
    payment_id: str
    account: str
    payee_ref: str | None


class InsufficientFunds(ValueError):
    """The provider definitively declined without moving funds."""


class PaymentProvider(Protocol):
    """Return a receipt only after settlement. Raise TimeoutError for an unknown outcome."""

    def lookup(self, key: str) -> PaymentReceipt | None: ...

    def pay(self, key: str, amount: int, account: str, fault: str | None = None,
            *, payee_ref: str | None = None) -> PaymentReceipt: ...
