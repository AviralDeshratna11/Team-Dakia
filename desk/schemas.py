"""The shared contract. Every step takes and returns these types."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel


# ---------- request ----------

class BomLine(BaseModel):
    line_id: str
    description: str                 # the founder's words
    mpn: str | None = None           # exact manufacturer part number, if known
    maker: str | None = None         # original manufacturer; same mpn from another maker = possible clone
    params: dict[str, str] = {}      # e.g. {"output_voltage": "3.3V"}
    qty_per_unit: int = 1


class Request(BaseModel):
    """Only `text` is required: step 1 reads the rest from it. Fields given here override it,
    and `lines` given here skip the LLM entirely (manual mode)."""
    text: str
    category: str | None = None
    units: int | None = None
    deliver_to: str | None = None
    deadline: date | None = None
    data_budget_inr: float | None = None
    approval_limit_inr: float = 50_000   # orders above this wait for the founder's approval
    lines: list[BomLine] = []
    answers: list[str] = []          # founder's answers to step 1's questions, e.g. "Q: ... A: ..."


# ---------- step 1: spec ----------

class PartCandidate(BaseModel):
    mpn: str
    maker: str | None = None


class Spec(BaseModel):
    line_id: str
    description: str
    part_type: str | None = None
    mpn: str                         # the part we search for first
    maker: str | None
    alternates: list[PartCandidate] = []   # tried if mpn finds nothing (one retry)
    params: dict[str, str]
    qty: int                         # units x qty_per_unit
    source: str = "founder"          # "founder" (given) or the model that suggested the mpn


class Question(BaseModel):
    line_id: str | None              # None = about the whole request
    question: str


class SpecResult(BaseModel):
    parsed_by: str                   # "manual" or the Gemini model
    category: str
    units: int | None
    deliver_to: str | None
    deadline: date | None
    data_budget_inr: float | None
    specs: list[Spec]                # ready for step 2
    questions: list[Question]        # lines held back until the founder answers
    assumptions: list[str]           # things the model assumed that the text didn't say


# ---------- step 2: find ----------

class Status(str, Enum):
    # ordered best -> worst; a part takes the best status among its quotes
    FOUND = "FOUND"                  # matching part, price at our qty, stock known
    FOUND_UNFIT = "FOUND_UNFIT"      # matching part, but e.g. not enough stock
    GAP = "GAP"                      # matching part, but price or stock missing
    BLOCKED = "BLOCKED"              # the site refused us
    NO_MATCH = "NO_MATCH"            # product pages found, none is our part
    NOT_FOUND = "NOT_FOUND"          # nothing came back


STATUS_RANK = list(Status)


class PriceTier(BaseModel):
    min_qty: int
    unit_price: float
    currency: str
    packaging: str | None = None


class Quote(BaseModel):
    line_id: str
    vendor: str
    vendor_domain: str
    url: str
    fetched_at: datetime | None
    status: Status
    note: str = ""
    extracted_by: str | None = None  # which extractor read the page
    cache_key: str | None = None     # open the raw page with desk.view.page(cache_key)
    mpn: str | None = None
    manufacturer: str | None = None
    package: str | None = None
    stock: int | None = None
    moq: int | None = None
    tiers: list[PriceTier] = []
    params: dict[str, str] = {}
    flags: list[str] = []            # e.g. possible clone: shown to the founder, never auto-picked
    found_by: str = "catalogue"      # "catalogue" (fixed recipe) or "agent" (investigated lead)
    seller_type: str | None = None   # from the agent: manufacturer, authorized_distributor, marketplace, ...
    ships_to_india: bool | None = None   # from the agent; catalogue vendors use the pack instead
    evidence: list[str] = []         # the agent's facts, each with its source
    buy_qty: int | None = None       # qty we'd actually buy (>= need, if MOQ forces it)
    unit_price: float | None = None  # at buy_qty, in the vendor's currency
    currency: str | None = None
    unit_price_inr: float | None = None


class Lead(BaseModel):
    """An unknown seller from open web search. Not quoted yet; step 3 checks it."""
    title: str
    url: str
    domain: str | None               # None when the URL is a Google redirect we can't resolve yet
    snippet: str


class FindResult(BaseModel):
    line_id: str
    mpn: str                         # the part number these quotes are for
    maker: str | None
    tried: list[str] = []            # "MPN: STATUS" for each part number searched
    status: Status
    reason: str
    quotes: list[Quote]              # quotes for our exact part (any status but NO_MATCH)
    alternatives: list[Quote]        # other parts seen on the way: substitute candidates
    leads: list[Lead]


# ---------- step 3: check ----------

class Signal(BaseModel):
    name: str                        # real_company, age, size, growth, web_traffic, authorized
    value: str | None                # what we saw, e.g. "founded 1972"
    ok: bool | None                  # None = couldn't check (unverified)
    weight: float
    source: str                      # where the value came from


class Check(BaseModel):
    vendor: str
    vendor_domain: str               # the storefront we quoted from
    seller_domain: str               # the company that actually sells (may differ, e.g. digikey.in -> digikey.com)
    company_id: int | None
    company_name: str | None
    status: str                      # VERIFIED | REJECTED | UNVERIFIED
    score: float | None              # weighted share of signals that passed, 0..1
    signals: list[Signal]
    reason: str


# ---------- step 5: decide ----------

class LandedCost(BaseModel):
    goods_inr: float
    shipping_inr: float
    duty_inr: float
    gst_inr: float
    total_inr: float
    per_unit_inr: float              # total / qty needed
    arrives_by: date | None
    placeholder_rates: bool          # True while pack rates are unverified placeholders


class Decision(BaseModel):
    line_id: str
    status: str                      # PICKED | NEEDS_FOUNDER | NONE
    rule: str
    picked: Quote | None
    landed: LandedCost | None = None
    total_inr: float | None
    reason: str
    founder_review: list[dict] = []  # flagged options the founder should see (e.g. cheaper possible clone)
    rejected: list[dict] = []        # vendors ruled out, with the rule that ruled them out
    considered: list[dict]
    substitutes: list[str] = []      # near-miss part numbers seen, when nothing qualifies


# ---------- step 6: order ----------

ORDER_STATES = ["PLANNED", "APPROVED", "PAY_PENDING", "PAID"]


class Order(BaseModel):
    idempotency_key: str             # same request + line + vendor + part + qty -> same key, across runs
    request_key: str
    line_id: str
    vendor: str
    vendor_domain: str
    mpn: str
    qty: int
    amount_inr: float
    state: str                       # PLANNED -> APPROVED -> PAY_PENDING -> PAID
    held_reason: str | None = None   # why it's waiting (e.g. above the approval limit)
    payment_id: str | None = None
    events: list[str] = []           # state history, oldest first
