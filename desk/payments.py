"""Payments, mocked, shaped like a real gateway so a real one can drop in later.

- PaymentProvider: the interface orders use (create an intent, confirm it, refund, read events).
- MockGateway: implements it on SQLite (payments_mock.db), with Stripe/Razorpay-style semantics:
  idempotency keys (same key + amount -> the same intent; same key + different amount -> error),
  intent states requires_confirmation -> succeeded | failed, refunds, and an event log (the webhooks).
- AgentWallet: the money the agent may spend. The founder funds it; it enforces a per-payment limit and
  a payee blocklist, and keeps a ledger. The mock gateway charges it, so a payment it can't cover fails
  with a reason instead of going through.
- RazorpayProvider: a placeholder showing where a real provider plugs in (not implemented).

Pick the provider with DESK_PAYMENT_PROVIDER (default "mock").
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from desk import ROOT

PAYMENTS_DB = ROOT / "payments_mock.db"

DEFAULT_WALLET = {"balance_inr": 1_000_000.0, "per_payment_limit_inr": 500_000.0}


class Intent(BaseModel):
    id: str
    idempotency_key: str
    amount_inr: float
    payee: str
    method: str                      # card (vendor checkout) | bank_transfer (NEFT/RTGS/UPI to a vendor)
    status: str                      # requires_confirmation | succeeded | failed | refunded
    failure_reason: str | None = None
    refunded_inr: float = 0.0
    metadata: dict = {}
    created_at: str
    updated_at: str


class PaymentProvider(Protocol):
    name: str

    def create_intent(self, idempotency_key: str, amount_inr: float, payee: str, method: str,
                      metadata: dict | None = None) -> tuple[Intent, bool]: ...      # (intent, newly created)

    def confirm(self, intent_id: str) -> Intent: ...

    def refund(self, intent_id: str, amount_inr: float | None = None, reason: str = "") -> Intent: ...

    def get(self, intent_id: str) -> Intent | None: ...

    def find(self, idempotency_key: str) -> Intent | None: ...

    def events(self, since_id: int = 0) -> list[dict]: ...


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(PAYMENTS_DB)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, data TEXT);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, type TEXT,
                                           intent_id TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS wallet (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT,
                                           amount_inr REAL, balance_inr REAL, ref TEXT, note TEXT);
    """)
    return db


# ---------- the agent's wallet ----------

class AgentWallet:
    """Spend limits the agent can't exceed, whatever it decides."""

    def _get(self, k: str, default):
        with _db() as db:
            row = db.execute("SELECT v FROM wallet WHERE k = ?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, k: str, v) -> None:
        with _db() as db:
            db.execute("INSERT INTO wallet VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, json.dumps(v)))

    def _ensure(self) -> None:
        if self._get("balance_inr", None) is None:
            self._set("per_payment_limit_inr", DEFAULT_WALLET["per_payment_limit_inr"])
            self._set("blocked_payees", [])
            self._set("balance_inr", 0.0)
            self.fund(DEFAULT_WALLET["balance_inr"], "initial mock funding")

    @property
    def balance(self) -> float:
        self._ensure()
        return float(self._get("balance_inr", 0.0))

    @property
    def per_payment_limit(self) -> float:
        self._ensure()
        return float(self._get("per_payment_limit_inr", 0.0))

    def set_limit(self, inr: float) -> None:
        self._ensure()
        self._set("per_payment_limit_inr", inr)

    def block(self, payee: str) -> None:
        self._ensure()
        self._set("blocked_payees", sorted(set(self._get("blocked_payees", [])) | {payee}))

    def fund(self, inr: float, note: str = "founder top-up") -> None:
        bal = float(self._get("balance_inr", 0.0)) + inr
        self._set("balance_inr", bal)
        self._ledger("fund", inr, bal, None, note)

    def authorize(self, amount: float, payee: str) -> str | None:
        """None if the wallet allows this payment, else the reason it doesn't."""
        if payee in self._get("blocked_payees", []):
            return f"payee {payee} is blocked"
        if amount > self.per_payment_limit:
            return f"₹{amount:,.2f} is over the wallet's per-payment limit of ₹{self.per_payment_limit:,.0f}"
        if amount > self.balance:
            return f"insufficient funds: ₹{self.balance:,.2f} left, ₹{amount:,.2f} needed"
        return None

    def debit(self, amount: float, ref: str, note: str) -> None:
        bal = self.balance - amount
        self._set("balance_inr", bal)
        self._ledger("debit", -amount, bal, ref, note)

    def credit(self, amount: float, ref: str, note: str) -> None:
        bal = self.balance + amount
        self._set("balance_inr", bal)
        self._ledger("refund", amount, bal, ref, note)

    def _ledger(self, kind: str, amount: float, bal: float, ref: str | None, note: str) -> None:
        with _db() as db:
            db.execute("INSERT INTO ledger (at, kind, amount_inr, balance_inr, ref, note) VALUES (?, ?, ?, ?, ?, ?)",
                       (_now(), kind, round(amount, 2), round(bal, 2), ref, note))

    def ledger(self) -> list[dict]:
        with _db() as db:
            rows = db.execute("SELECT at, kind, amount_inr, balance_inr, ref, note FROM ledger ORDER BY id").fetchall()
        return [dict(zip(["at", "kind", "amount_inr", "balance_inr", "ref", "note"], r)) for r in rows]


# ---------- the mock gateway ----------

class MockGateway:
    name = "mock"

    def __init__(self, wallet: AgentWallet | None = None):
        self.wallet = wallet or AgentWallet()

    def create_intent(self, idempotency_key: str, amount_inr: float, payee: str, method: str,
                      metadata: dict | None = None) -> tuple[Intent, bool]:
        existing = self.find(idempotency_key)
        if existing:
            if abs(existing.amount_inr - amount_inr) > 0.01:
                raise ValueError(f"idempotency key {idempotency_key} reused with a different amount "
                                 f"({existing.amount_inr} vs {amount_inr})")
            return existing, False
        it = Intent(id="pi_" + uuid.uuid4().hex[:14], idempotency_key=idempotency_key, amount_inr=round(amount_inr, 2),
                    payee=payee, method=method, status="requires_confirmation", metadata=metadata or {},
                    created_at=_now(), updated_at=_now())
        with _db() as db:
            db.execute("INSERT INTO intents VALUES (?, ?, ?)", (it.id, idempotency_key, it.model_dump_json()))
        self._event("payment_intent.created", it)
        return it, True

    def confirm(self, intent_id: str) -> Intent:
        it = self.get(intent_id)
        if it is None:
            raise KeyError(intent_id)
        if it.status != "requires_confirmation":
            return it                    # already settled: confirming again is a no-op
        reason = os.environ.get("DESK_PAY_FAIL") or self.wallet.authorize(it.amount_inr, it.payee)
        if reason:
            it.status, it.failure_reason = "failed", reason
            self._save(it)
            self._event("payment_intent.payment_failed", it)
            return it
        self.wallet.debit(it.amount_inr, it.id, f"{it.method} to {it.payee}")
        it.status = "succeeded"
        self._save(it)
        self._event("payment_intent.succeeded", it)
        return it

    def retry(self, intent_id: str) -> Intent:
        """A failed intent can be confirmed again once the cause is fixed (e.g. wallet topped up)."""
        it = self.get(intent_id)
        if it and it.status == "failed":
            it.status, it.failure_reason = "requires_confirmation", None
            self._save(it)
        return self.confirm(intent_id)

    def refund(self, intent_id: str, amount_inr: float | None = None, reason: str = "") -> Intent:
        it = self.get(intent_id)
        if it is None or it.status not in ("succeeded", "refunded"):
            raise ValueError(f"can't refund {intent_id}")
        amount = min(amount_inr or it.amount_inr, it.amount_inr - it.refunded_inr)
        it.refunded_inr = round(it.refunded_inr + amount, 2)
        it.status = "refunded" if it.refunded_inr >= it.amount_inr - 0.01 else "succeeded"
        self._save(it)
        self.wallet.credit(amount, it.id, f"refund from {it.payee}: {reason}")
        self._event("charge.refunded", it, {"amount_inr": amount, "reason": reason})
        return it

    def get(self, intent_id: str) -> Intent | None:
        with _db() as db:
            row = db.execute("SELECT data FROM intents WHERE id = ?", (intent_id,)).fetchone()
        return Intent.model_validate_json(row[0]) if row else None

    def find(self, idempotency_key: str) -> Intent | None:
        with _db() as db:
            row = db.execute("SELECT data FROM intents WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return Intent.model_validate_json(row[0]) if row else None

    def intents(self) -> list[Intent]:
        with _db() as db:
            rows = db.execute("SELECT data FROM intents").fetchall()
        return sorted((Intent.model_validate_json(r[0]) for r in rows), key=lambda i: i.created_at)

    def events(self, since_id: int = 0) -> list[dict]:
        with _db() as db:
            rows = db.execute("SELECT id, at, type, intent_id, data FROM events WHERE id > ? ORDER BY id",
                              (since_id,)).fetchall()
        return [{"id": r[0], "at": r[1], "type": r[2], "intent_id": r[3], **json.loads(r[4])} for r in rows]

    def _save(self, it: Intent) -> None:
        it.updated_at = _now()
        with _db() as db:
            db.execute("UPDATE intents SET data = ? WHERE id = ?", (it.model_dump_json(), it.id))

    def _event(self, type_: str, it: Intent, extra: dict | None = None) -> None:
        data = {"amount_inr": it.amount_inr, "payee": it.payee, "status": it.status,
                "failure_reason": it.failure_reason, **(extra or {})}
        with _db() as db:
            db.execute("INSERT INTO events (at, type, intent_id, data) VALUES (?, ?, ?, ?)",
                       (_now(), type_, it.id, json.dumps(data)))


class RazorpayProvider:
    """Where a real provider plugs in. Same interface; maps to Razorpay Orders/Payments/Refunds:
    create_intent -> POST /v1/orders (receipt = idempotency key), confirm -> capture after checkout or
    a payout for bank transfers (RazorpayX Payouts with X-Payout-Idempotency), refund -> POST
    /v1/payments/{id}/refund, events -> webhooks (payment.captured, payment.failed, refund.processed)."""
    name = "razorpay"

    def __init__(self, *_, **__):
        raise NotImplementedError("real payments are out of scope for the hackathon; use DESK_PAYMENT_PROVIDER=mock")


def provider() -> PaymentProvider:
    name = os.environ.get("DESK_PAYMENT_PROVIDER", "mock")
    return {"mock": MockGateway, "razorpay": RazorpayProvider}[name]()
