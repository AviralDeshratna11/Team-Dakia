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


# ---------- step 3: check (stub) ----------

class Check(BaseModel):
    vendor: str
    vendor_domain: str
    status: str
    note: str


# ---------- step 5: decide ----------

class Decision(BaseModel):
    line_id: str
    status: str                      # PICKED | NEEDS_FOUNDER | NONE
    rule: str
    picked: Quote | None
    total_inr: float | None
    reason: str
    founder_review: list[dict] = []  # flagged options the founder should see (e.g. cheaper possible clone)
    considered: list[dict]


# ---------- step 6: order (stub) ----------

class Order(BaseModel):
    line_id: str
    vendor: str
    qty: int
    amount_inr: float
    state: str                       # PLANNED -> APPROVED -> PAY_PENDING -> PAID
    idempotency_key: str
