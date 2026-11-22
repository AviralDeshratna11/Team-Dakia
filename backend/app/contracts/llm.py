"""Narrow extraction contracts: no tools, spending decisions or identity overrides."""
from typing import Literal
from pydantic import Field
from app.contracts.models import Contract


class Parameter(Contract):
    name: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=200)


class ParsedItem(Contract):
    description: str = Field(min_length=1, max_length=500)
    part_number: str | None = None
    parameters: list[Parameter] = Field(default_factory=list, max_length=30)
    quantity: int | None = Field(default=None, gt=0)
    quantity_excerpt: str | None = None
    unit: str = "piece"
    substitutes: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class ParsedIntent(Contract):
    summary: str = Field(max_length=2000)
    category: Literal["electronics", "leather_goods"] | None = None
    request_type: Literal["purchase", "shortlist"] | None = None
    destination: str | None = None
    delivery_requirement: str | None = None
    research_budget_requirement: str | None = None
    items: list[ParsedItem] = Field(max_length=30)
    clarification_question: str | None = None


FactField = Literal["supplier_kind", "location", "unit_price_minor", "currency", "stock", "moq",
                    "shipping_minor", "shipping_basis", "lead_days", "delivery_at", "serves_destination",
                    "matches_spec", "authorized", "factory", "stable", "complaints_clear"]


class ExtractedFact(Contract):
    spec_id: str | None = None
    field: FactField
    value: str
    evidence_id: str
    excerpt: str = Field(min_length=5, max_length=1500)


class SupplierInsight(Contract):
    supplier_id: str
    summary: str = Field(max_length=2000)
    evidence_ids: list[str] = Field(max_length=30)
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=50)
    missing_information: list[str] = Field(default_factory=list)


class SupplierExtraction(Contract):
    insights: list[SupplierInsight] = Field(max_length=20)
