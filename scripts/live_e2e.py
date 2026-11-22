"""Run actual local HTTP API -> workflow -> live Crustdata -> saved result.

Reads the API key from an existing environment variable or a hidden prompt. Does not
persist it, put it on a command line, contact vendors or use synthetic supplier data.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import getpass
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
import httpx
import uvicorn
from app.main import create_app
from app.persistence.database import Database
from app.providers.crustdata import CrustdataClient
from app.workflow.orchestrator import Orchestrator, ACTIVE


def main():
    key = os.getenv("CRUSTDATA_API_KEY") or getpass.getpass("Crustdata API key (hidden, not saved): ")
    directory = root / "tmp" / "live-e2e"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    database = Database(str(directory / f"{stamp}.db"))
    provider = CrustdataClient(key=key, credit_limit="0.2", journal_path=database.path + ".credits.db")
    key = None
    before = provider.credits()["account"]["credits"]
    print("Authentication passed. Provider credit cap: 0.2; one Identify call only, no search/fetch.", flush=True)
    worker = Orchestrator(database, live_client=provider)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(10)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(database), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets":[listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        if not server.started: raise RuntimeError("Local HTTP API did not start")
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=45) as client:
            body = {"text":"Source 500 ESP32-S3 modules delivered to Bengaluru within three weeks", "category":"electronics",
                "request_type":"purchase", "destination":"Bengaluru, India", "deadline":(datetime.now(timezone.utc)+timedelta(days=21)).isoformat(),
                "mode":"live", "supplier_domains":["mouser.com"], "scenario":"normal", "research_budget_minor":0, "approval_threshold_minor":0,
                "specs":[{"id":"esp32-s3", "description":"ESP32-S3 module", "part_number":"ESP32-S3", "quantity":500,
                          "parameters":{"part_number":"ESP32-S3"}}]}
            response = client.post("/api/runs", json=body)
            response.raise_for_status()
            run_id = response.json()["id"]
            for _ in range(30):
                run = client.get(f"/api/runs/{run_id}").json()
                print("Stage:", run["state"], flush=True)
                if run["state"] not in ACTIVE: break
                worker.tick(run_id)
            run = client.get(f"/api/runs/{run_id}").json()
            ledger = client.get(f"/api/runs/{run_id}/ledger").json()
        after = provider.credits()["account"]["credits"]
        calls = provider.usage(run_id)
        charged = sum((Decimal(c["charged"] or c["reserved"]) for c in calls), Decimal(0))
        report = {"run_id":run_id, "state":run["state"], "supplier_count":len(run["suppliers"]),
            "resolved_supplier_count":sum(s["identity"] == "resolved" for s in run["suppliers"]),
            "live_evidence_count":sum(e["mode"] == "live" for e in run["evidence"]),
            "provider_credit_cap":"0.2", "provider_accounted_credits":str(charged),
            "account_balance_delta":str(Decimal(str(before))-Decimal(str(after))),
            "provider_calls":calls, "run":run, "ledger":ledger,
            "limitations":["No LLM key: explicit structured specifications supplied", "Live pricing, stock, delivery and distributor authorization extraction remain unverified", "No mock supplier evidence or paid-stock fallback in live mode", "No browser UI test; local HTTP API and real provider calls exercised"]}
        output = root / "docs" / "live-e2e-identity-result.json"
        output.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({k:v for k,v in report.items() if k not in {"run","ledger"}}, indent=2), flush=True)
        print("Saved report:", output, flush=True)
        assert run["state"] == "WAITING_FOR_HUMAN" and any(t["status"] == "pending" for t in run["human_reviews"]), "Unverified purchasing facts must create a human-review handoff"
        assert run["suppliers"] and any(s["identity"] == "resolved" for s in run["suppliers"]), "No supplier identity resolved"
        assert all(e["mode"] == "live" for e in run["evidence"])
        assert not run["orders"] and not ledger and worker.payments.count() == 0
        assert charged <= Decimal("0.2")
        print("PASS: live HTTP flow; missing purchasing evidence blocked orders and requested a human reviewer.", flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        provider.http.close()
        provider.engine.dispose()
        worker.payments.engine.dispose()
        database.engine.dispose()


if __name__ == "__main__":
    main()
