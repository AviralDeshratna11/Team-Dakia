"""Run one live leather-goods request end to end against Crustdata and print the result.

Usage (from the repository root):
    .venv/bin/python scripts/live_leather.py ["your request text"] [--credits 25] [--db live.db]

Reads CRUSTDATA_API_KEY from the environment or from ./.env (never printed).
Spends real Crustdata credits, capped by --credits. No vendor is contacted; RFQs stay drafts.
"""
import argparse
import json
import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts import SourcingRequest, WorkflowRun, now  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.repositories import save_run, get_run  # noqa: E402
from app.workflow.orchestrator import Orchestrator, ACTIVE  # noqa: E402

DEFAULT = "Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft the first email to each."


def load_env():
    env = ROOT / ".env"
    if env.exists() and not os.getenv("CRUSTDATA_API_KEY"):
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", default=DEFAULT)
    parser.add_argument("--credits", type=float, default=25)
    parser.add_argument("--db", default=str(ROOT / "sourcing.db"))
    args = parser.parse_args()
    load_env()
    if not os.getenv("CRUSTDATA_API_KEY"):
        sys.exit("Put CRUSTDATA_API_KEY=... in .env first.")
    db = Database(args.db)
    worker = Orchestrator(db)
    run = WorkflowRun(request=SourcingRequest(text=args.text, request_type="shortlist", category="leather_goods", mode="live",
                                              deadline=now() + timedelta(days=21), credit_budget=args.credits))
    with db.transaction() as c:
        save_run(c, run)
    seen = 0
    for _ in range(200):
        with db.transaction() as c:
            run = get_run(c, run.id)
        for event in run.events[seen:]:
            print(f"[{event.state}] {event.message}")
        seen = len(run.events)
        if run.state not in ACTIVE:
            break
        worker.tick(run.id)
    print("\n== Result:", run.state, "| run id", run.id)
    if run.error:
        print("Error:", run.error)
    decision = run.decisions[0] if run.decisions else None
    for s in run.suppliers:
        cand = next((c for c in decision.candidates if c.supplier_id == s.id), None) if decision else None
        status = "SHORTLIST" if decision and s.id in decision.selected_supplier_ids else ("eligible" if cand and cand.eligible else "rejected")
        checks = {c.kind: c.result for c in run.checks if c.supplier_id == s.id}
        print(f"- {s.name} [{status}] kind={s.kind} checks={json.dumps(checks)}")
        if cand and not cand.eligible:
            print("    why not:", ", ".join(cand.reason_codes))
        if s.profile.get("factory_for") or s.profile.get("factory_against"):
            print("    factory evidence:", s.profile.get("factory_for"), "| against:", s.profile.get("factory_against"))
    for p in run.research:
        print(f"Research: {p.status} - {p.question} - {p.rationale}")
    for r in run.rfqs:
        print(f"RFQ -> {r.recipient or 'company inbox'} @ {next(s.name for s in run.suppliers if s.id == r.supplier_id)}")
    usage = worker.live_client.usage(run.id) if worker.live_client else []
    total = sum(Decimal(u["charged"] if u["charged"] is not None else u["reserved"]) for u in usage)
    print(f"\nCrustdata credits used: {total} of {args.credits}")
    for u in usage:
        print(f"  {u['charged'] or u['reserved']:>6}  {u['path']:<18} {u['state']:<9} {u['purpose']}")


if __name__ == "__main__":
    main()
