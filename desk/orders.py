"""Step 6: place and pay each order exactly once, even across crashes and re-runs.

Orders live in desk.db and move PLANNED -> APPROVED -> PAY_PENDING -> PAID, saved at every step.
The idempotency key depends only on what is being bought (request, line, vendor, part, qty), so a
re-run finds the same order and resumes from its saved state. Payment goes through the payment provider
(payments.py: a mock gateway + agent wallet), which never charges the same key twice.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime
from math import isfinite

from desk import ROOT, payments
from desk.schemas import Decision, Order

ORDERS_DB = ROOT / "desk.db"


class SimulatedCrash(RuntimeError):
    """Raised on purpose (DESK_CRASH_AT=after_charge) to test that a re-run never pays twice."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _orders_db() -> sqlite3.Connection:
    db = sqlite3.connect(ORDERS_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS orders (idempotency_key TEXT PRIMARY KEY, data TEXT NOT NULL,
                  state TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    return db


def get_order(key: str) -> Order | None:
    with _orders_db() as db:
        row = db.execute("SELECT data FROM orders WHERE idempotency_key = ?", (key,)).fetchone()
    return Order.model_validate_json(row[0]) if row else None


def list_orders(request_key: str | None = None) -> list[Order]:
    with _orders_db() as db:
        rows = db.execute("SELECT data FROM orders ORDER BY updated_at").fetchall()
    orders = [Order.model_validate_json(r[0]) for r in rows]
    return [o for o in orders if request_key is None or o.request_key == request_key]


def _save(o: Order, event: str) -> Order:
    o.events.append(f"{_now()} {o.state}: {event}")
    with _orders_db() as db:
        db.execute("INSERT INTO orders VALUES (?, ?, ?, ?) ON CONFLICT(idempotency_key) DO UPDATE SET "
                   "data = excluded.data, state = excluded.state, updated_at = excluded.updated_at",
                   (o.idempotency_key, o.model_dump_json(), o.state, _now()))
    return o


def approve(key: str) -> Order:
    """The founder approves a held order. The next run pays it."""
    o = get_order(key)
    if o is None:
        raise KeyError(f"no order {key}")
    if o.state != "PLANNED":
        return o
    o.state, o.held_reason = "APPROVED", None
    return _save(o, "approved by the founder")


# ---------- what was actually charged (the payment provider's records) ----------

def checkout_payments() -> list[dict]:
    """Every payment intent at the provider: one per order, however often the pipeline re-runs."""
    return [{"intent": i.id, "idempotency_key": i.idempotency_key, "payee": i.payee, "method": i.method,
             "amount_inr": i.amount_inr, "status": i.status, "failure": i.failure_reason, "at": i.created_at}
            for i in payments.provider().intents()]


# ---------- step 6 ----------

def order_key(request_key: str, decision: Decision) -> str:
    q = decision.picked
    raw = f"{request_key}|{decision.line_id}|{q.vendor_domain}|{q.mpn}|{q.buy_qty}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def place_order(decision: Decision, request_key: str, approval_limit_inr: float,
                method: str = "card") -> Order | None:
    """method: card (vendor checkout) or bank_transfer (RFQ vendors are paid against their invoice)."""
    if decision.status != "PICKED" or decision.picked is None:
        return None
    if decision.total_inr is None or not isfinite(decision.total_inr) or decision.total_inr < 0:
        raise ValueError("Order total must be finite and non-negative")
    q = decision.picked
    key = order_key(request_key, decision)
    o = get_order(key)
    if o is None:
        o = _save(Order(idempotency_key=key, request_key=request_key, line_id=decision.line_id, vendor=q.vendor,
                        vendor_domain=q.vendor_domain, mpn=q.mpn, qty=q.buy_qty, amount_inr=decision.total_inr,
                        state="PLANNED"), f"planned from decision: {decision.reason[:120]}")

    if o.state == "PAID":
        return o
    if o.state == "PLANNED":
        if o.amount_inr > approval_limit_inr:
            o.held_reason = f"₹{o.amount_inr:,.2f} is above the approval limit of ₹{approval_limit_inr:,.0f}"
            return _save(o, f"held for approval ({o.held_reason})")
        o.state = "APPROVED"
        _save(o, f"auto-approved: under the ₹{approval_limit_inr:,.0f} limit")
    if o.state == "APPROVED":
        o.state = "PAY_PENDING"
        _save(o, "sending payment to vendor checkout")
    if o.state == "PAY_PENDING":
        gw = payments.provider()
        intent, created = gw.create_intent(o.idempotency_key, o.amount_inr, o.vendor, method,
                                           {"order": o.idempotency_key, "line": o.line_id, "mpn": o.mpn})
        replayed = not created and intent.status == "succeeded"
        intent = gw.confirm(intent.id) if intent.status == "requires_confirmation" else \
            gw.retry(intent.id) if intent.status == "failed" else intent
        if intent.status == "failed":
            o.state, o.held_reason = "APPROVED", f"payment failed: {intent.failure_reason}"
            return _save(o, f"{gw.name} {intent.id} failed ({intent.failure_reason}); will retry on the next run")
        if os.environ.get("DESK_CRASH_AT") == "after_charge" and not replayed:
            raise SimulatedCrash(f"crashed after {gw.name} charged {intent.id}, before we recorded it")
        o.state, o.payment_id, o.held_reason = "PAID", intent.id, None
        _save(o, f"paid via {gw.name} {method} {intent.id}"
              + (" (provider returned the earlier payment for this key: not charged twice)" if replayed else ""))
    return o
