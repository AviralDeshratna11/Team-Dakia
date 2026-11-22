"""RFQ threads run by an agent: write the request for quote, read the vendor's reply, follow up once
for anything missing, then record the quote or hand it to the founder.

Mail is a mock mailbox (rfq.db): during the hackathon a teammate plays the vendor and replies with
`python -m desk rfq reply <thread_id> "..."`. One thread per (request, part, vendor), so an RFQ is
never sent twice. Code checks every number the agent recorded appears in the vendor's own words.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import date, datetime, timezone

from pydantic import BaseModel

from desk import ROOT
from desk.agent import AgentResult, Tool, run_agent
from desk.schemas import PriceTier, Quote, Spec, Status

RFQ_DB = ROOT / "rfq.db"
MAX_FOLLOWUPS = 1


class RfqMessage(BaseModel):
    direction: str                   # out (us) | in (vendor)
    at: str
    subject: str
    body: str


class RfqThread(BaseModel):
    thread_id: str
    request_key: str
    line_id: str
    vendor: str
    vendor_domain: str
    vendor_email: str
    part: str                        # what we're asking for, in words
    qty: int
    deliver_to: str | None
    need_by: date | None
    status: str                      # NEW | SENT | REPLIED | FOLLOWED_UP | QUOTED | ESCALATED
    followups: int = 0
    messages: list[RfqMessage] = []
    quote: dict | None = None
    escalation: str | None = None
    agent_runs: list[dict] = []      # summaries of each agent run on this thread


class RfqQuote(BaseModel):
    currency: str                    # ISO code
    unit_price: float                # at our quantity
    moq: int | None
    lead_time_days: int | None
    ships_to_india: bool | None
    valid_until: str | None
    payment_terms: str | None
    vendor_words: str                # the exact sentence(s) from the vendor's email with the numbers


class RfqOutcome(BaseModel):
    action: str                      # SENT | QUOTED | FOLLOWED_UP | ESCALATE | WAITING
    quote: RfqQuote | None
    missing: list[str]               # what the vendor still hasn't told us
    note: str


SYSTEM = """You run request-for-quote email threads for a company in India, as "Sourcing Desk".
- Be brief, specific and professional. Ask for: unit price at our quantity (and price breaks), currency,
  MOQ, lead time, delivery to our city in India (and cost), payment terms, and how long the quote is valid.
- Never commit to buying or paying, never mention a budget, and never invent facts about us.
- When the vendor's reply has the price, currency and lead time, finish with action QUOTED and copy the
  numbers exactly as the vendor wrote them, with their exact words in vendor_words.
- If something important is missing and you may still follow up, send ONE email asking only for what's
  missing, then finish with action FOLLOWED_UP.
- If the vendor asks for something only the founder can decide (advance payment, NDA, custom work,
  samples, a call), or you can't follow up again, finish with action ESCALATE and say why in note."""


# ---------- storage ----------

def _db() -> sqlite3.Connection:
    db = sqlite3.connect(RFQ_DB)
    db.execute("CREATE TABLE IF NOT EXISTS threads (thread_id TEXT PRIMARY KEY, data TEXT, status TEXT, updated_at TEXT)")
    return db


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save(t: RfqThread) -> RfqThread:
    with _db() as db:
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?) ON CONFLICT(thread_id) DO UPDATE SET "
                   "data = excluded.data, status = excluded.status, updated_at = excluded.updated_at",
                   (t.thread_id, t.model_dump_json(), t.status, _now()))
    return t


def get(thread_id: str) -> RfqThread | None:
    with _db() as db:
        row = db.execute("SELECT data FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
    return RfqThread.model_validate_json(row[0]) if row else None


def threads(request_key: str | None = None) -> list[RfqThread]:
    with _db() as db:
        rows = db.execute("SELECT data FROM threads ORDER BY updated_at").fetchall()
    ts = [RfqThread.model_validate_json(r[0]) for r in rows]
    return [t for t in ts if request_key is None or t.request_key == request_key]


def open_thread(request_key: str, spec: Spec, vendor: str, vendor_domain: str, deliver_to: str | None,
                need_by: date | None, vendor_email: str | None = None) -> RfqThread:
    tid = hashlib.sha256(f"{request_key}|{spec.line_id}|{vendor_domain}".encode()).hexdigest()[:12]
    existing = get(tid)
    if existing:
        return existing
    part = f"{spec.description} ({spec.mpn}" + (f", {spec.maker}" if spec.maker else "") + ")" \
        + (f"; specs: {', '.join(f'{k}={v}' for k, v in spec.params.items())}" if spec.params else "")
    return save(RfqThread(thread_id=tid, request_key=request_key, line_id=spec.line_id, vendor=vendor,
                          vendor_domain=vendor_domain, vendor_email=vendor_email or f"sales@{vendor_domain}",
                          part=part, qty=spec.qty, deliver_to=deliver_to, need_by=need_by, status="NEW"))


def vendor_reply(thread_id: str, body: str) -> RfqThread:
    """The vendor (a teammate, in the hackathon) replies."""
    t = get(thread_id)
    if t is None:
        raise KeyError(f"no thread {thread_id}")
    subj = t.messages[-1].subject if t.messages else "RFQ"
    t.messages.append(RfqMessage(direction="in", at=_now(), subject=subj if subj.startswith("Re:") else f"Re: {subj}",
                                 body=body))
    t.status = "REPLIED"
    return save(t)


# ---------- the agent ----------

def run_thread(t: RfqThread) -> AgentResult | None:
    """Advance one thread if it's our move (NEW or REPLIED). Returns the agent run, or None."""
    if t.status not in ("NEW", "REPLIED"):
        return None
    sent: list[RfqMessage] = []

    def send_email(subject: str, body: str) -> dict:
        if t.status == "REPLIED" and t.followups + len(sent) >= MAX_FOLLOWUPS:
            return {"error": "no follow-ups left on this thread: finish with ESCALATE"}
        if sent:
            return {"error": "one email per turn of the thread: finish now"}
        sent.append(RfqMessage(direction="out", at=_now(), subject=subject, body=body))
        return {"sent": True, "to": t.vendor_email}

    tools = [Tool("send_email", f"Send an email to the vendor ({t.vendor_email}).",
                  {"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                   "required": ["subject", "body"]}, send_email)]
    history = "\n\n".join(f"[{m.direction.upper()} {m.at}] {m.subject}\n{m.body}" for m in t.messages) or "(none yet)"
    task = (f"Vendor: {t.vendor} <{t.vendor_email}>\nWe need: {t.qty} x {t.part}\nDeliver to: {t.deliver_to or 'India'}"
            f"\nNeeded by: {t.need_by or 'not stated'}\nFollow-ups used: {t.followups}/{MAX_FOLLOWUPS}\n\n"
            f"Thread so far:\n{history}\n\n"
            + ("Write and send the first RFQ email, then finish with action SENT." if t.status == "NEW"
               else "The vendor replied. Decide: QUOTED, FOLLOWED_UP (send one email first) or ESCALATE."))
    result = run_agent("rfq_agent", SYSTEM, task, tools, RfqOutcome, max_turns=4, credit_budget=0)

    t.messages += sent
    out = RfqOutcome.model_validate(result.report) if result.report else None
    t.agent_runs.append({"at": _now(), "stop": result.stop_reason, "action": out.action if out else None,
                         "note": out.note if out else None, "llm_calls": result.llm_calls})
    if out is None:
        t.status, t.escalation = "ESCALATED", f"agent didn't finish ({result.stop_reason})"
    elif t.status == "NEW":
        t.status = "SENT" if sent else "ESCALATED"
        t.escalation = None if sent else "agent didn't send the RFQ"
    elif out.action == "QUOTED" and out.quote:
        problem = _unsupported(out.quote, [m.body for m in t.messages if m.direction == "in"])
        if problem:
            t.status, t.escalation = "ESCALATED", f"quote not backed by the vendor's email: {problem}"
        else:
            t.status, t.quote = "QUOTED", out.quote.model_dump()
    elif out.action == "FOLLOWED_UP" and sent:
        t.status, t.followups = "FOLLOWED_UP", t.followups + 1
    else:
        t.status, t.escalation = "ESCALATED", out.note or "needs the founder"
    save(t)
    return result


def _unsupported(q: RfqQuote, vendor_texts: list[str]) -> str | None:
    """Every number the agent recorded must appear in what the vendor wrote."""
    text = " ".join(vendor_texts).replace(",", "")
    nums = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)}
    for name in ("unit_price", "moq", "lead_time_days"):
        v = getattr(q, name)
        if v is not None and float(v) not in nums:
            return f"{name}={v} isn't in the vendor's email"
    return None


def quote_from(t: RfqThread, spec: Spec, fx: dict) -> Quote | None:
    if t.status != "QUOTED" or not t.quote:
        return None
    rq = RfqQuote.model_validate(t.quote)
    q = Quote(line_id=t.line_id, vendor=t.vendor, vendor_domain=t.vendor_domain, url=f"rfq:{t.thread_id}",
              fetched_at=datetime.now(timezone.utc), status=Status.FOUND, found_by="rfq", mpn=spec.mpn,
              manufacturer=spec.maker, moq=rq.moq, lead_time_days=rq.lead_time_days, ships_to_india=rq.ships_to_india,
              tiers=[PriceTier(min_qty=rq.moq or 1, unit_price=rq.unit_price, currency=rq.currency.upper())],
              evidence=[f"vendor's email: {rq.vendor_words}"])
    return q
