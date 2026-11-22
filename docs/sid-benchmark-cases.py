"""Offline procurement benchmark for sid's command-line workflow."""

import json
import sys
from copy import deepcopy
from datetime import date, timedelta

import httpx
import pytest
import yaml

from conftest import FakeCrustdata, find_with, quote
from desk import draft_pack, llm, orders, payments, pipeline, rfq, steps, trace
from desk.__main__ import main
from desk.check import check_suppliers
from desk.schemas import PriceTier, Status


@pytest.fixture(autouse=True)
def mock_services_only(monkeypatch):
    monkeypatch.setenv("DESK_PAYMENT_PROVIDER", "mock")

    def unexpected_provider(*_args, **_kwargs):
        pytest.fail("Benchmark attempted a live provider call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_provider)
    monkeypatch.setattr(llm, "client", unexpected_provider)


@pytest.fixture
def cli(monkeypatch, capsys):
    def invoke(*args):
        monkeypatch.setattr(sys, "argv", ["desk", *map(str, args)])
        main()
        return capsys.readouterr().out
    return invoke


class CatalogueFixture(FakeCrustdata):
    def __init__(self, companies, spec):
        super().__init__(companies)
        self.spec = spec

    def fetch(self, urls, **_kwargs):
        pages = []
        for url in urls:
            page = {"url": url, "timestamp": 1_795_334_400, "success": False}
            if "digikey.in" in url:
                data = {
                    "priceQuantity": {"qtyAvailable": "1,000", "pricing": [
                        {"minOrderQuantity": "1", "mergedPricingTiers": [{"brkQty": "1", "unitPrice": "10.00"}]},
                    ]},
                    "productOverview": {"manufacturerProductNumber": self.spec.mpn,
                                        "manufacturer": self.spec.maker},
                }
                embedded = {"props": {"pageProps": {"envelope": {"data": data}}}}
                html = '<script id="__NEXT_DATA__">' + json.dumps(embedded) + '</script>' + " " * 2200
                page.update(title="Fixture product", content=html, success=True)
            pages.append(page)
        return pages

    def search(self, _query):
        return []


def test_cli_purchase_approval_crash_resume_and_accounting(cli, monkeypatch, tmp_path, spec, real_sellers):
    monkeypatch.setattr(pipeline, "Crustdata", lambda **_: CatalogueFixture(real_sellers, spec))
    request = tmp_path / "benchmark.yaml"
    request.write_text(yaml.safe_dump({
        "text": "500 regulators for an offline checkout benchmark", "category": "electronics",
        "units": 500, "deliver_to": "Bengaluru, India", "deadline": date.today() + timedelta(days=10),
        "approval_limit_inr": 6489.99, "lines": [{"line_id": spec.line_id, "description": spec.description,
                                                  "mpn": spec.mpn, "maker": spec.maker}],
    }), encoding="utf-8")

    assert "ORDER [ldo] PLANNED" in cli("run", request, "--no-cache")
    held, = orders.list_orders()
    assert (held.amount_inr, held.vendor) == (6490.0, "DigiKey India")
    assert not payments.MockGateway().intents()
    records = trace.load_run()
    decision = next(r["output"] for r in records if r["step"] == "05_decide")
    assert decision["picked"]["url"].startswith("https://www.digikey.in/")
    assert "fetched " in decision["reason"]
    landed = decision["landed"]
    assert (landed["goods_inr"], landed["shipping_inr"], landed["duty_inr"], landed["gst_inr"]) == (5000, 0, 500, 990)

    wallet = payments.AgentWallet()
    before = wallet.balance
    assert "APPROVED" in cli("approve", held.idempotency_key)
    assert "CRASHED (on purpose)" in cli("run", request, "--crash-after-charge")
    assert orders.get_order(held.idempotency_key).state == "PAY_PENDING"
    monkeypatch.delenv("DESK_CRASH_AT")
    assert "ORDER [ldo] PAID" in cli("run", request)
    paid, = orders.list_orders()
    intent, = payments.MockGateway().intents()
    assert paid.payment_id == intent.id and intent.status == "succeeded"
    assert "PAID" in cli("orders") and "ORDER [ldo] PAID" in cli("show")
    assert "payment_intent.succeeded" in cli("wallet")
    assert "ORDER [ldo] PAID" in cli("run", request)
    debit, = [row for row in wallet.ledger() if row["kind"] == "debit"]
    assert (debit["amount_inr"], debit["ref"], wallet.balance) == (-6490.0, intent.id, before - 6490.0)


def test_cli_wallet_mailbox_and_pack_operations(cli, monkeypatch, tmp_path, spec):
    wallet = payments.AgentWallet()
    before = wallet.balance
    cli("wallet", "limit", 200)
    cli("wallet", "fund", 25)
    assert (wallet.balance, wallet.per_payment_limit) == (before + 25, 200)

    thread = rfq.open_thread("fixture", spec, "Fixture vendor", "vendor.example", "India", None)
    assert thread.thread_id in cli("rfq")
    assert "NEW" in cli("rfq", thread.thread_id)
    cli("rfq", "reply", thread.thread_id, "Fixture price is INR 20 per part.")
    reply = rfq.get(thread.thread_id)
    assert reply.status == "REPLIED" and reply.messages[-1].direction == "in"
    assert "Fixture price is INR 20" in cli("rfq", thread.thread_id)

    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "fixture.yaml").write_text("name: fixture\ndraft: true\n", encoding="utf-8")
    monkeypatch.setattr(draft_pack, "DRAFTS_DIR", drafts)
    monkeypatch.setattr(draft_pack, "PACKS_DIR", tmp_path)
    assert "pack fixture approved" in cli("pack", "approve", "fixture")
    assert yaml.safe_load((tmp_path / "fixture.yaml").read_text(encoding="utf-8")) == {"name": "fixture", "draft": True}
    assert payments.MockGateway().intents() == []


@pytest.mark.parametrize("stock, expected", [(499, "NONE"), (500, "PICKED"), (501, "PICKED")])
def test_stock_boundary(pack, spec, real_sellers, stock, expected):
    found = find_with(steps.assess_quote(quote(stock=stock), spec, pack))
    checks = check_suppliers([found], pack, FakeCrustdata(real_sellers))
    decision = steps.decide(spec, found, checks, pack, None)
    assert decision.status == expected


@pytest.mark.parametrize("delta, expected", [(-1, "NONE"), (0, "PICKED"), (1, "PICKED")])
def test_delivery_boundary(pack, spec, real_sellers, delta, expected):
    found = find_with(steps.assess_quote(quote(), spec, pack))
    checks = check_suppliers([found], pack, FakeCrustdata(real_sellers))
    deadline = date.today() + timedelta(days=7 + delta)
    assert steps.decide(spec, found, checks, pack, deadline).status == expected


def test_consolidated_shipping_preserves_per_line_and_total_cost(pack, spec, real_sellers):
    second = spec.model_copy(update={"line_id": "ldo-2"})
    first_find = find_with(steps.assess_quote(quote(price_inr=8.0), spec, pack))
    second_found = quote(price_inr=12.0).model_copy(update={"line_id": second.line_id})
    second_find = find_with(steps.assess_quote(second_found, second, pack), line_id=second.line_id)
    checks = check_suppliers([first_find, second_find], pack, FakeCrustdata(real_sellers))
    decisions = [steps.decide(line, found, checks, pack, None)
                 for line, found in ((spec, first_find), (second, second_find))]
    assert [item.landed.shipping_inr for item in decisions] == [2200.0, 2200.0]
    first, second = steps.consolidate_shipping(decisions, pack)
    assert (first.landed.shipping_inr, second.landed.shipping_inr) == (880.0, 1320.0)
    assert (first.total_inr, second.total_inr) == (6334.24, 9501.36)
    assert first.total_inr + second.total_inr == pytest.approx(15_835.60)


@pytest.mark.parametrize("case", [
    pytest.param("negative-price", marks=pytest.mark.xfail(
        strict=True, raises=AssertionError, reason="A negative quote can be picked and create a negative mock charge")),
    pytest.param("moq-stock", marks=pytest.mark.xfail(
        strict=True, raises=AssertionError, reason="Stock is checked against requested quantity, below forced MOQ")),
    pytest.param("unknown-arrival", marks=pytest.mark.xfail(
        strict=True, raises=AssertionError, reason="An unknown arrival date can pass an explicit deadline")),
])
def test_quote_with_missing_or_inconsistent_purchase_facts_creates_no_order(pack, spec, real_sellers, case):
    pack = deepcopy(pack)
    candidate = quote()
    deadline = date.today() + timedelta(days=10)
    if case == "negative-price":
        candidate.tiers = [PriceTier(min_qty=1, unit_price=-10.0, currency="INR")]
    elif case == "moq-stock":
        candidate.moq = 1000
        candidate.stock = 500
        candidate.tiers = [PriceTier(min_qty=1000, unit_price=10.0, currency="INR")]
    else:
        next(d for d in pack["distributors"] if d["domain"] == candidate.vendor_domain)["transit_days"] = None
    found = find_with(steps.assess_quote(candidate, spec, pack))
    checks = check_suppliers([found], pack, FakeCrustdata(real_sellers))
    result = steps.decide(spec, found, checks, pack, deadline)
    order = orders.place_order(result, "fixture", approval_limit_inr=50_000)
    assert order is None and not payments.MockGateway().intents()


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="A failure after wallet debit but before intent save debits again on retry")
def test_internal_gateway_interruption_reconciles_without_a_second_debit(pack, spec, real_sellers, monkeypatch):
    found = find_with(steps.assess_quote(quote(), spec, pack))
    checks = check_suppliers([found], pack, FakeCrustdata(real_sellers))
    decision = steps.decide(spec, found, checks, pack, None)
    original_save = payments.MockGateway._save

    def interrupt_confirmation(gateway, intent):
        if intent.status == "succeeded":
            raise orders.SimulatedCrash("mock intent write interrupted after wallet debit")
        original_save(gateway, intent)

    wallet = payments.AgentWallet()
    before = wallet.balance
    with monkeypatch.context() as patch:
        patch.setattr(payments.MockGateway, "_save", interrupt_confirmation)
        with pytest.raises(orders.SimulatedCrash):
            orders.place_order(decision, "fixture", approval_limit_inr=50_000)

    [pending] = orders.list_orders()
    assert pending.state == "PAY_PENDING"
    paid = orders.place_order(decision, "fixture", approval_limit_inr=50_000)
    assert paid.state == "PAID"
    matching_debits = [entry for entry in wallet.ledger() if entry["ref"] == paid.payment_id and entry["kind"] == "debit"]
    assert len(matching_debits) == 1 and wallet.balance == before - paid.amount_inr
