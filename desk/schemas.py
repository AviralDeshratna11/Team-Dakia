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
    text: str
    category: str
    units: int
    deliver_to: str
    deadline: date
    data_budget_inr: float
    lines: list[BomLine]


# ---------- step 1: spec ----------

class Spec(BaseModel):
    line_id: str
    description: str
    mpn: str
    maker: str | None
    params: dict[str, str]
    qty: int                         # units x qty_per_unit


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
    rule: str
    picked: Quote | None
    total_inr: float | None
    reason: str
    considered: list[dict]


# ---------- step 6: order (stub) ----------

class Order(BaseModel):
    line_id: str
    vendor: str
    qty: int
    amount_inr: float
    state: str                       # PLANNED -> APPROVED -> PAY_PENDING -> PAID
    idempotency_key: str
