"""Shared test setup: no network, no credits, no LLM. Every database goes to a temp dir."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from desk import orders, payments, rfq, steps, trace
from desk.crustdata import CrustdataError
from desk.schemas import FindResult, PriceTier, Quote, Spec, Status


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "ORDERS_DB", tmp_path / "desk.db")
    monkeypatch.setattr(payments, "PAYMENTS_DB", tmp_path / "payments.db")
    monkeypatch.setattr(rfq, "RFQ_DB", tmp_path / "rfq.db")
    monkeypatch.setattr(trace, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.delenv("DESK_CRASH_AT", raising=False)
    monkeypatch.delenv("DESK_PAY_FAIL", raising=False)
    trace.CALL_LOG.clear()


@pytest.fixture
def pack():
    return steps.load_pack("electronics")


@pytest.fixture
def spec():
    return Spec(line_id="ldo", description="3.3V LDO", mpn="AP2112K-3.3TRG1", maker="Diodes Incorporated",
                params={}, qty=500)


def quote(vendor="LCSC", domain="lcsc.com", price_inr=10.0, stock=10_000, mpn="AP2112K-3.3TRG1",
          maker="Diodes Incorporated", **kw) -> Quote:
    q = Quote(line_id="ldo", vendor=vendor, vendor_domain=domain, url=f"https://{domain}/p/{mpn}",
              fetched_at=datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc), status=Status.FOUND, mpn=mpn,
              manufacturer=maker, stock=stock, moq=1,
              tiers=[PriceTier(min_qty=1, unit_price=price_inr, currency="INR")], **kw)
    return q


def find_with(*quotes: Quote, line_id="ldo") -> FindResult:
    return FindResult(line_id=line_id, mpn="AP2112K-3.3TRG1", maker="Diodes Incorporated", status=Status.FOUND,
                      reason="test", quotes=list(quotes), alternatives=[], leads=[])


class FakeCrustdata:
    """Crustdata with canned answers. companies: domain -> dict(id, name, founded, headcount, yoy, visitors)."""

    def __init__(self, companies=None, jobs=None, people=None, fail_identify=False, makers=None):
        self.companies = companies or {}
        self.jobs = jobs or {}
        self.people = people or {}
        self.fail_identify = fail_identify
        self.makers = makers or []
        self.calls = []

    def identify(self, domain):
        self.calls.append(("identify", domain))
        if self.fail_identify:
            raise CrustdataError("/company/identify failed after 4 tries (429, waited 5s)")
        c = self.companies.get(domain)
        if not c:
            return []
        return [{"confidence_score": 1.0, "company_data": {"crustdata_company_id": c["id"], "basic_info": {
            "name": c["name"], "year_founded": c.get("founded", 1990), "employee_count_range": "1001-5000"}}}]

    def enrich(self, company_id, fields):
        self.calls.append(("enrich", company_id))
        c = next((c for c in self.companies.values() if c["id"] == company_id), None) or \
            next((m for m in self.makers if m["id"] == company_id), {})
        return {"basic_info": {"name": c.get("name"), "year_founded": c.get("founded", 1990)},
                "headcount": {"total": c.get("headcount", 500), "growth_percent": {"yoy": c.get("yoy", 2.0)}},
                "locations": {"headquarters": c.get("hq", "Somewhere, India")},
                "web_traffic": {"domain_traffic": {"x": {"monthly_visitors": c.get("visitors", 100_000)}}}}

    def job_search(self, company_id, limit=25):
        return [{"job_details": {"title": t}} for t in self.jobs.get(company_id, [])]

    def person_search(self, company_id, limit=5):
        return [{"basic_profile": {"name": n, "current_title": t, "location": {"country": "India"}}}
                for n, t in self.people.get(company_id, [])]

    def company_search(self, query, filters=None, limit=10):
        return [{"crustdata_company_id": m["id"], "basic_info": {"name": m["name"], "primary_domain": m.get("domain")},
                 "locations": {"headquarters": m.get("hq", "Kanpur, Uttar Pradesh, India")}} for m in self.makers][:limit]


@pytest.fixture
def real_sellers():
    return {"lcsc.com": {"id": 1, "name": "LCSC Electronics", "founded": 2012},
            "digikey.com": {"id": 2, "name": "DigiKey", "founded": 1972}}
