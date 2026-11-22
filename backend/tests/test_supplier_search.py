import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.contracts import now
from app.main import create_app
from app.persistence.database import Database
from app.workflow.orchestrator import Orchestrator
from test_general_human_review import drive
from test_live import client as crustdata_client
from test_llm import fake_client, intent, response


@pytest.mark.parametrize(
    ("budget", "domains", "citation", "expected"),
    [
        ("5", [], "valid", "completed"),
        ("2.2", [], "valid", "skipped"),
        ("5", [], "fabricated", "skipped"),
        ("0.2", ["first.example"], "valid", None),
    ],
)
def test_gemini_supplier_search_respects_evidence_and_credit_cap(tmp_path, budget, domains, citation, expected):
    first = "https://first.example/components"
    better = "https://better.example/esp32-s3"
    calls = []
    queries = []

    def crustdata(request):
        path = request.url.path
        data = json.loads(request.content)
        calls.append(path)
        if path == "/web/search/live":
            queries.append(data["query"])
            urls = [first, better] if "authorized catalogue" in data["query"] else [first]
            return httpx.Response(200, headers={"x-credits-used": "1"}, json={"results": [{"url": url} for url in urls]})
        if path == "/company/identify":
            domain, = data["domains"]
            company = {"crustdata_company_id": 1 if domain == "first.example" else 2,
                       "basic_info": {"name": domain, "primary_domain": domain}}
            return httpx.Response(200, headers={"x-credits-used": "0.2"}, json=[
                {"matched_on": domain, "matches": [{"company_data": company}]},
            ])
        if path == "/web/enrich/live":
            url, = data["urls"]
            return httpx.Response(200, headers={"x-credits-used": "1"}, json=[
                {"url": url, "success": True, "content": "ESP32-S3 official product catalogue"},
            ])
        pytest.fail(f"Unexpected Crustdata endpoint {path}")

    plan_data = []

    def gemini(request):
        body = json.loads(request.content)
        schema = body["generationConfig"]["responseJsonSchema"]["title"]
        data = json.loads(body["contents"][0]["parts"][0]["text"])
        if schema == "ParsedIntent":
            return response(intent())
        if schema == "SupplierSearchPlan":
            plan_data.append(data)
            assert data["item"]["part_number"] == "ESP32-S3"
            assert "QUOTE_UNVERIFIED" in data["blockers"]
            assert any("first.example" in e["content"] for e in data["evidence"])
            eid = "invented" if citation == "fabricated" else data["evidence"][0]["id"]
            return response({"search_terms": "authorized catalogue", "evidence_ids": [eid]})
        assert schema == "SupplierExtraction"
        if [s["domain"] for s in data["suppliers"]] == ["better.example"]:
            page, = [e for e in data["evidence"] if e["provider"] == "crustdata_web_fetch" and e["reference"] == better]
            return response({"insights": [{
                "supplier_id": "crustdata:2", "summary": "The supplier lists an ESP32-S3 catalogue.",
                "evidence_ids": [page["id"]], "facts": [{
                    "spec_id": data["specs"][0]["id"], "field": "matches_spec", "value": "true",
                    "evidence_id": page["id"], "excerpt": "ESP32-S3 official product catalogue",
                }],
            }]})
        return response({"insights": []})

    db = Database(str(tmp_path / "state.db"))
    worker = Orchestrator(db, live_client=crustdata_client(tmp_path, crustdata), llm_client=fake_client(tmp_path, gemini))
    with TestClient(create_app(db)) as api:
        posted = api.post("/api/runs", json={
            "text": "ESP32-S3 modules, 500 of each", "mode": "live", "category": "electronics",
            "llm_enabled": True, "supplier_domains": domains, "provider_credit_limit": budget,
            "deadline": (now() + timedelta(days=21)).isoformat(),
        })
        assert posted.status_code == 201
        run = drive(api, worker, posted.json()["id"])
        assert run["state"] == "WAITING_FOR_HUMAN", run["events"]
        assert not run["orders"] and worker.payments.count() == 0
        assert sum(Decimal(row["charged"]) for row in run["provider_usage"]) <= Decimal(budget)

    search = run["supplier_search"]
    if expected is None:
        assert search is None and calls == ["/company/identify"] and not plan_data
        return
    assert search["status"] == expected
    if expected == "completed":
        assert len(queries) == 2 and "authorized catalogue" in queries[-1]
        assert [s["domain"] for s in run["suppliers"]] == ["first.example", "better.example"]
        assert search["supplier_ids"] == ["crustdata:2"]
        assert any(e["reference"] == better and e["id"] in search["evidence_ids"] for e in run["evidence"])
        assert {call["stage"] for call in run["llm_usage"]} == {
            "parse_request", "extract_provider_evidence", "plan_supplier_search", "extract_follow_up_evidence",
        }
        assert all(c["eligible"] is False for c in run["decisions"][0]["candidates"])
        lead, = [q for q in run["quotes"] if q["supplier_id"] == "crustdata:2"]
        assert lead["matches_spec"] is True and lead["status"] == "unverified"
    else:
        assert len(queries) == 1 and not search["supplier_ids"]
        assert len(plan_data) == (1 if citation == "fabricated" else 0)
