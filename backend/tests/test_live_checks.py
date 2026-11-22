"""Live supplier checks against a mocked Crustdata API: no keys, no credits."""
from datetime import timedelta, datetime, timezone
from decimal import Decimal
import json
import httpx
import pytest

from app.contracts import SourcingRequest, WorkflowRun, RunState as S, now
from app.domain.supplier_signals import assess_company, factory_verdict, stability_verdict
from app.persistence.database import Database
from app.persistence.repositories import save_run, get_run
from app.providers.crustdata import CrustdataClient
from app.workflow.orchestrator import Orchestrator, ACTIVE

TODAY = datetime(2026, 11, 22, tzinfo=timezone.utc)


def company(cid, name, *, naics=None, industry=None, state="Uttar Pradesh", hq="Kanpur, Uttar Pradesh, India", founded=2005,
            headcount=120, growth=4.0, news=None, people=None, description="Leather goods manufacturer"):
    return {"crustdata_company_id": cid, "updated_at": "2026-11-01T00:00:00Z",
            "basic_info": {"name": name, "primary_domain": f"{name.split()[0].lower()}.example", "year_founded": founded,
                           "status": "active", "description": description},
            "headcount": {"total": headcount, "growth_percent": {"six_months": growth}} if headcount else {},
            "locations": {"country": "India", "state": state, "headquarters": hq, "all_office_addresses": [hq]},
            "taxonomy": {"professional_network_industry": industry,
                         "primary_naics_detail": {"naics_code": naics, "industry": "Leather"} if naics else None},
            "news": news if news is not None else [{"article_title": f"{name} expands export unit", "article_url": "https://news.example/a"}],
            "people": people if people is not None else {"decision_makers": [{"basic_profile": {"name": "Asha Verma", "current_title": "Export Manager"}}]}}


CATALOG = {
    1: company(1, "Kanpur Hides Pvt Ltd", naics="316998"),
    2: company(2, "UP Leather Trading Co", naics="424990", industry="Wholesale", description="Traders of leather goods"),
    3: company(3, "Agra Leather Works", industry="Leather Product Manufacturing", hq="Agra, Uttar Pradesh, India"),
    4: company(4, "Unnao Tannery", naics="316110", founded=None, headcount=None, news=[]),
    5: company(5, "Chennai Leathers", naics="316998", state="Tamil Nadu", hq="Chennai, Tamil Nadu, India"),
    6: company(6, "Noida Bags Manufacturing", naics="316992", hq="Noida, Uttar Pradesh, India"),
}


class FakeCrustdata:
    def __init__(self, order, rate_limit_enrich_once=False):
        self.order, self.calls, self.rate_limit_once, self.sleeps = order, [], rate_limit_enrich_once, []

    def __call__(self, request):
        path = request.url.path
        if path == "/account/endpoints":
            return httpx.Response(200, json={"endpoints": [
                {"path": "/company/search", "status": "enabled", "base_credits": 0.03},
                {"path": "/company/enrich", "status": "enabled", "base_credits": 2},
                {"path": "/job/search", "status": "enabled", "base_credits": 0.03},
                {"path": "/web/search/live", "status": "enabled", "base_credits": 1}]})
        body = json.loads(request.content)
        self.calls.append((path, body))
        if path == "/company/search":
            rows = [{"crustdata_company_id": c, "basic_info": {"name": CATALOG[c]["basic_info"]["name"],
                     "primary_domain": CATALOG[c]["basic_info"]["primary_domain"]},
                     "locations": {"headquarters": CATALOG[c]["locations"]["headquarters"]}} for c in self.order]
            return httpx.Response(200, headers={"x-credits-used": str(0.03 * len(rows))}, json={"companies": rows, "next_cursor": None})
        if path == "/company/enrich":
            if self.rate_limit_once:
                self.rate_limit_once = False
                return httpx.Response(429, headers={"x-ratelimit-reset": "2"}, json={"error": "rate limited"})
            cid = body["crustdata_company_ids"][0]
            return httpx.Response(200, headers={"x-credits-used": "2"},
                                  json=[{"matched_on": str(cid), "match_type": "crustdata_company_id",
                                         "matches": [{"confidence_score": 1.0, "company_data": CATALOG[cid]}]}])
        if path == "/job/search":
            text = json.dumps(body)
            value = 4 if "machine operator" in text else 1
            return httpx.Response(200, headers={"x-credits-used": "0"}, json={"job_listings": [], "aggregations": [{"type": "count", "field": None, "value": value}]})
        if path == "/web/search/live":
            return httpx.Response(200, headers={"x-credits-used": "1"}, json={"success": True, "results": []})
        return httpx.Response(404, json={})


def system(tmp_path, fake):
    db = Database(str(tmp_path / "state.db"))
    client = CrustdataClient(key="unit-test-placeholder", journal_path=tmp_path / "credits.db", sleep=fake.sleeps.append,
                             client=httpx.Client(base_url="https://api.crustdata.com", transport=httpx.MockTransport(fake)))
    return db, Orchestrator(db, live_client=client), client


def leather_request(**changes):
    return SourcingRequest.model_validate({"text": "Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft the first email to each.",
        "request_type": "shortlist", "category": "leather_goods", "mode": "live", "deadline": now() + timedelta(days=21), **changes})


def run_until_done(db, worker, run_id, between=None):
    for step in range(200):
        with db.transaction() as c:
            run = get_run(c, run_id)
        if run.state not in ACTIVE:
            return run
        if between:
            between(step, run)
        worker.tick(run_id)
    pytest.fail("run did not finish")


def start(db, req):
    run = WorkflowRun(request=req)
    with db.transaction() as c:
        save_run(c, run)
    return run.id


# ---- signals -----------------------------------------------------------------
def test_factory_and_trader_verdicts_cite_their_reasons():
    assert factory_verdict(CATALOG[1], None)[0] is True
    verdict, _, against = factory_verdict(CATALOG[2], {"production": 0, "sales": 5})
    assert verdict is False and any("wholesale" in r.lower() for r in against)
    conflicted = company(9, "Mixed Co", naics="424990", industry="Leather Product Manufacturing")
    assert factory_verdict(conflicted, None)[0] is None  # conflicting records are never guessed


def test_stability_needs_evidence_and_catches_shrinking():
    assert stability_verdict(CATALOG[1], TODAY)[0] is True
    assert stability_verdict(CATALOG[4], TODAY)[0] is None
    assert stability_verdict(company(9, "Shrinking", growth=-40), TODAY)[0] is False


def test_assessment_region_contact_and_red_flags():
    report = assess_company(CATALOG[5], region="Uttar Pradesh", relevance_keywords=["leather"], relevance_naics=["316"], today=TODAY)
    assert report.region_match is False and report.in_country is True
    assert report.contact == {"name": "Asha Verma", "title": "Export Manager"}
    flagged = company(9, "Flagged", news=[{"article_title": "GST raid at Flagged tannery"}])
    assert assess_company(flagged, region=None, relevance_keywords=[], relevance_naics=[], today=TODAY).signals["complaints_clear"] is False


# ---- full live leather run -------------------------------------------------------
def test_live_leather_run_rejects_trader_buys_only_useful_news_and_drafts_named_rfqs(tmp_path):
    fake = FakeCrustdata(order=[1, 2, 3, 5, 4])
    db, worker, client = system(tmp_path, fake)
    rid = start(db, leather_request())
    run = run_until_done(db, worker, rid)

    assert run.state == S.COMPLETED, run.events[-1].message
    decision = run.decisions[0]
    selected = set(decision.selected_supplier_ids)
    assert selected == {"crustdata:1", "crustdata:3", "crustdata:4"}
    trader = next(c for c in decision.candidates if c.supplier_id == "crustdata:2")
    assert not trader.eligible and "SUPPLIER_TYPE_NOT_ELIGIBLE" in trader.reason_codes
    wrong_state = next(c for c in decision.candidates if c.supplier_id == "crustdata:5")
    assert "SPEC_MISMATCH_OR_UNVERIFIED" in wrong_state.reason_codes

    # Unnao Tannery had no age/headcount and no news: a 1-credit news check could qualify it, so it was bought.
    paid = [p for p in run.research if p.status == "paid"]
    assert len(paid) == 1 and "Unnao" in paid[0].question and paid[0].changed_decision is True
    assert all(p.status in {"paid", "skipped"} for p in run.research)

    # RFQs go to the named decision-maker, one per selected maker, never sent.
    assert len(run.rfqs) == 3 and all(r.status == "draft" for r in run.rfqs)
    assert all(r.recipient == "Asha Verma (Export Manager)" for r in run.rfqs)
    assert "Dear Asha," in run.rfqs[0].body

    usage = client.usage(rid)
    assert all(u["purpose"] for u in usage)
    spent = sum(Decimal(u["charged"]) for u in usage)
    assert spent <= Decimal(str(run.request.credit_budget))
    assert any(e.provider == "crustdata_company_enrich" and e.mode == "live" for e in run.evidence)


def test_stops_paying_once_the_shortlist_is_full(tmp_path):
    fake = FakeCrustdata(order=[1, 3, 6, 2, 5])
    db, worker, _ = system(tmp_path, fake)
    run = run_until_done(db, worker, start(db, leather_request()))
    enriched = [b["crustdata_company_ids"][0] for p, b in fake.calls if p == "/company/enrich"]
    assert enriched == [1, 3, 6]
    skipped = [c for c in run.checks if c.kind == "profile" and c.result == "not_applicable"]
    assert {c.supplier_id for c in skipped} == {"crustdata:2", "crustdata:5"}


def test_lowering_the_credit_ceiling_mid_run_finishes_with_gaps(tmp_path):
    fake = FakeCrustdata(order=[1, 2, 3, 5, 4])
    db, worker, client = system(tmp_path, fake)
    rid = start(db, leather_request())

    def cut_budget(step, run):
        if run.state == S.VERIFYING and any(c.kind == "profile" for c in run.checks) and run.request.credit_budget != 3:
            with db.transaction() as c:
                current = get_run(c, rid)
                current.request.credit_budget = 3
                save_run(c, current)

    run = run_until_done(db, worker, rid, between=cut_budget)
    assert run.state == S.COMPLETED_WITH_GAPS
    assert any("Credit ceiling reached" in c.reason for c in run.checks)
    assert sum(Decimal(u["charged"]) for u in client.usage(rid)) <= 3


def test_rate_limit_waits_for_reset_then_retries_without_double_charging(tmp_path):
    fake = FakeCrustdata(order=[1, 3, 6], rate_limit_enrich_once=True)
    db, worker, client = system(tmp_path, fake)
    rid = start(db, leather_request())
    run = run_until_done(db, worker, rid)
    assert fake.sleeps == [2.0]  # waited for x-ratelimit-reset
    assert run.state == S.COMPLETED and len(run.decisions[0].selected_supplier_ids) == 3
    enrich = [u for u in client.usage(rid) if u["path"] == "/company/enrich"]
    assert len(enrich) == 3 and all(u["state"] == "succeeded" for u in enrich)


def test_persistent_rate_limit_leaves_supplier_unverified_not_failed(tmp_path):
    fake = FakeCrustdata(order=[1, 3, 6])
    fake.rate_limit_once = True
    original = fake.__call__
    def always_limited(request):
        if request.url.path == "/company/enrich" and json.loads(request.content)["crustdata_company_ids"] == [3]:
            return httpx.Response(429, headers={"x-ratelimit-reset": "1"}, json={})
        return original(request)
    always_limited.sleeps = []
    db, worker, _ = system(tmp_path, always_limited)
    run = run_until_done(db, worker, start(db, leather_request()))
    assert run.state == S.COMPLETED_WITH_GAPS
    assert any(c.supplier_id == "crustdata:3" and c.result == "unverified" and "rate-limiting" in c.reason for c in run.checks)


def test_second_run_reuses_recent_company_profiles_for_free(tmp_path):
    fake = FakeCrustdata(order=[1, 3, 6])
    db, worker, client = system(tmp_path, fake)
    run_until_done(db, worker, start(db, leather_request()))
    second = start(db, leather_request(text="Find leather-goods manufacturers in Uttar Pradesh for belts"))
    run_until_done(db, worker, second)
    cached = [u for u in client.usage(second) if u["state"] == "cached"]
    assert len(cached) == 3 and all(Decimal(u["charged"]) == 0 for u in cached)


def test_red_flag_or_too_new_rejects_even_with_enough_points():
    from app.config import pack_for
    from app.contracts import Supplier, Quote
    from app.domain.decision_engine import assess
    pack = pack_for("leather_goods")
    supplier = Supplier(id="s", canonical_id="1", name="Flagged Tannery", domain="f.example", locations=[], kind="manufacturer", identity="resolved")
    base = dict(id="q", supplier_id="s", spec_id="x", tiers={}, quantity=1, matches_spec=True, serves_destination=True, expires_at=now() + timedelta(hours=1))
    red_flag = Quote(**base, signals={"identity": True, "factory": True, "stable": True, "complaints_clear": False})
    too_new = Quote(**base, signals={"identity": True, "factory": True, "stable": False, "complaints_clear": True})
    for quote, code in ((red_flag, "COMPLAINTS_CLEAR_FAILED"), (too_new, "STABLE_FAILED")):
        candidate = assess(quote, supplier, leather_request(), pack)
        assert candidate.reliability >= pack.threshold - 20 and not candidate.eligible and code in candidate.reason_codes
