"""Regression benchmark for the externally visible Sourcing Desk contract.

The suite is deterministic and offline: it uses the same synthetic catalogue,
SQLite workflow, and mock payment provider as the local demo.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.config import pack_for
from app.contracts import CategoryPack, Quote, SourcingRequest, Spec, Supplier, RunState as S, now
from app.domain.data_value import worth_buying
from app.domain.decision_engine import assess, decide, landed_cost
from app.main import create_app
from app.persistence.database import Database
from app.persistence.repositories import get_run
from app.providers.mock_checkout import SimulatedCrash
from app.workflow.orchestrator import Orchestrator, ACTIVE


ORDER_TOTAL = 14_879_800
FALLBACK_TOTAL = 18_596_800
RESEARCH_PRICE = 4_000


@pytest.fixture
def system(tmp_path):
    db = Database(str(tmp_path / "benchmark.db"))
    worker = Orchestrator(db)
    try:
        yield db, worker
    finally:
        db.engine.dispose()
        worker.payments.engine.dispose()


def payload(**overrides):
    return SourcingRequest.model_validate({
        "text": "Source ESP32-S3 modules, 500 of each",
        "category": "electronics",
        "deadline": now() + timedelta(days=21),
        "human_review_enabled": False,
        **overrides,
    }).model_dump(mode="json")


def advance(db, worker, run_id, stop=None):
    for _ in range(100):
        with db.transaction() as c:
            run = get_run(c, run_id)
        if (stop and stop(run)) or run.state not in ACTIVE:
            return run
        worker.tick(run_id)
    pytest.fail("benchmark workflow exceeded 100 durable steps")


def assert_response(response, status):
    assert response.status_code == status, response.text
    return response.json()


def assert_error(response, status, code):
    error = assert_response(response, status)["error"]
    assert error["code"] == code
    assert error["message"]
    if status == 422:
        assert error["fields"] and all(isinstance(field, str) for field in error["fields"])


def assert_payment_effects(client, worker, run_id, orders, research):
    """Compare public ledger entries with the independent mock provider's receipts."""
    entries = assert_response(client.get(f"/api/runs/{run_id}/ledger"), 200)
    expected = {("orders", key): amount for key, amount in orders.items()}
    expected.update({("research", key): amount for key, amount in research.items()})
    assert len(entries) == len(expected)
    assert {(row["account"], row["reference"]): row["amount_minor"] for row in entries} == expected
    for (account, key), amount in expected.items():
        receipt = worker.payments.lookup(key)
        assert receipt and (receipt["account"], receipt["amount"]) == (account, amount)


def candidate_inputs(*, quantity=10, stock=10, moq=1, expires_at=None,
                     delivery_at=None, signals=None, shipping_minor=0,
                     shipping_basis="shipment", tiers=None):
    observed = now()
    deadline = observed + timedelta(days=10)
    request = SourcingRequest.model_validate(payload(deadline=deadline))
    supplier = Supplier(id="vendor", canonical_id="canonical-vendor", name="Vendor",
        aliases=[], domain="vendor.example", locations=["India"], kind="manufacturer", identity="resolved")
    quote = Quote(id="quote", supplier_id=supplier.id, spec_id="spec", tiers={1: 101} if tiers is None else tiers,
        quantity=quantity, moq=moq, stock=stock, shipping_minor=shipping_minor,
        shipping_basis=shipping_basis, lead_days=1, delivery_at=delivery_at or deadline,
        serves_destination=True, matches_spec=True, observed_at=observed,
        expires_at=expires_at or observed + timedelta(hours=1), evidence_ids=["evidence"],
        status="verified", signals={"authorized": True, "stable": True, "complaints_clear": True}
        if signals is None else signals)
    return quote, supplier, request


def test_api_endpoint_inventory_and_success_contract(system):
    db, worker = system
    app = create_app(db)
    successes = {}
    run_path = "/api/runs/{run_id}"
    with TestClient(app) as client:
        def call(method, template, status=200, *, run_id="", task_id="", order_id="", **kwargs):
            response = client.request(method, template.format(run_id=run_id, task_id=task_id, order_id=order_id), **kwargs)
            assert response.status_code == status, response.text
            successes[method, template] = status
            return response

        health = call("GET", "/api/health").json()
        assert health["status"] == "ok" and health["payments"] == "mock"
        assert health["worker"] == "separate process required"
        assert call("GET", "/api/runs").json() == []
        wallet_path = "/api/payments/wallet"
        funding_path = wallet_path + "/fund"
        initial_wallet = call("GET", wallet_path).json()
        assert initial_wallet["mode"] == "synthetic" and initial_wallet["currency"] == "INR"
        funding = {"amount_minor": 50_000, "idempotency_key": "benchmark-top-up-01"}
        funded_wallet = call("POST", funding_path, json=funding).json()
        assert funded_wallet["funded_minor"] == initial_wallet["funded_minor"] + funding["amount_minor"]
        assert funded_wallet["available_minor"] == initial_wallet["available_minor"] + funding["amount_minor"]
        assert call("POST", funding_path, json=funding).json() == funded_wallet
        assert_error(client.post(funding_path, json={**funding, "amount_minor": 1}), 409, "INVALID_STATE")
        assert_error(client.post(funding_path, json={**funding, "amount_minor": 0}), 422, "VALIDATION_ERROR")
        assert call("GET", wallet_path).json() == funded_wallet
        created = call("POST", "/api/runs", 201, json=payload(
            text="Source required components", scenario="missing_stock", approval_threshold_minor=0,
        )).json()
        run_id = created["id"]
        assert created["state"] == "CREATED" and created["events"][0]["state"] == "CREATED"
        assert advance(db, worker, run_id).state == S.WAITING_FOR_CLARIFICATION
        answer = "Source ESP32-S3 modules, 500 of each"
        clarified = call("POST", run_path + "/clarifications", run_id=run_id,
                         json={"answer": answer}).json()
        assert clarified["state"] == "PARSING" and clarified["clarification"]["answer"] == answer
        assert clarified["request"]["text"].endswith(f"\nClarification: {answer}")
        updated = call("PATCH", run_path + "/budget", run_id=run_id,
                       json={"research_budget_minor": RESEARCH_PRICE}).json()
        assert updated["request"]["research_budget_minor"] == RESEARCH_PRICE

        waiting = advance(db, worker, run_id)
        assert waiting.state == S.WAITING_FOR_APPROVAL
        order, = waiting.orders
        assert order.state == "PLANNED" and order.amount_minor == ORDER_TOTAL
        assert not worker.payments.lookup(order.idempotency_key)
        payout_path = run_path + "/orders/{order_id}/payout-preview"
        assert_error(client.get(payout_path.format(run_id=run_id, order_id=order.id)), 409, "INVALID_STATE")
        assert_error(client.get(payout_path.format(run_id=run_id, order_id="missing")), 404, "NOT_FOUND")
        approval = {"plan_version": order.plan_version, "order_id": order.id, "amount_minor": ORDER_TOTAL}
        approved = call("POST", run_path + "/approvals", run_id=run_id, json=approval).json()
        assert approved["state"] == "EXECUTING" and approved["orders"][0]["state"] == "APPROVED"
        before_preview = call("GET", wallet_path).json()
        preview = call("GET", payout_path, run_id=run_id, order_id=order.id).json()
        assert preview["mode"] == "preview"
        assert (preview["body"]["mode"], preview["body"]["amount"]) == ("UPI", ORDER_TOTAL)
        assert preview["body"]["queue_if_low_balance"] is False
        assert call("GET", wallet_path).json() == before_preview
        assert not worker.payments.lookup(order.idempotency_key)
        assert advance(db, worker, run_id).state == S.COMPLETED
        detail = call("GET", run_path, run_id=run_id).json()
        assert call("GET", "/api/runs").json() == [detail]
        for suffix, field in (("events", "events"), ("plan", "decisions"), ("orders", "orders"), ("rfqs", "rfqs")):
            assert call("GET", f"{run_path}/{suffix}", run_id=run_id).json() == detail[field]
        assert detail["events"][-1]["state"] == "COMPLETED"
        assert detail["orders"][0]["state"] == "PAID" and detail["rfqs"] == []
        assert detail["decisions"][0]["selected_supplier_ids"] == ["bright"]
        research, = detail["research"]
        assert research["status"] == "paid" and research["changed_decision"]
        assert call("GET", run_path + "/reservations", run_id=run_id).json() == [
            {"id": research["id"], "run_id": run_id, "amount_minor": RESEARCH_PRICE, "status": "settled"},
        ]
        call("GET", run_path + "/ledger", run_id=run_id)
        assert_payment_effects(client, worker, run_id, {order.idempotency_key: ORDER_TOTAL},
                               {research["id"]: RESEARCH_PRICE})
        assert (worker.payments.count(), worker.payments.count("research")) == (1, 1)
        assert call("GET", wallet_path).json()["available_minor"] == (
            funded_wallet["available_minor"] - ORDER_TOTAL - RESEARCH_PRICE
        )
        assert call("POST", run_path + "/resume", run_id=run_id).json() == detail

        queue_path = "/api/human-reviews"
        assert call("GET", queue_path).json() == []
        review_run = call("POST", "/api/runs", 201, json=payload(
            human_review_enabled=True, category="packaging", text="Source 100 cotton tote bags",
            specs=[{"id": "bags", "description": "Cotton tote bags", "quantity": 100}],
        )).json()
        review_id = review_run["id"]
        assert advance(db, worker, review_id, lambda run: run.state == S.DECIDING).state == S.DECIDING
        paused = call("POST", run_path + "/human-review", run_id=review_id,
                      json={"note": "Please have a reviewer evaluate this request."}).json()
        task, = paused["human_reviews"]
        assert paused["state"] == "WAITING_FOR_HUMAN" and task["run_id"] == review_id
        assert task["status"] == "pending" and call("GET", queue_path).json() == [task]
        submission = {
            "spec_id": "bags", "supplier_name": "Fixture Packaging", "supplier_domain": "bags.example",
            "supplier_kind": "manufacturer", "identity_reference": "BENCHMARK-COMPANY",
            "quote_reference": "BENCHMARK-QUOTE", "quote_excerpt": "100 tote bags, delivered, INR 8000 all in.",
            "reviewer": "Benchmark reviewer", "quantity": 100, "unit": paused["specs"][0]["unit"],
            "destination": paused["request"]["destination"], "total_minor": 800_000,
            "includes_all_costs": True, "requirements_and_availability_confirmed": True,
            "delivery_at": (now() + timedelta(days=7)).isoformat(),
            "expires_at": (now() + timedelta(days=2)).isoformat(),
        }
        quote_path = run_path + "/human-reviews/{task_id}/supplier-quotes"
        concrete = quote_path.format(run_id=review_id, task_id=task["id"])
        assert_error(client.post(concrete, json={**submission, "quantity": 99}), 409, "INVALID_STATE")
        assert_error(client.post(concrete, json={**submission, "includes_all_costs": False}), 422, "VALIDATION_ERROR")
        assert client.get(run_path.format(run_id=review_id)).json() == paused
        reviewed = call("POST", quote_path, 201, run_id=review_id, task_id=task["id"], json=submission).json()
        quote, = reviewed["quotes"]
        assert reviewed["state"] == "WAITING_FOR_HUMAN" and reviewed["orders"] == []
        assert quote["reviewed_terms"]["total_minor"] == 800_000
        assert {fact["reference"] for fact in reviewed["evidence"] if fact["id"] in quote["evidence_ids"]} == {
            "BENCHMARK-COMPANY", "BENCHMARK-QUOTE",
        }
        cancelled = call("POST", run_path + "/human-reviews/{task_id}/resolve",
                         run_id=review_id, task_id=task["id"],
                         json={"reviewer": "Benchmark reviewer", "action": "cancel",
                               "note": "Cancel this fixture request."}).json()
        assert cancelled["state"] == "CANCELLED" and cancelled["human_reviews"][0]["status"] == "cancelled"
        assert call("GET", queue_path).json() == []
        assert_payment_effects(client, worker, review_id, {}, {})
        assert (worker.payments.count(), worker.payments.count("research")) == (1, 1)

        schema = call("GET", "/openapi.json").json()
        for path, marker in (("/docs", "Swagger UI"), ("/redoc", "ReDoc"),
                             ("/docs/oauth2-redirect", "swaggerUIRedirectOauth2")):
            assert marker in call("GET", path).text
        for path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
            assert call("HEAD", path).content == b""

    registered = {(method, route.path) for route in app.routes for method in route.methods}
    assert registered == successes.keys()
    checked_in = json.loads((Path(__file__).resolve().parents[2] / "docs" / "openapi.json").read_text(encoding="utf-8"))
    for document in (schema, checked_in):
        operations = {
            (method.upper(), path): operation["responses"]
            for path, methods in document["paths"].items()
            for method, operation in methods.items()
            if method in {"get", "head", "post", "put", "patch", "delete", "options", "trace"}
        }
        assert operations.keys() == {operation for operation in successes if operation[1].startswith("/api/")}
        for operation, responses in operations.items():
            assert str(successes[operation]) in responses


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/runs/missing", None),
    ("get", "/api/runs/missing/events", None),
    ("get", "/api/runs/missing/plan", None),
    ("get", "/api/runs/missing/ledger", None),
    ("get", "/api/runs/missing/reservations", None),
    ("get", "/api/runs/missing/orders", None),
    ("get", "/api/runs/missing/rfqs", None),
    ("patch", "/api/runs/missing/budget", {"research_budget_minor": 0}),
    ("post", "/api/runs/missing/clarifications", {"answer": "answer"}),
    ("post", "/api/runs/missing/approvals", {"plan_version": "v", "order_id": "o", "amount_minor": 0}),
    ("post", "/api/runs/missing/resume", None),
])
def test_all_run_scoped_endpoints_return_structured_404_for_unknown_run(system, method, path, body):
    db, _ = system
    with TestClient(create_app(db)) as client:
        kwargs = {"json": body} if body is not None else {}
        assert_error(client.request(method, path, **kwargs), 404, "NOT_FOUND")
        assert assert_response(client.get("/api/runs"), 200) == []


@pytest.mark.parametrize("overrides", [
    pytest.param({"text": "tiny"}, id="short-text"),
    pytest.param({"text": "x" * 10001}, id="oversized-text"),
    pytest.param({"deadline": "2026-12-01T00:00:00"}, id="timezone-required"),
    pytest.param({"unknown": True}, id="unknown-field"),
    pytest.param({"supplier_domains": ["https://vendor.example/path"]}, id="domain-not-url"),
    pytest.param({"supplier_domains": ["localhost"]}, id="domain-shape"),
    pytest.param({"supplier_domains": ["a.example", "b.example", "c.example", "d.example"]}, id="domain-count"),
    pytest.param({"shortlist_count": 0}, id="shortlist-too-small"),
    pytest.param({"shortlist_count": 11}, id="shortlist-too-large"),
    pytest.param({"category": ""}, id="empty-category"),
    pytest.param({"request_type": "unsupported"}, id="unknown-request-type"),
    pytest.param({"mode": "unsupported"}, id="unknown-mode"),
    pytest.param({"research_budget_minor": -1}, id="negative-research-budget"),
    pytest.param({"approval_threshold_minor": -1}, id="negative-approval-threshold"),
    pytest.param({"specs": [{"description": "Part", "quantity": 0}]}, id="nonpositive-quantity"),
    pytest.param({"specs": [{"id": "same", "description": "Part A"},
                            {"id": "same", "description": "Part B"}]}, id="reused-spec-id"),
])
def test_run_creation_rejects_malformed_or_conflicting_contracts(system, overrides):
    db, _ = system
    with TestClient(create_app(db)) as client:
        assert_error(client.post("/api/runs", json={**payload(), **overrides}), 422, "VALIDATION_ERROR")
        assert assert_response(client.get("/api/runs"), 200) == []


@pytest.mark.parametrize("method, suffix", [
    ("POST", ""), ("PATCH", "/budget"), ("POST", "/clarifications"), ("POST", "/approvals"),
])
def test_malformed_json_never_changes_persisted_state(system, method, suffix):
    db, worker = system
    with TestClient(create_app(db)) as client:
        created = assert_response(client.post("/api/runs", json=payload()), 201)
        path = "/api/runs" + (f"/{created['id']}{suffix}" if suffix else "")
        rejected = client.request(method, path, content=b"{", headers={"Content-Type": "application/json"})
        assert_error(rejected, 422, "VALIDATION_ERROR")
        assert assert_response(client.get("/api/runs"), 200) == [created]
        assert (worker.payments.count(), worker.payments.count("research")) == (0, 0)


def test_clarification_endpoint_state_and_body_boundaries(system):
    db, worker = system
    with TestClient(create_app(db)) as client:
        rid = assert_response(client.post("/api/runs", json=payload(text="a small regulator")), 201)["id"]
        route = f"/api/runs/{rid}"
        assert_error(client.post(route + "/clarifications", json={"answer": "too early"}), 409, "INVALID_STATE")
        run = advance(db, worker, rid)
        assert run.state == S.WAITING_FOR_CLARIFICATION
        assert_error(client.post(route + "/resume"), 409, "INVALID_STATE")
        waiting = assert_response(client.get(route), 200)
        for invalid in ({"answer": ""}, {"answer": "a" * 10001}, {"answer": "regulator", "extra": 1},
                        {"answer": "regulator", "specs": [{"description": "regulator", "quantity": 0}]}):
            assert_error(client.post(route + "/clarifications", json=invalid), 422, "VALIDATION_ERROR")
        assert assert_response(client.get(route), 200) == waiting
        specs = [{"id": "ldo-custom", "description": "3.3V LDO", "quantity": 500,
                  "parameters": {"fixture_item": "ldo", "voltage": "3.3V", "current": ">=500mA", "package": "SOT-23"}}]
        corrected = assert_response(client.post(route + "/clarifications",
                                               json={"answer": "Use the structured spec", "specs": specs}), 200)
        assert corrected["state"] == "PARSING" and corrected["request"]["specs"][0]["id"] == "ldo-custom"
        assert_error(client.post(route + "/clarifications", json={"answer": "Repeated"}), 409, "INVALID_STATE")
        finished = advance(db, worker, rid)
        order, = finished.orders
        assert (finished.state, order.spec_id, order.amount_minor) == (S.COMPLETED, "ldo-custom", 649_000)
        assert_payment_effects(client, worker, rid, {order.idempotency_key: 649_000}, {})


def test_budget_endpoint_rejects_negative_and_extra_fields(system):
    db, _ = system
    with TestClient(create_app(db)) as client:
        created = assert_response(client.post("/api/runs", json=payload()), 201)
        route = f"/api/runs/{created['id']}"
        assert_error(client.patch(route + "/budget", json={"research_budget_minor": -1}), 422, "VALIDATION_ERROR")
        assert_error(client.patch(route + "/budget", json={"research_budget_minor": 0, "currency": "USD"}),
                     422, "VALIDATION_ERROR")
        assert assert_response(client.get(route), 200) == created


def test_approval_is_exact_and_concurrent_replays_settle_once(system):
    db, worker = system
    with TestClient(create_app(db)) as client:
        run_id = assert_response(client.post("/api/runs", json=payload(
            approval_threshold_minor=ORDER_TOTAL - 1, scenario="checkout_timeout",
        )), 201)["id"]
        route = f"/api/runs/{run_id}"
        assert assert_response(client.post(route + "/resume"), 200)["state"] == "CREATED"
        waiting = advance(db, worker, run_id)
        assert waiting.state == S.WAITING_FOR_APPROVAL
        order, = waiting.orders
        approval = {"plan_version": waiting.plan_version, "order_id": order.id, "amount_minor": ORDER_TOTAL}
        assert order.amount_minor == ORDER_TOTAL
        assert_error(client.post(route + "/approvals", json={**approval, "amount_minor": -1}),
                     422, "VALIDATION_ERROR")
        for mismatch in ({"plan_version": "old"}, {"order_id": "different-order"}, {"amount_minor": ORDER_TOTAL - 1}):
            assert_error(client.post(route + "/approvals", json={**approval, **mismatch}), 409, "INVALID_STATE")
        assert assert_response(client.post(route + "/resume"), 200) == waiting.model_dump(mode="json")
        assert_payment_effects(client, worker, run_id, {}, {})
        assert worker.payments.count() == 0

        ready = Barrier(4, timeout=10)

        def submit_approval(_):
            ready.wait()
            return client.post(route + "/approvals", json=approval)

        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(submit_approval, range(4)))
        assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]
        for response in responses:
            if response.status_code == 409:
                assert_error(response, 409, "INVALID_STATE")
        assert worker.payments.count() == 0

        def tick_from_new_connection(ready):
            other_db = Database(db.path)
            other_worker = Orchestrator(other_db)
            try:
                ready.wait()
                other_worker.tick(run_id)
            finally:
                other_worker.payments.engine.dispose()
                other_db.engine.dispose()

        workers_ready = Barrier(4, timeout=10)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(tick_from_new_connection, [workers_ready] * 4))
        run = advance(db, worker, run_id)
        assert run.state == S.COMPLETED and run.orders[0].state == "PAID"
        assert run.orders[0].id == order.id and worker.payments.count() == 1
        assert_payment_effects(client, worker, run_id, {order.idempotency_key: ORDER_TOTAL}, {})
        assert assert_response(client.post(route + "/resume"), 200) == run.model_dump(mode="json")
        assert_error(client.post(route + "/approvals", json=approval), 409, "INVALID_STATE")


def test_failed_live_run_cannot_resume_or_create_purchasing_effects(system):
    db, worker = system
    with TestClient(create_app(db)) as client:
        run_id = assert_response(client.post("/api/runs", json=payload(mode="live")), 201)["id"]
        run = advance(db, worker, run_id)
        assert run.state == S.FAILED and "CRUSTDATA_API_KEY is missing" in run.error
        assert_error(client.post(f"/api/runs/{run_id}/resume"), 409, "INVALID_STATE")
        assert assert_response(client.get(f"/api/runs/{run_id}"), 200) == run.model_dump(mode="json")
        assert not any((run.orders, run.evidence, run.rfqs))
        assert_payment_effects(client, worker, run_id, {}, {})
        assert (worker.payments.count(), worker.payments.count("research")) == (0, 0)


def test_human_review_queue_tracks_only_pending_tasks_and_manual_work_is_unverified(system):
    db, worker = system
    with TestClient(create_app(db)) as client:
        inputs = payload(category="packaging", text="Source 100 cotton tote bags", specs=[
            {"id": "bags", "description": "Cotton tote bags", "parameters": {"material": "cotton"}, "quantity": 100},
        ])
        del inputs["human_review_enabled"]  # Exercise the API default on the newer revision.
        run_id = assert_response(client.post("/api/runs", json=inputs), 201)["id"]
        route = f"/api/runs/{run_id}"
        run = advance(db, worker, run_id)
        assert run.state == "WAITING_FOR_HUMAN" and run.request.human_review_enabled
        assert run.suppliers == [] and run.orders == []
        task, = assert_response(client.get("/api/human-reviews"), 200)
        assert (task["run_id"], task["status"], task["spec_ids"], task["reason_code"]) == (
            run_id, "pending", ["bags"], "NO_PURCHASABLE_PLAN",
        )
        resolve = f"{route}/human-reviews/{task['id']}/resolve"
        note = {"note": "Review the current request."}
        body = {"reviewer": "Fixture reviewer", "action": "manual_completion",
                "note": "Confirmed manual handling with the buyer."}

        assert_error(client.post("/api/runs/missing/human-review", json=note), 404, "NOT_FOUND")
        assert_error(client.post(f"/api/runs/missing/human-reviews/{task['id']}/resolve", json=body),
                     404, "NOT_FOUND")
        assert_error(client.post(route + "/human-review", json={"note": ""}), 422, "VALIDATION_ERROR")
        assert_error(client.post(resolve, json={**body, "action": "unsupported"}), 422, "VALIDATION_ERROR")
        assert_error(client.post(resolve, json=body), 409, "INVALID_STATE")
        assert_error(client.post(route + "/resume"), 409, "INVALID_STATE")
        repeated = assert_response(client.post(route + "/human-review", json=note), 200)
        assert repeated == run.model_dump(mode="json")
        worker.tick(run_id)
        assert assert_response(client.get(route), 200) == repeated

        other_id = assert_response(client.post("/api/runs", json=payload()), 201)["id"]
        other_route = f"/api/runs/{other_id}"
        other = assert_response(client.post(other_route + "/human-review", json=note), 200)
        other_task, = other["human_reviews"]
        assert_error(client.post(f"{other_route}/human-reviews/{task['id']}/resolve",
                                 json={**body, "reference": "FIXTURE-1"}), 409, "INVALID_STATE")
        assert assert_response(client.get(other_route), 200) == other
        assert {item["id"] for item in assert_response(client.get("/api/human-reviews"), 200)} == {
            task["id"], other_task["id"],
        }

        resolution = {**body, "reference": "FIXTURE-1"}
        saved = assert_response(client.post(resolve, json=resolution), 200)
        assert saved["state"] == "DECIDING" and saved["human_completed_spec_ids"] == ["bags"]
        assert advance(db, worker, run_id).state == S.COMPLETED
        finished = assert_response(client.get(route), 200)
        (manual_fact,) = [item for item in finished["evidence"] if item["provider"] == "human_review"]
        assert (manual_fact["status"], manual_fact["reference"]) == ("unverified", "FIXTURE-1")
        assert finished["decisions"][0]["outcome"] == "handled_by_human"
        assert finished["decisions"][0]["evidence_ids"] == [manual_fact["id"]]
        assert finished["orders"] == [] and finished["rfqs"] == []
        assert assert_response(client.post(resolve, json=resolution), 200) == finished
        assert_error(client.post(resolve, json={**resolution, "reference": "DIFFERENT"}), 409, "INVALID_STATE")
        assert_error(client.post(route + "/human-review", json=note), 409, "INVALID_STATE")
        assert assert_response(client.get(route), 200) == finished
        assert assert_response(client.get("/api/human-reviews"), 200) == [other_task]
        assert_payment_effects(client, worker, run_id, {}, {})
        assert (worker.payments.count(), worker.payments.count("research")) == (0, 0)


def test_human_review_preserves_pending_checkout_until_unchanged_retry(system):
    db, worker = system
    with TestClient(create_app(db)) as client:
        run_id = assert_response(client.post("/api/runs", json=payload(
            human_review_enabled=True, approval_threshold_minor=ORDER_TOTAL, scenario="checkout_timeout",
        )), 201)["id"]
        route = f"/api/runs/{run_id}"
        run = advance(db, worker, run_id, lambda r: any(o.state == "RECONCILING" for o in r.orders))
        order, = run.orders
        assert order.state == "RECONCILING" and worker.payments.count() == 1
        assert assert_response(client.get(route + "/ledger"), 200) == []

        paused = assert_response(client.post(route + "/human-review",
                                             json={"note": "Confirm this ambiguous checkout."}), 200)
        task, = paused["human_reviews"]
        resolve = f"{route}/human-reviews/{task['id']}/resolve"
        review = {"reviewer": "Fixture reviewer", "note": "Check the saved mock receipt."}
        for rejected in ({"action": "cancel"}, {"action": "manual_completion", "reference": "FIXTURE-MANUAL"},
                         {"action": "retry", "destination": "Delhi, India"}):
            assert_error(client.post(resolve, json={**review, **rejected}), 409, "INVALID_STATE")
        worker.tick(run_id)
        assert assert_response(client.get(route), 200) == paused
        assert assert_response(client.get("/api/human-reviews"), 200) == [task]

        retry = {**review, "action": "retry"}
        assert assert_response(client.post(resolve, json=retry), 200)["state"] == "EXECUTING"
        finished = advance(db, worker, run_id)
        paid, = finished.orders
        assert (finished.state, paid.id, paid.state) == (S.COMPLETED, order.id, "PAID")
        assert paid.idempotency_key == order.idempotency_key
        assert finished.human_completed_spec_ids == []
        assert assert_response(client.post(resolve, json=retry), 200) == finished.model_dump(mode="json")
        assert assert_response(client.get("/api/human-reviews"), 200) == []
        assert_payment_effects(client, worker, run_id, {paid.idempotency_key: ORDER_TOTAL}, {})
        assert (worker.payments.count(), worker.payments.count("research")) == (1, 0)


@pytest.mark.parametrize("quote_changes, supplier_changes, reason", [
    pytest.param({}, {"identity": "unverified"}, "IDENTITY_UNVERIFIED", id="supplier-identity"),
    pytest.param({}, {"kind": "trader"}, "SUPPLIER_TYPE_NOT_ELIGIBLE", id="supplier-kind"),
    pytest.param({"matches_spec": False}, {}, "SPEC_MISMATCH_OR_UNVERIFIED", id="spec-mismatch"),
    pytest.param({"matches_spec": None}, {}, "SPEC_MISMATCH_OR_UNVERIFIED", id="spec-unknown"),
    pytest.param({"serves_destination": False}, {}, "DESTINATION_UNVERIFIED_OR_UNSERVED", id="destination-unserved"),
    pytest.param({"serves_destination": None}, {}, "DESTINATION_UNVERIFIED_OR_UNSERVED", id="destination-unknown"),
    pytest.param({"status": "unverified"}, {}, "QUOTE_UNVERIFIED", id="quote-unverified"),
    pytest.param({"signals": {"authorized": False, "stable": True, "complaints_clear": True}}, {},
                 "AUTHORIZED_UNVERIFIED_OR_FAILED", id="authorization-denied-despite-score"),
    pytest.param({"signals": {"stable": True, "complaints_clear": True}}, {},
                 "AUTHORIZED_UNVERIFIED_OR_FAILED", id="authorization-missing-despite-score"),
    pytest.param({"stock": None}, {}, "STOCK_UNVERIFIED", id="stock-unknown"),
    pytest.param({"delivery_at": None}, {}, "DELIVERY_UNVERIFIED", id="delivery-unknown"),
    pytest.param({"lead_days": None}, {}, "LEAD_TIME_UNVERIFIED", id="lead-time-unknown"),
    pytest.param({"currency": "EUR"}, {}, "COST_INCOMPLETE", id="fx-unknown"),
    pytest.param({"tiers": {}}, {}, "COST_INCOMPLETE", id="price-tiers-empty"),
    pytest.param({"shipping_minor": None}, {}, "COST_INCOMPLETE", id="shipping-unknown"),
])
def test_independent_purchase_gates_block_selection(quote_changes, supplier_changes, reason):
    quote, supplier, request = candidate_inputs()
    spec = Spec(id=quote.spec_id, description="Synthetic line", quantity=quote.quantity)
    pack = pack_for("electronics")
    assert decide(spec, [quote], {supplier.id: supplier}, request, pack).selected_quote_id == quote.id
    result = decide(spec, [quote.model_copy(update=quote_changes)],
                    {supplier.id: supplier.model_copy(update=supplier_changes)}, request, pack)
    assert result.outcome == "no_eligible_supplier"
    assert result.selected_quote_id is None and result.selected_supplier_ids == []
    assert reason in result.candidates[0].reason_codes


def test_exact_inventory_moq_deadline_and_reliability_threshold_are_eligible():
    deadline = now() + timedelta(days=1)
    quote, supplier, request = candidate_inputs(quantity=10, stock=10, moq=10, delivery_at=deadline)
    request.deadline = deadline
    pack = pack_for("electronics").model_copy(update={"threshold": 75})
    # identity (30) + authorized (25) + stock (20) = exactly the eligibility bar.
    quote.signals = {"authorized": True}
    result = assess(quote, supplier, request, pack)
    assert result.eligible
    assert result.reliability == 75
    assert result.coverage == 75


def test_one_below_reliability_bar_is_rejected():
    quote, supplier, request = candidate_inputs(signals={"authorized": True})
    pack = pack_for("electronics").model_copy(update={"threshold": 76})
    result = assess(quote, supplier, request, pack)
    assert not result.eligible
    assert result.reliability == 75
    assert "BELOW_RELIABILITY_THRESHOLD" in result.reason_codes


@pytest.mark.parametrize("stock,moq,delivery_offset,expected_reason", [
    (9, 1, 0, "INSUFFICIENT_STOCK"),
    (10, 11, 0, "MOQ_NOT_MET"),
    (10, 1, 1, "DEADLINE_MISSED"),
])
def test_each_hard_purchase_constraint_rejects_at_boundary(stock, moq, delivery_offset, expected_reason):
    deadline = now() + timedelta(days=1)
    quote, supplier, request = candidate_inputs(quantity=10, stock=stock, moq=moq,
        delivery_at=deadline + timedelta(days=delivery_offset))
    request.deadline = deadline
    result = assess(quote, supplier, request, pack_for("electronics"))
    assert not result.eligible
    assert expected_reason in result.reason_codes


def test_quote_expiring_at_evaluation_time_is_stale():
    quote, supplier, request = candidate_inputs()
    instant = now()
    quote.expires_at = instant
    result = assess(quote, supplier, request, pack_for("electronics"), at=instant)
    assert not result.eligible
    assert "EVIDENCE_STALE" in result.reason_codes


def test_factory_verification_is_required_even_when_score_is_high():
    quote, supplier, request = candidate_inputs(signals={"factory": True, "stable": True, "complaints_clear": True})
    request.request_type = "shortlist"
    pack = pack_for("leather_goods").model_copy(update={"threshold": 60})
    assert assess(quote, supplier, request, pack).eligible
    quote.signals["factory"] = False
    result = assess(quote, supplier, request, pack)
    assert result.reliability == 60
    assert result.reason_codes == ["FACTORY_UNVERIFIED_OR_FAILED"]


def test_selection_orders_by_cost_then_reliability_then_supplier_id():
    request_quote, supplier, request = candidate_inputs()
    spec = Spec(id=request_quote.spec_id, description="Synthetic line", quantity=request_quote.quantity)
    pack = pack_for("electronics")
    quotes = [
        request_quote.model_copy(update={"id": f"quote-{name}", "supplier_id": name, "tiers": {1: price},
                                         "signals": signals, "evidence_ids": [f"evidence-{name}"]})
        for name, price, signals in (
            ("a-reliable", 100, request_quote.signals),
            ("b-reliable", 100, request_quote.signals),
            ("c-expensive", 101, request_quote.signals),
            ("0-less-reliable", 100, {"authorized": True}),
            ("d-cheap-unverified", 1, {"authorized": False}),
        )
    ]
    suppliers = {q.supplier_id: supplier.model_copy(update={"id": q.supplier_id}) for q in quotes}
    for order in (quotes, list(reversed(quotes)), quotes[2:] + quotes[:2]):
        outcome = decide(spec, order, suppliers, request, pack)
        assert outcome.selected_supplier_ids == ["a-reliable"]
        assert outcome.selected_quote_id == "quote-a-reliable"
        assert outcome.evidence_ids == ["evidence-a-reliable"]


@pytest.mark.parametrize("tiers,shipping,fx,expected", [
    ({1: 100, 10: 80}, 0, {"INR": "1"}, (800, 0, 800)),
    ({1: 100, 10: 80}, 5, {"INR": "1"}, (800, 5, 805)),
    ({1: 100, 10: 80}, 2, {"INR": "1"}, (800, 20, 820)),
])
def test_landed_cost_tier_and_shipping_basis(tiers, shipping, fx, expected):
    basis = "unit" if shipping == 2 else "shipment"
    quote, _, _ = candidate_inputs(quantity=10, tiers=tiers, shipping_minor=shipping, shipping_basis=basis)
    pack = pack_for("electronics").model_copy(update={"fx_to_inr": fx, "duty_bps": 0, "tax_bps": 0})
    cost = landed_cost(quote, pack)
    assert (cost.goods_minor, cost.shipping_minor, cost.total_minor) == expected


def test_landed_cost_fails_closed_when_tier_shipping_or_fx_is_missing():
    quote, _, _ = candidate_inputs(quantity=10, tiers={11: 80})
    pack = pack_for("electronics")
    assert landed_cost(quote, pack) is None  # No tier applies below its minimum.
    quote.tiers = {1: 80}
    quote.shipping_minor = None
    assert landed_cost(quote, pack) is None
    quote.shipping_minor = 0
    pack = pack.model_copy(update={"fx_to_inr": {}})
    assert landed_cost(quote, pack) is None


@pytest.mark.parametrize("quantity, expected", [(9, (905, 1, 91, 179, 1176, "130.67")),
                                                (10, (805, 1, 81, 160, 1047, "104.70"))])
def test_fx_duty_tax_and_rounding_at_tier_boundary(quantity, expected):
    quote, _, _ = candidate_inputs(quantity=quantity, tiers={1: 100, 10: 80}, shipping_minor=1)
    quote.currency = "USD"
    pack = pack_for("electronics").model_copy(update={"fx_to_inr": {"USD": "1.006"},
                                                       "duty_bps": 1000, "tax_bps": 1800})
    cost = landed_cost(quote, pack)
    assert (cost.goods_minor, cost.shipping_minor, cost.duty_minor, cost.tax_minor,
            cost.total_minor, cost.unit_minor) == expected


@pytest.mark.parametrize("price, stock, expected", [(50, None, True), (51, None, False), (50, 10, False)])
def test_research_value_boundary(price, stock, expected):
    quote, _, _ = candidate_inputs(stock=stock, tiers={1: 100})
    assert worth_buying(quote, price, pack_for("electronics"))[0] is expected


@pytest.mark.parametrize("quote_changes", [
    pytest.param({"evidence_ids": []}, id="missing-citations", marks=pytest.mark.xfail(
        strict=True, raises=AssertionError, reason="Eligibility accepts an empty quote citation list")),
    pytest.param({"tiers": {1: -100}}, id="negative-price"),
    pytest.param({"observed_at": datetime(2025, 12, 30, tzinfo=timezone.utc)}, id="stale-observation",
                 marks=pytest.mark.xfail(strict=True, raises=AssertionError,
                                         reason="Eligibility honors expiry but ignores configured observation age")),
])
def test_known_quote_eligibility_gaps(quote_changes):
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quote, supplier, request = candidate_inputs(expires_at=at + timedelta(hours=1))
    quote.observed_at = at
    pack = pack_for("electronics")
    assert assess(quote, supplier, request, pack, at=at).eligible
    result = assess(quote.model_copy(update=quote_changes), supplier, request, pack, at=at)
    assert not result.eligible


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Fixture parsing confuses USB-C cables with connectors")
def test_known_fixture_product_matching_gap(system):
    db, worker = system
    with TestClient(create_app(db)) as client:
        created = assert_response(client.post("/api/runs", json=payload(
            text="Source USB-C cables, 500 of each", approval_threshold_minor=ORDER_TOTAL,
        )), 201)
        run = advance(db, worker, created["id"])
        assert not run.orders and worker.payments.count() == 0


@pytest.mark.parametrize("overrides, expected_state, expected_suppliers, expected_costs, expected_spend", [
    pytest.param({"approval_threshold_minor": ORDER_TOTAL},
                 S.COMPLETED, (("bright",),), (ORDER_TOTAL,), 0, id="electronics-normal"),
    pytest.param({"text": "ESP32-S3 modules, 3.3V 500mA SOT-23 LDO regulator, 10uF 0805 capacitors "
                          "and USB-C connectors; 500 of each"},
                 S.COMPLETED, (("bright",),) * 4, (ORDER_TOTAL, 649_000, 64_900, 1_339_300), 0,
                 id="electronics-full-bom"),
    pytest.param({"scenario": "provider_timeout"}, S.COMPLETED_WITH_GAPS, ((),), (), 0,
                 id="electronics-provider-timeout"),
    pytest.param({"specs": [{"description": "12V regulator", "quantity": 500, "parameters": {"voltage": "12V"}}]},
                 S.COMPLETED_WITH_GAPS, ((),), (), 0, id="electronics-unsupported-spec"),
    pytest.param({"destination": "Paris, France"}, S.COMPLETED_WITH_GAPS, ((),), (), 0,
                 id="electronics-unserved-destination"),
    pytest.param({"scenario": "missing_stock", "research_budget_minor": 0},
                 S.COMPLETED, (("steady",),), (FALLBACK_TOTAL,), 0, id="electronics-zero-budget"),
    pytest.param({"scenario": "missing_stock", "research_budget_minor": RESEARCH_PRICE - 1},
                 S.COMPLETED, (("steady",),), (FALLBACK_TOTAL,), 0, id="electronics-budget-one-short"),
    pytest.param({"scenario": "missing_stock", "research_budget_minor": RESEARCH_PRICE},
                 S.COMPLETED, (("bright",),), (ORDER_TOTAL,), RESEARCH_PRICE, id="electronics-budget-exact"),
    pytest.param({"text": "Find one leather-goods manufacturer in Uttar Pradesh", "category": "leather_goods",
                  "request_type": "shortlist", "shortlist_count": 1},
                 S.COMPLETED, (("agra",),), (), 0, id="leather-single-draft"),
    pytest.param({"text": "Find three leather-goods manufacturers in Uttar Pradesh", "category": "leather_goods",
                  "request_type": "shortlist"},
                 S.COMPLETED, (("agra", "kanpur", "noida"),), (), 0, id="leather-full-shortlist"),
    pytest.param({"text": "Find four leather-goods manufacturers in Uttar Pradesh", "category": "leather_goods",
                  "request_type": "shortlist", "shortlist_count": 4},
                 S.COMPLETED_WITH_GAPS, (("agra", "kanpur", "noida"),), (), 0, id="leather-shortlist-gap"),
])
def test_labeled_workflows(system, overrides, expected_state, expected_suppliers, expected_costs, expected_spend):
    """Independent expected supplier IDs, totals and externally visible effects."""
    db, worker = system
    with TestClient(create_app(db)) as client:
        run_id = assert_response(client.post("/api/runs", json=payload(
            **{"approval_threshold_minor": FALLBACK_TOTAL, **overrides})), 201)["id"]
        route = f"/api/runs/{run_id}"
        advance(db, worker, run_id)
        run = assert_response(client.get(route), 200)
        assert run["state"] == expected_state
        decisions = assert_response(client.get(route + "/plan"), 200)
        assert tuple(tuple(d["selected_supplier_ids"]) for d in decisions) == expected_suppliers
        evidence = {e["id"]: e for e in run["evidence"]}
        for decision, selected in zip(decisions, expected_suppliers, strict=True):
            assert decision["outcome"] == ("selected" if selected else "no_eligible_supplier")
            candidates = [candidate for candidate in decision["candidates"] if candidate["supplier_id"] in selected]
            assert all(candidate["eligible"] and not candidate["reason_codes"] for candidate in candidates)
            citations = {ref for candidate in candidates for ref in candidate["evidence_ids"]}
            assert set(decision["evidence_ids"]) == citations
            if selected:
                assert all(candidate["evidence_ids"] for candidate in candidates) and citations
                assert all(evidence[ref]["mode"] == "fixture" and evidence[ref]["status"] == "verified"
                           for ref in citations)

        orders = assert_response(client.get(route + "/orders"), 200)
        assert tuple(order["amount_minor"] for order in orders) == expected_costs
        assert all(order["state"] == "PAID" and order["payment_id"] for order in orders)
        for order in orders:
            decision = next(decision for decision in decisions if decision["spec_id"] == order["spec_id"])
            assert order["quote_id"] == decision["selected_quote_id"]
            assert [order["supplier_id"]] == decision["selected_supplier_ids"]
        rfqs = assert_response(client.get(route + "/rfqs"), 200)
        expected_drafts = expected_suppliers[0] if overrides.get("request_type") == "shortlist" else ()
        assert tuple(draft["supplier_id"] for draft in rfqs) == expected_drafts
        assert all(draft["status"] == "draft" and draft["subject"] and draft["body"] for draft in rfqs)
        assert len({draft["id"] for draft in rfqs}) == len(rfqs)

        paid_research = {item["id"]: item["quoted_minor"] for item in run["research"] if item["status"] == "paid"}
        assert sum(paid_research.values()) == expected_spend <= run["request"]["research_budget_minor"]
        assert len(paid_research) == int(expected_spend > 0)
        holds = assert_response(client.get(route + "/reservations"), 200)
        assert {(hold["id"], hold["amount_minor"], hold["status"]) for hold in holds} == {
            (key, amount, "settled") for key, amount in paid_research.items()
        }
        assert_payment_effects(client, worker, run_id, {o["idempotency_key"]: o["amount_minor"] for o in orders},
                               paid_research)
        assert assert_response(client.post(route + "/resume"), 200) == run
        worker.tick(run_id)
        assert assert_response(client.get(route), 200) == run
        assert (worker.payments.count(), worker.payments.count("research")) == (len(orders), len(paid_research))


def test_research_commit_survives_worker_failure_and_lowered_budget(system, monkeypatch):
    db, worker = system
    with TestClient(create_app(db)) as client:
        run_id = assert_response(client.post("/api/runs", json=payload(
            scenario="missing_stock", research_budget_minor=RESEARCH_PRICE, approval_threshold_minor=ORDER_TOTAL,
        )), 201)["id"]
        route = f"/api/runs/{run_id}"
        run = advance(db, worker, run_id, lambda r: any(p.status == "reserved" for p in r.research))
        purchase, = run.research
        original_pay = worker.payments.pay

        def fail_after_commit(key, amount, account):
            original_pay(key, amount, account)
            raise SimulatedCrash("research provider committed before workflow transaction completed")

        with monkeypatch.context() as patch:
            patch.setattr(worker.payments, "pay", fail_after_commit)
            with pytest.raises(SimulatedCrash):
                worker.tick(run_id)
        assert worker.payments.count("research") == 1
        assert assert_response(client.get(route + "/ledger"), 200) == []
        assert assert_response(client.get(route + "/reservations"), 200)[0]["status"] == "reserved"
        assert_response(client.patch(route + "/budget", json={"research_budget_minor": 0}), 200)

        restarted_db = Database(db.path)
        restarted = Orchestrator(restarted_db)
        try:
            run = advance(restarted_db, restarted, run_id)
            order, = run.orders
            assert run.state == S.COMPLETED and run.research[0].status == "paid"
            assert run.research[0].id == purchase.id
            assert run.request.research_budget_minor == 0
            assert assert_response(client.get(route + "/reservations"), 200) == [
                {"id": purchase.id, "run_id": run_id, "amount_minor": RESEARCH_PRICE, "status": "settled"},
            ]
            assert_payment_effects(client, restarted, run_id, {order.idempotency_key: ORDER_TOTAL},
                                   {purchase.id: RESEARCH_PRICE})
            assert (restarted.payments.count(), restarted.payments.count("research")) == (1, 1)
        finally:
            restarted.payments.engine.dispose()
            restarted_db.engine.dispose()


@pytest.mark.parametrize("pack_name,trust_signal,kind", [
    ("industrial_parts", "certified", "manufacturer"),
    ("medical_supplies", "license_valid", "distributor"),
    ("food_packaging", "food_contact_compliant", "manufacturer"),
    ("textiles", "material_test_passed", "manufacturer"),
])
def test_core_decision_engine_works_with_unlisted_category_profiles(pack_name, trust_signal, kind):
    """Exercise the shared domain engine with synthetic packs beyond today's API enum."""
    pack = CategoryPack.model_validate({
        "name": pack_name, "version": "benchmark-1",
        "spec_fields": ["grade", "tolerance"], "required_fields": ["grade"],
        "clarification_rules": ["Clarify missing grade"],
        "substitution_constraints": ["Require user-approved equivalent"],
        "discovery_hints": ["Manufacturer and authorized distribution sites"],
        "trust_signals": [trust_signal, "stable", "stock", "complaints_clear"],
        "allowed_kinds": [kind],
        "weights": {"identity": 30, trust_signal: 25, "stable": 15, "stock": 20, "complaints_clear": 10},
        "threshold": 76, "freshness_hours": 24, "ordering_method": "checkout",
        "duty_bps": 0, "tax_bps": 0, "tax_base": "goods_shipping_duty",
        "fx_to_inr": {"INR": "1", "USD": "84.5"},
        "research_cost_minor": 4000, "research_max_order_bps": 500,
    })
    quote, supplier, request = candidate_inputs(signals={trust_signal: True, "stable": True,
        "complaints_clear": True})
    supplier.kind = kind
    assert assess(quote, supplier, request, pack).eligible

    # A missing category-specific trust signal lowers confidence and fails closed.
    quote.signals[trust_signal] = None
    rejected = assess(quote, supplier, request, pack)
    assert not rejected.eligible
    assert "BELOW_RELIABILITY_THRESHOLD" in rejected.reason_codes


@pytest.mark.parametrize("kind", ["unknown", "trader"])
def test_generic_category_profile_rejects_supplier_kind_outside_pack_allowlist(kind):
    pack = CategoryPack.model_validate({
        "name": "industrial_parts", "version": "benchmark-1", "spec_fields": ["grade"],
        "required_fields": ["grade"], "clarification_rules": [], "substitution_constraints": [],
        "discovery_hints": [], "trust_signals": ["certified", "stable", "stock", "complaints_clear"],
        "allowed_kinds": ["manufacturer"],
        "weights": {"identity": 30, "certified": 25, "stable": 15, "stock": 20, "complaints_clear": 10},
        "threshold": 75, "freshness_hours": 24, "ordering_method": "checkout", "duty_bps": 0,
        "tax_bps": 0, "tax_base": "goods_shipping_duty", "fx_to_inr": {"INR": "1"},
        "research_cost_minor": 4000, "research_max_order_bps": 500,
    })
    quote, supplier, request = candidate_inputs(signals={"certified": True, "stable": True,
        "complaints_clear": True})
    supplier.kind = kind
    result = assess(quote, supplier, request, pack)
    assert not result.eligible
    assert "SUPPLIER_TYPE_NOT_ELIGIBLE" in result.reason_codes
