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
from contextlib import contextmanager
from datetime import datetime
from math import isfinite
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


@contextmanager
def _write():
    db = _db()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield db
    finally:
        db.close()


def _check_amount(amount: float) -> None:
    if not isfinite(amount) or amount < 0:
        raise ValueError("Amount must be finite and non-negative")


# ---------- the agent's wallet ----------

class AgentWallet:
    """Spend limits the agent can't exceed, whatever it decides."""

    @staticmethod
    def _get(db, key, default):
        row = db.execute("SELECT v FROM wallet WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT INTO wallet VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                   (key, json.dumps(value)))

    def _ensure(self, db):
        if self._get(db, "balance_inr", None) is None:
            balance = DEFAULT_WALLET["balance_inr"]
            self._set(db, "per_payment_limit_inr", DEFAULT_WALLET["per_payment_limit_inr"])
            self._set(db, "blocked_payees", [])
            self._set(db, "balance_inr", balance)
            self._ledger(db, "fund", balance, balance, None, "initial mock funding")

    @property
    def balance(self) -> float:
        with _write() as db:
            self._ensure(db)
            return self._get(db, "balance_inr", 0.0)

    @property
    def per_payment_limit(self) -> float:
        with _write() as db:
            self._ensure(db)
            return self._get(db, "per_payment_limit_inr", 0.0)

    def set_limit(self, inr: float) -> None:
        _check_amount(inr)
        with _write() as db:
            self._ensure(db)
            self._set(db, "per_payment_limit_inr", inr)

    def block(self, payee: str) -> None:
        with _write() as db:
            self._ensure(db)
            self._set(db, "blocked_payees", sorted(set(self._get(db, "blocked_payees", [])) | {payee}))

    def fund(self, inr: float, note: str = "founder top-up") -> None:
        with _write() as db:
            self._adjust(db, "fund", inr, None, note)

    def authorize(self, amount: float, payee: str, *, db=None) -> str | None:
        """None if the wallet allows this payment, else the reason it doesn't."""
        _check_amount(amount)
        if db is None:
            with _write() as connection:
                return self.authorize(amount, payee, db=connection)
        self._ensure(db)
        balance = self._get(db, "balance_inr", 0.0)
        limit = self._get(db, "per_payment_limit_inr", 0.0)
        if payee in self._get(db, "blocked_payees", []):
            return f"payee {payee} is blocked"
        if amount > limit:
            return f"₹{amount:,.2f} is over the wallet's per-payment limit of ₹{limit:,.0f}"
        if amount > balance:
            return f"insufficient funds: ₹{balance:,.2f} left, ₹{amount:,.2f} needed"
        return None

    def debit(self, amount: float, ref: str, note: str, *, db=None) -> None:
        if db is None:
            with _write() as connection:
                self.debit(amount, ref, note, db=connection)
            return
        self._adjust(db, "debit", amount, ref, note)

    def credit(self, amount: float, ref: str, note: str, *, db=None) -> None:
        if db is None:
            with _write() as connection:
                self.credit(amount, ref, note, db=connection)
            return
        self._adjust(db, "refund", amount, ref, note)

    def _adjust(self, db, kind, amount, ref, note):
        _check_amount(amount)
        self._ensure(db)
        delta = -amount if kind == "debit" else amount
        balance = round(self._get(db, "balance_inr", 0.0) + delta, 2)
        self._set(db, "balance_inr", balance)
        self._ledger(db, kind, delta, balance, ref, note)

    @staticmethod
    def _ledger(db, kind, amount, balance, ref, note):
        db.execute("INSERT INTO ledger (at, kind, amount_inr, balance_inr, ref, note) VALUES (?, ?, ?, ?, ?, ?)",
                   (_now(), kind, round(amount, 2), balance, ref, note))

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
        _check_amount(amount_inr)
        if amount_inr == 0:
            raise ValueError("Payment amount must be positive")
        amount_inr = round(amount_inr, 2)
        with _write() as db:
            row = db.execute("SELECT data FROM intents WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row:
                existing = Intent.model_validate_json(row[0])
                if (existing.amount_inr, existing.payee, existing.method) != (amount_inr, payee, method):
                    raise ValueError("Idempotency key reused with different payment details")
                return existing, False
            it = Intent(id="pi_" + uuid.uuid4().hex[:14], idempotency_key=idempotency_key, amount_inr=amount_inr,
                        payee=payee, method=method, status="requires_confirmation", metadata=metadata or {},
                        created_at=_now(), updated_at=_now())
            db.execute("INSERT INTO intents VALUES (?, ?, ?)", (it.id, idempotency_key, it.model_dump_json()))
            self._event(db, "payment_intent.created", it)
            return it, True

    def confirm(self, intent_id: str) -> Intent:
        return self._confirm(intent_id, retry=False)

    def retry(self, intent_id: str) -> Intent:
        """A failed intent can be confirmed again once the cause is fixed."""
        return self._confirm(intent_id, retry=True)

    def _confirm(self, intent_id, retry):
        with _write() as db:
            it = self.get(intent_id, db=db)
            if it is None:
                raise KeyError(intent_id)
            if it.status != "requires_confirmation" and not (retry and it.status == "failed"):
                return it
            _check_amount(it.amount_inr)

            # Older versions could commit the debit before saving the confirmation.
            prior_debits = db.execute("SELECT amount_inr FROM ledger WHERE kind = 'debit' AND ref = ?", (it.id,)).fetchall()
            if prior_debits and prior_debits != [(-it.amount_inr,)]:
                raise ValueError("Wallet debit history does not match the payment intent")
            reason = None
            if not prior_debits:
                reason = os.environ.get("DESK_PAY_FAIL") or self.wallet.authorize(it.amount_inr, it.payee, db=db)
                if not reason:
                    self.wallet.debit(it.amount_inr, it.id, f"{it.method} to {it.payee}", db=db)

            it.status, it.failure_reason = ("failed", reason) if reason else ("succeeded", None)
            self._save(it, db)
            self._event(db, "payment_intent.payment_failed" if reason else "payment_intent.succeeded", it)
            return it

    def refund(self, intent_id: str, amount_inr: float | None = None, reason: str = "") -> Intent:
        with _write() as db:
            it = self.get(intent_id, db=db)
            if it is None or it.status not in ("succeeded", "refunded"):
                raise ValueError(f"can't refund {intent_id}")
            requested = it.amount_inr if amount_inr is None else amount_inr
            _check_amount(requested)
            amount = round(min(requested, it.amount_inr - it.refunded_inr), 2)
            if not amount:
                return it
            it.refunded_inr = round(it.refunded_inr + amount, 2)
            it.status = "refunded" if it.refunded_inr == it.amount_inr else "succeeded"
            self.wallet.credit(amount, it.id, f"refund from {it.payee}: {reason}", db=db)
            self._save(it, db)
            self._event(db, "charge.refunded", it, {"amount_inr": amount, "reason": reason})
            return it

    def get(self, intent_id: str, *, db=None) -> Intent | None:
        if db is None:
            with _db() as connection:
                return self.get(intent_id, db=connection)
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

    @staticmethod
    def _save(it: Intent, db) -> None:
        it.updated_at = _now()
        db.execute("UPDATE intents SET data = ? WHERE id = ?", (it.model_dump_json(), it.id))

    @staticmethod
    def _event(db, type_: str, it: Intent, extra: dict | None = None) -> None:
        data = {"amount_inr": it.amount_inr, "payee": it.payee, "status": it.status,
                "failure_reason": it.failure_reason, **(extra or {})}
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
