"""Canonical runtime contracts; FastAPI exposes these through OpenAPI."""
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class RunState(StrEnum):
    CREATED = "CREATED"
    PARSING = "PARSING"
    WAITING_FOR_CLARIFICATION = "WAITING_FOR_CLARIFICATION"
    DISCOVERING = "DISCOVERING"
    VERIFYING = "VERIFYING"
    RESEARCHING = "RESEARCHING"
    DECIDING = "DECIDING"
    SCREENING = "SCREENING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_GAPS = "COMPLETED_WITH_GAPS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Spec(Contract):
    id: str = Field(default_factory=uid)
    description: str
    part_number: str | None = None
    parameters: dict[str, str] = Field(default_factory=dict)
    quantity: int | None = Field(default=None, gt=0)
    unit: str = "piece"
    substitutes: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    pack_version: str = "1"


class SourcingRequest(Contract):
    text: str = Field(min_length=5, max_length=10000)
    request_type: Literal["purchase", "shortlist"] = "purchase"
    category: str = Field(default="general", min_length=1, max_length=100)

    @field_validator("category", mode="before")
    @classmethod
    def route_category(cls, value):
        text = str(value or "").strip().lower()
        if "leather" in text: return "leather_goods"
        if "electronic" in text or text in {"pcb", "components", "bom"}: return "electronics"
        return value
    destination: str = Field(default="Bengaluru, India", min_length=2)
    deadline: datetime
    research_budget_minor: int = Field(default=200000, ge=0)
    approval_threshold_minor: int = Field(default=10000000, ge=0)
    currency: Literal["INR"] = "INR"
    specs: list[Spec] = Field(default_factory=list)
    shortlist_count: int = Field(default=3, ge=1, le=10)
    mode: Literal["fixture", "live"] = "fixture"
    llm_enabled: bool = False
    human_review_enabled: bool = True
    supplier_domains: list[str] = Field(default_factory=list, max_length=3)
    # Crustdata credits this run may spend; the founder can lower it mid-run. Default stays minimal (identity only).
    provider_credit_limit: Decimal = Field(default=Decimal("0.2"), ge=0, le=1000, decimal_places=2)
    scenario: Literal["normal", "missing_stock", "provider_timeout", "checkout_timeout", "checkout_crash"] = "normal"

    @model_validator(mode="after")
    def validate_deadline(self):
        if any(not re.fullmatch(r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}", d) for d in self.supplier_domains):
            raise ValueError("supplier_domains must be plain public domain names, without schemes, paths or ports")
        if self.deadline.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        if self.specs and len({s.id for s in self.specs}) != len(self.specs):
            raise ValueError("spec IDs must be unique")
        return self


class Evidence(Contract):
    id: str = Field(default_factory=uid)
    reference: str
    provider: str
    mode: Literal["fixture", "live"] = "fixture"
    observed_at: datetime = Field(default_factory=now)
    fact: str
    value: str
    status: Literal["verified", "unverified"] = "verified"


SupplierKind = Literal["manufacturer", "distributor", "retailer", "service_provider", "trader", "unknown"]


class Supplier(Contract):
    id: str
    canonical_id: str | None
    name: str
    aliases: list[str] = Field(default_factory=list)
    domain: str
    locations: list[str]
    kind: SupplierKind
    identity: Literal["resolved", "unverified"]
    # Trust-card facts computed from live company data (empty for fixtures).
    profile: dict[str, Any] = Field(default_factory=dict)
    contact: dict[str, str] | None = None


class SupplierCheck(Contract):
    supplier_id: str
    kind: str
    result: Literal["pass", "fail", "unverified", "not_applicable"]
    reason: str
    evidence_ids: list[str]


class ReviewedQuoteTerms(Contract):
    request_fingerprint: str
    total_minor: int = Field(gt=0, le=100_000_000_000, strict=True)
    destination: str
    reviewer: str
    supplier_kind: SupplierKind
    identity_reference: str


class SupplierQuoteSubmission(Contract):
    spec_id: str
    supplier_name: str = Field(min_length=2, max_length=160)
    supplier_domain: str = Field(max_length=253, pattern=r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$")
    supplier_kind: SupplierKind
    identity_reference: str = Field(min_length=5, max_length=1000)
    quote_reference: str = Field(min_length=3, max_length=1000)
    quote_excerpt: str = Field(min_length=10, max_length=8000)
    reviewer: str = Field(min_length=2, max_length=100)
    quantity: int = Field(gt=0, strict=True)
    unit: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=2, max_length=300)
    delivery_at: datetime
    expires_at: datetime
    total_minor: int = Field(gt=0, le=100_000_000_000, strict=True)
    includes_all_costs: Literal[True]
    requirements_and_availability_confirmed: Literal[True]
    authorized_distributor: bool = False
    manufacturer_verified: bool = False


class Quote(Contract):
    id: str
    supplier_id: str
    spec_id: str
    currency: str = "INR"
    # Integer paise per unit, indexed by minimum quantity.
    tiers: dict[int, int]
    quantity: int = Field(gt=0)
    moq: int = Field(default=1, gt=0)
    stock: int | None = Field(default=None, ge=0)
    shipping_minor: int | None = Field(default=None, ge=0)
    shipping_basis: Literal["shipment", "unit"] = "shipment"
    lead_days: int | None = Field(default=None, ge=0)
    delivery_at: datetime | None = None
    serves_destination: bool | None = None
    matches_spec: bool | None = None
    observed_at: datetime = Field(default_factory=now)
    expires_at: datetime
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["verified", "unverified"] = "verified"
    signals: dict[str, bool | None] = Field(default_factory=dict)
    reviewed_terms: ReviewedQuoteTerms | None = None


class Cost(Contract):
    currency: str = "INR"
    goods_minor: int | None
    shipping_minor: int | None
    duty_minor: int | None
    tax_minor: int | None
    total_minor: int
    unit_minor: str
    assumptions: str = "Demo rates; one shipment per quote, no cross-item consolidation"


class Candidate(Contract):
    quote_id: str
    supplier_id: str
    eligible: bool
    reason_codes: list[str]
    reliability: int
    coverage: int
    reliability_breakdown: dict[str, int]
    cost: Cost | None
    evidence_ids: list[str]


class Decision(Contract):
    spec_id: str
    selected_quote_id: str | None = None
    selected_supplier_ids: list[str] = Field(default_factory=list)
    outcome: Literal["selected", "no_eligible_supplier", "incomplete", "clarification_needed", "handled_by_human"]
    candidates: list[Candidate]
    evidence_ids: list[str]
    config_version: str
    explanation: str
    alternatives: list[str] = Field(default_factory=list)


class Clarification(Contract):
    question: str
    answer: str | None = None


class ResearchPurchase(Contract):
    id: str
    quote_id: str
    provider: str = "mock_paid_data"
    question: str
    quoted_minor: int
    quoted_credits: str | None = None
    rationale: str
    status: Literal["quoted", "reserved", "paid", "skipped", "released"]
    evidence_ids: list[str] = Field(default_factory=list)
    changed_decision: bool | None = None


class BudgetReservation(Contract):
    id: str
    run_id: str
    amount_minor: int
    status: Literal["reserved", "settled", "released"]


class LedgerEntry(Contract):
    id: str
    account: Literal["research", "orders"]
    amount_minor: int
    currency: str = "INR"
    description: str
    reference: str
    created_at: datetime


class Approval(Contract):
    plan_version: str
    order_id: str
    amount_minor: int = Field(ge=0)


class Order(Contract):
    id: str
    spec_id: str
    supplier_id: str
    quote_id: str
    plan_version: str
    amount_minor: int
    state: Literal["PLANNED", "APPROVED", "PAY_PENDING", "PAID", "RECONCILING", "FAILED"] = "PLANNED"
    idempotency_key: str
    payment_id: str | None = None


class RFQDraft(Contract):
    id: str
    supplier_id: str
    recipient: str | None = None
    subject: str
    body: str
    status: Literal["draft"] = "draft"


class WorkflowEvent(Contract):
    id: str = Field(default_factory=uid)
    time: datetime = Field(default_factory=now)
    state: RunState
    message: str


class HumanReviewTask(Contract):
    id: str = Field(default_factory=uid)
    run_id: str
    status: Literal["pending", "resolved", "cancelled"] = "pending"
    reason_code: str
    summary: str
    blockers: list[str] = Field(default_factory=list)
    spec_ids: list[str] = Field(default_factory=list)
    resume_state: RunState
    created_at: datetime = Field(default_factory=now)
    resolved_at: datetime | None = None
    reviewer: str | None = None
    resolution: Literal["retry", "evaluate_quotes", "manual_completion", "cancel"] | None = None
    note: str | None = None
    reference: str | None = None
    resolution_fingerprint: str | None = None


class HumanReviewResolution(Contract):
    reviewer: str = Field(min_length=2, max_length=100)
    action: Literal["retry", "evaluate_quotes", "manual_completion", "cancel"]
    note: str = Field(min_length=5, max_length=4000)
    reference: str | None = Field(default=None, max_length=1000)
    specs: list[Spec] | None = None
    request_text: str | None = Field(default=None, min_length=5, max_length=10000)
    supplier_domains: list[str] | None = None
    provider_credit_limit: Decimal | None = Field(default=None, ge=0, le=1000, decimal_places=2)
    destination: str | None = Field(default=None, min_length=2)
    deadline: datetime | None = None


class SupplierSearchReport(Contract):
    spec_id: str
    status: Literal["completed", "skipped", "unavailable"]
    message: str
    query: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    supplier_ids: list[str] = Field(default_factory=list)


class ProductPageDetail(Contract):
    field: Literal["product_name", "part_number", "specifications", "price", "stock", "minimum_order_quantity", "shipping", "delivery"]
    value: str = Field(min_length=1, max_length=400)
    excerpt: str = Field(min_length=1, max_length=500)


class ProductPageReading(Contract):
    supplier_id: str
    spec_id: str
    url: str
    details: list[ProductPageDetail] = Field(max_length=12)


class WorkflowRun(Contract):
    id: str = Field(default_factory=uid)
    request: SourcingRequest
    state: RunState = RunState.CREATED
    specs: list[Spec] = Field(default_factory=list)
    suppliers: list[Supplier] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    checks: list[SupplierCheck] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    research: list[ResearchPurchase] = Field(default_factory=list)
    orders: list[Order] = Field(default_factory=list)
    rfqs: list[RFQDraft] = Field(default_factory=list)
    clarification: Clarification | None = None
    plan_version: str | None = None
    events: list[WorkflowEvent] = Field(default_factory=list)
    stage_attempts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    provider_usage: list[dict[str, str | int | None]] = Field(default_factory=list)
    llm_usage: list[dict[str, str | int | None]] = Field(default_factory=list)
    request_interpretation: dict[str, Any] | None = None
    supplier_insights: list[dict[str, Any]] = Field(default_factory=list)
    supplier_search: SupplierSearchReport | None = None
    product_page_readings: list[ProductPageReading] = Field(default_factory=list)
    extraction_warnings: list[str] = Field(default_factory=list)
    human_reviews: list[HumanReviewTask] = Field(default_factory=list)
    human_completed_spec_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)


class CategoryPack(Contract):
    name: str
    version: str
    spec_fields: list[str]
    required_fields: list[str]
    clarification_rules: list[str]
    substitution_constraints: list[str]
    discovery_hints: list[str]
    trust_signals: list[str]
    allowed_kinds: list[str]
    weights: dict[str, int]
    threshold: int = Field(ge=0, le=100)
    freshness_hours: int = Field(gt=0)
    ordering_method: Literal["checkout", "rfq"]
    duty_bps: int = Field(ge=0)
    tax_bps: int = Field(ge=0)
    tax_base: Literal["goods_shipping_duty"]
    fx_to_inr: dict[str, str]
    research_cost_minor: int = Field(gt=0)
    research_max_order_bps: int = Field(gt=0)
    cost_policy_confirmed: bool = True
    # Live discovery: "web_search" (catalogue pages) or "company_search" (company graph first).
    live_discovery: Literal["web_search", "company_search"] = "web_search"
    search_query: str = ""
    max_candidates: int = Field(default=8, ge=1, le=25)
    enrich_fields: list[str] = Field(default_factory=lambda: ["basic_info", "headcount", "locations", "taxonomy", "people", "news", "revenue"])
    relevance_keywords: list[str] = Field(default_factory=list)
    relevance_naics: list[str] = Field(default_factory=list)
    production_job_terms: list[str] = Field(default_factory=list)
    # A signal proven False here rejects the supplier outright (a red flag is not offset by points elsewhere).
    hard_fail_signals: list[str] = Field(default_factory=list)
    # Screen finalists (who runs it, adverse media, front-company flags) before any RFQ or order.
    compliance_screen: bool = False
    sales_job_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def weights_sum(self):
        if sum(self.weights.values()) != 100:
            raise ValueError("reliability weights must sum to 100")
        return self
