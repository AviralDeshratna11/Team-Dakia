"""HTTP integration smoke test; requires API, worker and Vite running locally."""
from datetime import datetime, timezone, timedelta
import time
import httpx

base = "http://127.0.0.1:5173"
with httpx.Client(base_url=base, timeout=10) as client:
    assert client.get("/").status_code == 200
    assert client.get("/src/main.tsx").status_code == 200
    assert client.get("/api/health").json()["payments"] == "mock"
    for category, text, kind in [
        ("electronics", "ESP32-S3 modules, 500 of each", "purchase"),
        ("leather_goods", "Find three leather-goods manufacturers in Uttar Pradesh", "shortlist"),
    ]:
        response = client.post("/api/runs", json={
            "text": text, "category": category, "request_type": kind,
            "destination": "Bengaluru, India", "deadline": (datetime.now(timezone.utc)+timedelta(days=21)).isoformat(),
            "research_budget_minor": 200000, "approval_threshold_minor": 100000000,
            "mode": "fixture", "scenario": "missing_stock" if kind == "purchase" else "normal",
        })
        response.raise_for_status()
        run_id = response.json()["id"]
        end = time.monotonic() + 30
        while time.monotonic() < end:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["state"] in {"COMPLETED", "COMPLETED_WITH_GAPS", "FAILED"}:
                break
            time.sleep(.2)
        assert run["state"] == "COMPLETED", run
        assert run["decisions"][0]["evidence_ids"]
        if kind == "purchase":
            assert len(run["orders"]) == 1 and run["orders"][0]["state"] == "PAID"
            spend = client.get(f"/api/runs/{run_id}/ledger").json()
            assert sum(e["amount_minor"] for e in spend if e["account"] == "research") == 4000
        else:
            assert len(run["rfqs"]) == 3 and not run["orders"]
        print(f"PASS {category}: proxy -> API -> worker -> SQLite -> completed cited result ({run_id})")
