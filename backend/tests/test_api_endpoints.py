from datetime import timedelta

from app.contracts import now
from test_general_human_review import drive, payload, system


def test_every_api_route_in_sourcing_and_checkout_workflow(system):
    _, worker, api = system
    checked = set()

    def call(method, path, status=200, body=None, **params):
        response = api.request(method, path.format(**params), json=body)
        assert response.status_code == status, response.text
        if 200 <= status < 300:
            checked.add((method.lower(), path))
        return response.json()

    schema = api.get("/openapi.json").json()
    assert api.get("/docs").status_code == 200 and api.get("/redoc").status_code == 200
    assert call("GET", "/api/health")["payments"] == "mock"
    opening = call("GET", "/api/payments/wallet")["available_minor"]
    funding = {"amount_minor": 1_000, "idempotency_key": "api-smoke-test-funding"}
    for _ in range(2):
        assert call("POST", "/api/payments/wallet/fund", body=funding)["available_minor"] == opening + 1_000
    assert call("GET", "/api/human-reviews") == []

    created = call("POST", "/api/runs", 201, payload(
        category="packaging", specs=[], text="Source cotton tote bags", approval_threshold_minor=0,
    ))
    rid = created["id"]
    run_path = "/api/runs/{run_id}"
    run = {"run_id": rid}
    assert call("GET", "/api/runs")[0]["id"] == rid
    assert call("GET", run_path, **run)["state"] == "CREATED"
    assert call("GET", run_path, 404, run_id="unknown")["error"]["code"] == "NOT_FOUND"
    budget = run_path + "/budget"
    call("PATCH", budget, 422, {"provider_credit_limit": 0.001}, **run)
    saved = call("PATCH", budget, body={"research_budget_minor": 2200, "provider_credit_limit": 1.25}, **run)
    assert saved["request"]["research_budget_minor"] == 2200
    assert call("GET", run_path, **run)["request"]["provider_credit_limit"] == "1.25"

    assert drive(api, worker, rid)["state"] == "WAITING_FOR_CLARIFICATION"
    specs = payload()["specs"]
    call("POST", run_path + "/clarifications", body={"answer": "100 cotton bags, 38 x 42 cm.", "specs": specs}, **run)
    paused = drive(api, worker, rid)
    assert paused["state"] == "WAITING_FOR_HUMAN"
    task = call("POST", run_path + "/human-review", body={"note": "Please review the supplier quote."}, **run)
    task_id = task["human_reviews"][-1]["id"]
    assert call("GET", "/api/human-reviews")[0]["id"] == task_id
    task_path = run_path + "/human-reviews/{task_id}"
    review = dict(run, task_id=task_id)
    quote = {
        "spec_id": specs[0]["id"], "supplier_name": "Fixture Supplier", "supplier_domain": "fixture.example",
        "supplier_kind": "manufacturer", "identity_reference": "FIXTURE registration",
        "quote_reference": "FIXTURE-001", "quote_excerpt": "100 specified tote bags, INR 8000 delivered.",
        "reviewer": "Fixture reviewer", "quantity": 100, "unit": paused["specs"][0]["unit"],
        "destination": paused["request"]["destination"], "delivery_at": (now() + timedelta(days=7)).isoformat(),
        "expires_at": (now() + timedelta(days=2)).isoformat(), "total_minor": 800_000,
        "includes_all_costs": True, "requirements_and_availability_confirmed": True,
    }
    call("POST", task_path + "/supplier-quotes", 201, quote, **review)
    call("POST", task_path + "/resolve", body={
        "reviewer": "Fixture reviewer", "action": "evaluate_quotes", "note": "Fixture quote reviewed.",
    }, **review)
    assert drive(api, worker, rid)["state"] == "WAITING_FOR_APPROVAL"
    [order] = call("GET", run_path + "/orders", **run)
    assert call("GET", run_path + "/plan", **run)[0]["selected_quote_id"] == order["quote_id"]
    preview_path = run_path + "/orders/{order_id}/payout-preview"
    call("GET", preview_path, 409, order_id=order["id"], **run)
    call("POST", run_path + "/approvals", body={
        "plan_version": order["plan_version"], "order_id": order["id"], "amount_minor": order["amount_minor"],
    }, **run)
    preview = call("GET", preview_path, order_id=order["id"], **run)
    assert preview["body"]["amount"] == order["amount_minor"] == quote["total_minor"]
    assert drive(api, worker, rid)["state"] == "COMPLETED"
    assert call("POST", run_path + "/resume", **run)["state"] == "COMPLETED"
    assert call("GET", run_path + "/events", **run)[-1]["state"] == "COMPLETED"
    assert call("GET", run_path + "/orders", **run)[0]["payment_id"].startswith("sim_")
    assert call("GET", run_path + "/ledger", **run)[0]["amount_minor"] == quote["total_minor"]
    assert call("GET", run_path + "/rfqs", **run) == []
    assert call("GET", run_path + "/reservations", **run) == []
    assert call("GET", "/api/payments/wallet")["available_minor"] == opening + 1_000 - quote["total_minor"]
    expected = {(method, path) for path, operations in schema["paths"].items() for method in operations}
    assert checked == expected
