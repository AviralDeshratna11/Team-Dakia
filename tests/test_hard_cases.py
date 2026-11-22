"""The brief's ten hard cases, as tests. Offline: fake Crustdata, fake Gemini, temp databases.

Run: uv run pytest -q
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from conftest import FakeCrustdata, find_with, quote

from desk import crustdata, orders, payments, pipeline, rfq, steps, understand
from desk.agent import AgentResult
from desk.check import check_suppliers
from desk.draft_pack import FACTORY_SIGNAL
from desk.schemas import Request, Status
from desk.trace import Run

DEADLINE = date(2027, 1, 31)


def priced(spec, pack, *qs):
    return find_with(*[steps.assess_quote(q, spec, pack) for q in qs])


def test_retry_after_debit_before_confirmation(pack, spec, real_sellers, monkeypatch):
    found = priced(spec, pack, quote())
    checks = check_suppliers([found], pack, FakeCrustdata(real_sellers))
    decision = steps.decide(spec, found, checks, pack, date.today() + timedelta(days=30))
    gateway = payments.MockGateway()
    starting_balance = gateway.wallet.balance
    save = gateway._save

    def crash_before_confirmation(intent, db):
        assert db.execute("SELECT count(*) FROM ledger WHERE kind = 'debit'").fetchone() == (1,)
        raise RuntimeError("interrupted before payment confirmation")

    monkeypatch.setattr(payments, "provider", lambda: gateway)
    monkeypatch.setattr(gateway, "_save", crash_before_confirmation)
    with pytest.raises(RuntimeError, match="interrupted"):
        orders.place_order(decision, "capture-crash", approval_limit_inr=50_000)
    assert gateway.wallet.balance == starting_balance
    assert [o.state for o in orders.list_orders()] == ["PAY_PENDING"]
    monkeypatch.setattr(gateway, "_save", save)
    paid = orders.place_order(decision, "capture-crash", approval_limit_inr=50_000)
    assert paid.state == "PAID"
    assert gateway.wallet.balance == round(starting_balance - paid.amount_inr, 2)
    assert len([e for e in gateway.wallet.ledger() if e["kind"] == "debit"]) == 1


def test_concurrent_confirmation_recovers_legacy_debit():
    gateway = payments.MockGateway()
    intent, _ = gateway.create_intent("legacy", 100, "LCSC", "card")
    gateway.wallet.debit(100, intent.id, "debit recorded by older gateway")
    balance_after_debit = gateway.wallet.balance
    gateway.wallet.set_limit(0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(gateway.confirm, (intent.id, intent.id)))
    assert [receipt.status for receipt in receipts] == ["succeeded", "succeeded"]
    assert gateway.wallet.balance == balance_after_debit
    assert len([e for e in gateway.wallet.ledger() if e["kind"] == "debit"]) == 1
    assert len([e for e in gateway.events() if e["type"] == "payment_intent.succeeded"]) == 1


def test_negative_quote_and_payment_are_rejected(spec, pack, real_sellers):
    found = priced(spec, pack, quote(price_inr=-5))
    checks = check_suppliers([found], pack, FakeCrustdata(real_sellers))
    decision = steps.decide(spec, found, checks, pack, None)
    assert decision.status == "NONE" and decision.picked is None
    gateway = payments.MockGateway()
    with pytest.raises(ValueError, match="non-negative"):
        gateway.create_intent("negative", -5, "LCSC", "card")
    assert gateway.intents() == []


@pytest.mark.parametrize("minimum, tier_minimum", [(600, 1), (1, 600)])
def test_stock_covers_effective_purchase_quantity(spec, pack, real_sellers, minimum, tier_minimum):
    supplier_quote = quote(stock=550)
    supplier_quote.moq = minimum
    supplier_quote.tiers[0].min_qty = tier_minimum
    found = priced(spec, pack, supplier_quote)
    assert supplier_quote.buy_qty == 600 and supplier_quote.status == Status.FOUND_UNFIT
    checks = check_suppliers([found], pack, FakeCrustdata(real_sellers))
    assert steps.decide(spec, found, checks, pack, None).status == "NONE"


def test_unknown_arrival_rejected_only_with_deadline(spec, pack, real_sellers):
    for distributor in pack["distributors"]:
        if distributor["domain"] == "lcsc.com":
            distributor["transit_days"] = None
    found = priced(spec, pack, quote())
    checks = check_suppliers([found], pack, FakeCrustdata(real_sellers))
    deadline = date.today() + timedelta(days=30)
    decision = steps.decide(spec, found, checks, pack, deadline)
    assert decision.picked is None and decision.rejected[0]["rule"] == "arrival date unknown"
    assert steps.decide(spec, found, checks, pack, None).status == "PICKED"


# 1. Clean BOM, reliable vendors in stock -> plan and paid orders, each pick cited
def test_01_clean_pick_is_cited_and_paid_once(pack, spec, real_sellers):
    f = priced(spec, pack, quote("LCSC", "lcsc.com", 8.0), quote("DigiKey India", "digikey.in", 14.0))
    checks = check_suppliers([f], pack, FakeCrustdata(real_sellers))
    d = steps.decide(spec, f, checks, pack, DEADLINE)
    assert d.status == "PICKED" and d.picked.vendor == "LCSC"
    assert "https://lcsc.com/p/AP2112K-3.3TRG1" in d.reason and "fetched 2026-11-22" in d.reason
    assert d.landed.total_inr > d.landed.goods_inr                      # shipping + duty + GST included
    o1 = orders.place_order(d, "req1", approval_limit_inr=50_000)
    o2 = orders.place_order(d, "req1", approval_limit_inr=50_000)
    assert o1.state == o2.state == "PAID" and o1.payment_id == o2.payment_id
    assert len(payments.provider().intents()) == 1


# 2. Cheapest seller is a storefront with no company record -> rejected, next best shown with the reason
def test_02_storefront_without_company_record_is_rejected(pack, spec, real_sellers):
    f = priced(spec, pack, quote("BestChips Store", "bestchips.example", 3.0), quote("LCSC", "lcsc.com", 8.0))
    checks = check_suppliers([f], pack, FakeCrustdata(real_sellers))
    bad = next(c for c in checks if c.vendor_domain == "bestchips.example")
    assert bad.status == "REJECTED" and "doesn't resolve to any company" in bad.reason
    d = steps.decide(spec, f, checks, pack, DEADLINE)
    assert d.picked.vendor == "LCSC"
    assert [r["rule"] for r in d.rejected] == ["not a real company"]


# 3. Same vendor under two names -> one company
def test_03_two_storefronts_one_company(pack, spec, real_sellers):
    f = priced(spec, pack, quote("DigiKey India", "digikey.in", 14.0), quote("DigiKey", "digikey.com", 15.0))
    checks = check_suppliers([f], pack, FakeCrustdata(real_sellers))
    assert {c.company_id for c in checks} == {2}                        # digikey.in -> digikey.com -> one id
    assert any("same company as DigiKey India" in c.reason for c in checks)


# 4. "Manufacturer" whose hiring shows it's a trader -> marked trader, lower reliability
def test_04_hiring_shows_trader(pack, spec):
    made_to_order = {**pack, "factory_signal": FACTORY_SIGNAL}
    sellers = {"realfactory.example": {"id": 10, "name": "Real Factory"},
               "fakefactory.example": {"id": 11, "name": "Fake Factory"}}
    cd = FakeCrustdata(sellers, jobs={10: ["Machine Operator", "QC Inspector"], 11: ["Sales Executive", "Export Manager"]})
    f = priced(spec, made_to_order, quote("Real Factory", "realfactory.example", 9.0),
               quote("Fake Factory", "fakefactory.example", 9.0))
    by = {c.vendor: c for c in check_suppliers([f], made_to_order, cd)}
    fac = {c: next(s for s in by[c].signals if s.name == "factory") for c in by}
    assert fac["Real Factory"].ok is True and fac["Fake Factory"].ok is False
    assert "likely a trader" in fac["Fake Factory"].value
    assert by["Fake Factory"].score < by["Real Factory"].score


# 5. Free data is missing stock levels -> a gap, never a silent pick (buying the data: step 4, deferred)
def test_05_missing_stock_is_a_gap_not_a_pick(pack, spec, real_sellers):
    q = steps.assess_quote(quote("LCSC", "lcsc.com", 5.0, stock=None), spec, pack)
    assert q.status == Status.GAP and q.note == "stock not on page"
    f = find_with(q, steps.assess_quote(quote("DigiKey India", "digikey.in", 14.0), spec, pack))
    d = steps.decide(spec, f, check_suppliers([f], pack, FakeCrustdata(real_sellers)), pack, DEADLINE)
    assert d.picked.vendor == "DigiKey India"                           # the cheaper but unverified-stock quote isn't picked


# 6. Founder cuts the budget mid-run -> stops buying, finishes, marks gaps unverified
@pytest.mark.skip(reason="step 4 (buying data within a budget) is deferred; see test_06b for the spending ceiling")
def test_06_data_budget_cut_mid_run():
    pass


def test_06b_wallet_limit_cut_mid_run_holds_payment(pack, spec, real_sellers):
    f = priced(spec, pack, quote("LCSC", "lcsc.com", 8.0))
    d = steps.decide(spec, f, check_suppliers([f], pack, FakeCrustdata(real_sellers)), pack, DEADLINE)
    payments.AgentWallet().set_limit(1_000)                             # founder lowers the ceiling
    o = orders.place_order(d, "req6", approval_limit_inr=50_000)
    assert o.state == "APPROVED" and "per-payment limit" in o.held_reason   # not paid, reason kept
    payments.AgentWallet().set_limit(100_000)
    o = orders.place_order(d, "req6", approval_limit_inr=50_000)
    assert o.state == "PAID" and len(payments.provider().intents()) == 1    # same intent, retried


# 7. Vendor page times out, or Crustdata returns 429 -> retry with backoff, then mark unverified
class _Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = code, body or {}, headers or {}, ""

    def json(self):
        return self._body


def test_07a_429_is_retried(monkeypatch):
    replies = iter([_Resp(429, headers={"x-ratelimit-reset": "1"}), _Resp(200, {"ok": 1}, {"x-credits-used": "1"})])
    monkeypatch.setattr(crustdata.httpx, "post", lambda *a, **k: next(replies))
    monkeypatch.setattr(crustdata.time, "sleep", lambda s: None)
    body, credits = crustdata.Crustdata(api_key="test", use_cache=False)._post("/x", {})
    assert body == {"ok": 1} and credits == 1.0


def test_07b_check_that_cannot_run_is_unverified_and_never_picked(pack, spec):
    f = priced(spec, pack, quote("LCSC", "lcsc.com", 8.0))
    checks = check_suppliers([f], pack, FakeCrustdata(fail_identify=True))
    assert checks[0].status == "UNVERIFIED" and "identify failed" in checks[0].reason
    d = steps.decide(spec, f, checks, pack, DEADLINE)
    assert d.status == "NEEDS_FOUNDER" and d.picked is None


# 8. Crash during checkout, then restart -> each order paid exactly once
def test_08_crash_after_charge_then_resume_pays_once(pack, spec, real_sellers, monkeypatch):
    f = priced(spec, pack, quote("LCSC", "lcsc.com", 8.0))
    d = steps.decide(spec, f, check_suppliers([f], pack, FakeCrustdata(real_sellers)), pack, DEADLINE)
    monkeypatch.setenv("DESK_CRASH_AT", "after_charge")
    with pytest.raises(orders.SimulatedCrash):
        orders.place_order(d, "req8", approval_limit_inr=50_000)
    monkeypatch.delenv("DESK_CRASH_AT")
    o = orders.place_order(d, "req8", approval_limit_inr=50_000)
    assert o.state == "PAID" and "not charged twice" in o.events[-1]
    debits = [e for e in payments.AgentWallet().ledger() if e["kind"] == "debit"]
    assert len(payments.provider().intents()) == 1 and len(debits) == 1


# 9. Vague spec ("a small regulator") -> one clarifying question, not a guess
def test_09_vague_spec_asks_once_then_proceeds(monkeypatch):
    prompts = []

    def fake_ask(prompt, schema):
        prompts.append(prompt)
        final = "final pass" in prompt
        line = {"line_id": "regulator", "description": "small regulator", "part_type": "regulator",
                "params": [{"name": "output_voltage", "value": "3.3V"}] if final else [], "qty_per_unit": 1,
                "candidates": [{"mpn": "AP2112K-3.3TRG1", "manufacturer": "Diodes Incorporated"}] if final else [],
                "clarifying_question": None if final else "What output voltage and current?"}
        return schema.model_validate({"category": "electronics", "units": 200, "deliver_to": "Pune",
                                      "deadline_date": None, "deadline_days": 30, "data_budget_inr": None,
                                      "lines": [line], "assumptions": [], "question": None})
    monkeypatch.setattr(understand.llm, "ask", fake_ask)
    asked = []
    req = Request(text="Need a small regulator for 200 boards, Pune, a month.")
    packs = steps.load_packs()

    assert pipeline.clarify_specs(Run("t9a"), req, packs, ask=None) is None        # no one to ask: stop, don't guess
    sr = pipeline.clarify_specs(Run("t9b"), req, packs, ask=lambda q: asked.append(q) or "3.3V, 500mA")
    assert len(asked) == 1 and asked[0].line_id == "regulator"
    assert [s.mpn for s in sr.specs] == ["AP2112K-3.3TRG1"] and not sr.questions
    assert "3.3V, 500mA" in prompts[-1] and "final pass" in prompts[-1]


# 10. Leather-goods request, same engine -> makers found, checked, RFQs drafted, quote picked
def test_10_leather_makers_found_checked_rfqd_and_quoted(monkeypatch):
    leather = steps.load_pack("leather_goods")
    from desk.schemas import Spec
    spec = Spec(line_id="sleeve", description="leather laptop sleeve", mpn="SLEEVE-15", maker=None, params={}, qty=300)
    makers = [{"id": 100, "name": "Kanpur Leather Works"}, {"id": 101, "name": "UP Bags"}]
    cd = FakeCrustdata(makers=makers, jobs={100: ["Stitching Operator"], 101: ["Cutting Supervisor"]})

    f = steps.find_suppliers(spec, leather, cd)
    assert [q.status for q in f.quotes] == [Status.GAP, Status.GAP] and {q.found_by for q in f.quotes} == {"search"}
    checks = check_suppliers([f], leather, cd)
    assert all(c.status == "VERIFIED" for c in checks)

    def fake_agent(name, system, task, tools, report, **kw):          # stands in for Gemini
        if "Write and send the first RFQ" in task:
            tools[0].fn(subject="RFQ: 300 leather sleeves", body="Please quote.")
            rep = {"action": "SENT", "quote": None, "missing": [], "note": ""}
        else:
            rep = {"action": "QUOTED", "missing": [], "note": "", "quote": {
                "currency": "INR", "unit_price": 1150, "moq": 100, "lead_time_days": 25, "ships_to_india": True,
                "valid_until": None, "payment_terms": "on delivery", "vendor_words": "INR 1150, MOQ 100, 25 days"}}
        return AgentResult(agent=name, task=task, finished=True, stop_reason="finished", report=rep, turns=1,
                           credits=0, llm_calls=1, llm_cached=0, transcript=[])
    monkeypatch.setattr(rfq, "run_agent", fake_agent)

    out = pipeline.rfq_step("req10", [spec], [f], checks, leather, "Bengaluru", DEADLINE)
    assert sorted(t["status"] for t in out) == ["SENT", "SENT"]
    first = rfq.threads("req10")[0]
    rfq.vendor_reply(first.thread_id, "Price INR 1150 per piece, MOQ 100, lead time 25 days, payment on delivery.")
    pipeline.rfq_step("req10", [spec], [f], checks, leather, "Bengaluru", DEADLINE)
    d = steps.decide(spec, f, checks, leather, DEADLINE)
    assert d.status == "PICKED" and d.picked.found_by == "rfq" and d.picked.unit_price_inr == 1150


# ---------- guarantees beyond the ten ----------

def test_clone_is_flagged_for_the_founder_never_picked(pack, spec, real_sellers):
    f = priced(spec, pack, quote("LCSC", "lcsc.com", 6.0, maker="TECH PUBLIC"), quote("LCSC", "lcsc.com", 8.0))
    d = steps.decide(spec, f, check_suppliers([f], pack, FakeCrustdata(real_sellers)), pack, DEADLINE)
    assert d.picked.manufacturer == "Diodes Incorporated"
    assert len(d.founder_review) == 1 and "possible clone" in d.founder_review[0]["flags"][0]


def test_rfq_numbers_must_appear_in_vendor_email():
    q = rfq.RfqQuote(currency="INR", unit_price=999, moq=100, lead_time_days=25, ships_to_india=True,
                     valid_until=None, payment_terms=None, vendor_words="...")
    assert "unit_price=999" in rfq._unsupported(q, ["Price INR 1150, MOQ 100, 25 days"])


def test_payment_key_reused_with_different_amount_is_refused():
    gw = payments.provider()
    gw.create_intent("k1", 100.0, "LCSC", "card")
    with pytest.raises(ValueError):
        gw.create_intent("k1", 120.0, "LCSC", "card")
