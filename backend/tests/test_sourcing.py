from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.config import pack_for
from app.contracts import SourcingRequest, WorkflowRun, RunState as S, Approval, now
from app.domain.decision_engine import landed_cost, decide
from app.main import create_app
from app.persistence.database import Database
from app.persistence.repositories import save_run, get_run, ledger, reserve
from app.providers.mock_checkout import MockPayments, SimulatedCrash
from app.providers.resilience import SafeTransport, ProviderFailure
from app.workflow.orchestrator import Orchestrator, approve, ACTIVE


def request(**changes):
    return SourcingRequest.model_validate({"text":"Source ESP32-S3 modules, 500 of each", "category":"electronics", "human_review_enabled":False, "deadline":now()+timedelta(days=21), "approval_threshold_minor":100000000, **changes})


@pytest.fixture
def system(tmp_path):
    db = Database(str(tmp_path / "state.db"))
    worker = Orchestrator(db)
    yield db, worker
    db.engine.dispose()
    worker.payments.engine.dispose()


def create(db, req=None):
    run = WorkflowRun(request=req or request())
    with db.transaction() as c: save_run(c, run)
    return run.id


def read(db, rid):
    with db.transaction() as c: return get_run(c, rid)


def drive(db, worker, rid, until=None):
    for _ in range(100):
        run = read(db, rid)
        if (until and until(run)) or run.state not in ACTIVE: return run
        worker.tick(rid)
    pytest.fail("workflow exceeded 100 steps")


def test_clean_full_bom_is_cited_and_paid(system):
    db, worker = system
    req = request(text="ESP32-S3 modules, 3.3V 500mA SOT-23 LDO regulator, 10uF 0805 capacitors and USB-C connectors; 500 of each")
    rid = create(db, req)
    run = drive(db, worker, rid)
    assert run.state == S.COMPLETED
    assert len(run.orders) == 4
    assert all(o.state == "PAID" for o in run.orders)
    assert all(d.evidence_ids for d in run.decisions)
    assert worker.payments.count() == 4
    assert all(e.mode == "fixture" for e in run.evidence)


def test_unresolved_cheap_storefront_is_rejected_and_aliases_merge(system):
    db, worker = system
    run = drive(db, worker, create(db))
    assert len(run.suppliers) == 3
    bright = next(s for s in run.suppliers if s.id == "bright")
    assert "Bright Circuit India" in bright.aliases
    cheap = next(c for c in run.decisions[0].candidates if c.supplier_id == "cheap")
    assert not cheap.eligible and "IDENTITY_UNVERIFIED" in cheap.reason_codes
    assert run.decisions[0].selected_supplier_ids == ["bright"]
    assert "fraud" not in run.decisions[0].explanation.lower()


def test_leather_same_engine_rfq_and_trader_evidence(system):
    db, worker = system
    rid = create(db, request(text="Find three leather-goods manufacturers in Uttar Pradesh", category="leather_goods", request_type="shortlist"))
    run = drive(db, worker, rid)
    assert run.state == S.COMPLETED and len(run.rfqs) == 3
    assert not run.orders and worker.payments.count() == 0
    trader = next(c for c in run.decisions[0].candidates if c.supplier_id == "trader")
    assert not trader.eligible and trader.reliability_breakdown["factory"] == 0
    assert all(d.status == "draft" for d in run.rfqs)
    worker.tick(rid)
    assert len(read(db, rid).rfqs) == 3


def test_missing_stock_buys_research_and_records_changed_decision(system):
    db, worker = system
    rid = create(db, request(scenario="missing_stock"))
    run = drive(db, worker, rid)
    paid = [p for p in run.research if p.status == "paid"]
    assert len(paid) == 1 and paid[0].changed_decision is True
    assert paid[0].quoted_minor == 4000 and paid[0].evidence_ids
    with db.transaction() as c:
        assert sum(l.amount_minor for l in ledger(c, rid) if l.account == "research") == 4000
    assert worker.payments.count("research") == 1


def test_budget_reduction_releases_uncommitted_reservation(system):
    db, worker = system
    rid = create(db, request(scenario="missing_stock"))
    run = drive(db, worker, rid, lambda r: any(p.status == "reserved" for p in r.research))
    assert run.state == S.RESEARCHING
    with db.transaction() as c:
        run = get_run(c, rid)
        run.request.research_budget_minor = 0
        save_run(c, run)
    run = drive(db, worker, rid)
    assert any(p.status == "released" for p in run.research)
    assert worker.payments.count("research") == 0
    assert run.decisions[0].selected_supplier_ids == ["steady"]
    assert next(q for q in run.quotes if q.supplier_id == "bright").stock is None


def test_concurrent_reservations_do_not_overspend(system):
    db, _ = system
    rid = create(db, request(research_budget_minor=5000))
    def attempt(index):
        # Separate engine demonstrates cross-connection SQLite serialization.
        connection_db = Database(db.path)
        try:
            with connection_db.transaction() as c:
                return reserve(c, get_run(c, rid), f"reservation-{index}", 4000)
        finally: connection_db.engine.dispose()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1


def test_provider_timeout_bounded_then_explicit_gaps(system):
    db, worker = system
    run = drive(db, worker, create(db, request(scenario="provider_timeout")))
    assert run.stage_attempts["discovery"] == 3
    assert run.state == S.COMPLETED_WITH_GAPS and not run.orders
    assert all(q.status == "unverified" for q in run.quotes)


@pytest.mark.parametrize("scenario", ["checkout_crash", "checkout_timeout"])
def test_checkout_recovers_across_restart_once(system, scenario):
    db, worker = system
    rid = create(db, request(scenario=scenario))
    if scenario == "checkout_crash":
        with pytest.raises(SimulatedCrash): drive(db, worker, rid)
        assert read(db, rid).orders[0].state == "PAY_PENDING"
        assert worker.payments.count() == 1
    restarted = Orchestrator(Database(db.path))
    run = drive(db, restarted, rid)
    assert run.state == S.COMPLETED
    assert run.orders[0].state == "PAID"
    for _ in range(3): restarted.tick(rid)
    assert restarted.payments.count() == 1
    with db.transaction() as c: assert len(ledger(c, rid)) == 1
    restarted.db.engine.dispose()
    restarted.payments.engine.dispose()


def test_vague_request_waits_and_api_answer_resumes(system):
    db, worker = system
    with TestClient(create_app(db)) as client:
        payload = request(text="a small regulator").model_dump(mode="json")
        created = client.post("/api/runs", json=payload)
        assert created.status_code == 201
        rid = created.json()["id"]
        run = drive(db, worker, rid)
        assert run.state == S.WAITING_FOR_CLARIFICATION
        assert run.clarification.question
        assert client.post(f"/api/runs/{rid}/resume").status_code == 409
        response = client.post(f"/api/runs/{rid}/clarifications", json={"answer": "3.3V, 500mA, SOT-23, 500 of each"})
        assert response.status_code == 200
        assert drive(db, worker, rid).state == S.COMPLETED


def test_build_count_does_not_invent_line_quantities(system):
    db, worker = system
    run = drive(db, worker, create(db, request(text="ESP32-S3 and USB-C connectors for 500 builds")))
    assert run.state == S.WAITING_FOR_CLARIFICATION
    assert not run.orders


def test_landed_cost_shipping_tiers_tax_and_unit_allocation(system):
    db, worker = system
    run = drive(db, worker, create(db))
    q = next(q for q in run.quotes if q.supplier_id == "bright")
    q.quantity = 500
    q.tiers = {1: 1200, 500: 1000}
    q.shipping_minor = 10000
    pack = pack_for("electronics").model_copy(update={"duty_bps": 1000, "tax_bps": 1800})
    cost = landed_cost(q, pack)
    assert cost.goods_minor == 500000
    assert cost.shipping_minor == 10000
    assert cost.duty_minor == 51000 and cost.tax_minor == 100980
    assert cost.total_minor == 661980 and cost.unit_minor == "1323.96"
    q.shipping_basis = "unit"
    q.shipping_minor = 20
    assert landed_cost(q, pack).shipping_minor == 10000


def test_unknown_shipping_stock_and_stale_evidence_block_orders(system):
    db, worker = system
    rid = create(db)
    drive(db, worker, rid, lambda r: r.state == S.DECIDING)
    with db.transaction() as c:
        run = get_run(c, rid)
        for q in run.quotes:
            q.shipping_minor = None
            q.stock = None
            q.expires_at = now() - timedelta(seconds=1)
        save_run(c, run)
    run = drive(db, worker, rid)
    assert run.state == S.COMPLETED_WITH_GAPS and not run.orders
    assert "COST_INCOMPLETE" in run.decisions[0].candidates[0].reason_codes


def test_decision_deterministic_for_normalized_inputs(system):
    db, worker = system
    run = drive(db, worker, create(db))
    suppliers = {s.id:s for s in run.suppliers}
    one = decide(run.specs[0], run.quotes, suppliers, run.request, pack_for("electronics"))
    run.request.text = "Different words, identical normalized specifications"
    two = decide(run.specs[0], list(reversed(run.quotes)), suppliers, run.request, pack_for("electronics"))
    assert one.selected_quote_id == two.selected_quote_id
    assert {c.quote_id:c.cost for c in one.candidates} == {c.quote_id:c.cost for c in two.candidates}


def test_approval_exact_amount_and_changed_quote_invalidates(system):
    db, worker = system
    rid = create(db, request(approval_threshold_minor=0))
    run = drive(db, worker, rid)
    assert run.state == S.WAITING_FOR_APPROVAL
    order = run.orders[0]
    old = Approval(plan_version=order.plan_version, order_id=order.id, amount_minor=order.amount_minor)
    with pytest.raises(ValueError): approve(run, old.model_copy(update={"amount_minor": 1}))
    with db.transaction() as c:
        run = get_run(c, rid)
        quote = next(q for q in run.quotes if q.id == order.quote_id)
        quote.shipping_minor += 500
        save_run(c, run)
    with TestClient(create_app(db)) as client:
        assert client.post(f"/api/runs/{rid}/approvals", json=old.model_dump()).status_code == 409
        assert client.post(f"/api/runs/{rid}/resume").status_code == 200
        run = drive(db, worker, rid)
        assert run.orders[0].state == "PLANNED" and run.plan_version != old.plan_version
        new = run.orders[0]
        assert client.post(f"/api/runs/{rid}/approvals", json={"plan_version":new.plan_version,"order_id":new.id,"amount_minor":new.amount_minor}).status_code == 200
    assert drive(db, worker, rid).orders[0].state == "PAID"


def test_reconcile_paid_effect_before_replanning_expired_quote(system):
    db, worker = system
    rid = create(db, request(scenario="checkout_crash"))
    with pytest.raises(SimulatedCrash): drive(db, worker, rid)
    with db.transaction() as c:
        run = get_run(c, rid)
        for q in run.quotes: q.expires_at = now() - timedelta(seconds=1)
        save_run(c, run)
    run = drive(db, worker, rid)
    assert worker.payments.count() == 1
    assert len(run.orders) == 1 and run.orders[0].state == "PAID"


def test_live_mode_explicitly_unavailable(system):
    db, worker = system
    run = drive(db, worker, create(db, request(mode="live")))
    assert run.state == S.FAILED
    assert "INTEGRATION_UNAVAILABLE" in run.error and not run.evidence


def test_api_budget_validation_resume_and_views(system):
    db, worker = system
    rid = create(db)
    drive(db, worker, rid)
    with TestClient(create_app(db)) as client:
        assert client.patch(f"/api/runs/{rid}/budget", json={"research_budget_minor":-1}).status_code == 422
        assert client.get("/api/runs/missing").status_code == 404
        for endpoint in ["", "/events", "/plan", "/orders", "/rfqs", "/ledger", "/reservations"]:
            assert client.get(f"/api/runs/{rid}{endpoint}").status_code == 200
        for _ in range(3): assert client.post(f"/api/runs/{rid}/resume").status_code == 200
    assert worker.payments.count() == 1


def test_transport_rate_limits_cache_and_secret_redaction():
    class Response(BaseModel):
        name: str
    calls, delays = [], []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After":"0"}) if len(calls) < 3 else httpx.Response(200, json={"name":"fixture"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = SafeTransport(client, sleep=delays.append)
    assert transport.fetch("https://fixture.invalid/company", Response, "canonical-123").name == "fixture"
    transport.fetch("https://fixture.invalid/company", Response, "canonical-123")
    assert len(calls) == 3 and delays == [0, 0]
    broken = SafeTransport(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, text="SECRET_TOKEN"))), sleep=lambda _: None)
    with pytest.raises(ProviderFailure) as error:
        broken.fetch("https://fixture.invalid/?key=SECRET_TOKEN", Response, "key")
    assert error.value.attempts == 3 and "SECRET" not in str(error.value)


def test_unsupported_specs_are_not_falsely_matched(system):
    db, worker = system
    req = request(specs=[{"id":"custom", "description":"12V unusual regulator", "quantity":500, "parameters":{"voltage":"12V"}}])
    run = drive(db, worker, create(db, req))
    assert run.state == S.COMPLETED_WITH_GAPS and not run.orders


def test_real_worker_process_crash_and_restart(system):
    """Exercise process death, independent provider commit and subsequent recovery."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time

    db, worker = system
    rid = create(db, request(scenario="checkout_crash"))
    environment = dict(os.environ, SOURCING_DB=db.path)
    command = [sys.executable, "-m", "app.workflow.worker", "--interval", ".01"]
    cwd = str(Path(__file__).resolve().parents[1])
    first = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, timeout=15)
    assert first.returncode == 42, first.stderr.decode(errors="replace")
    assert read(db, rid).orders[0].state == "PAY_PENDING"
    assert worker.payments.count() == 1
    restarted = subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = read(db, rid)
            if run.state == S.COMPLETED:
                break
            if restarted.poll() is not None:
                pytest.fail("restarted worker exited unexpectedly")
            time.sleep(.03)
        assert run.state == S.COMPLETED
        assert run.orders[0].state == "PAID"
        assert worker.payments.count() == 1
    finally:
        restarted.terminate()
        restarted.communicate(timeout=5)
