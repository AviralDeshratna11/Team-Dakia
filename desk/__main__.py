"""python -m desk run examples/electronics_bom.yaml [--no-cache] [--crash-after-charge]
python -m desk show [run_id]          # latest run if omitted
python -m desk orders                 # every order and its state
python -m desk approve <order_key>    # approve a held order; the next run pays it
"""

import argparse
import os
import sys

import pandas as pd

from desk import orders, pipeline, view
from desk.trace import load_run

pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 60)


def show(records: list[dict]) -> None:
    print(view.steps(records).to_string(index=False))
    print()
    for r in records:
        o = r["output"]
        if r["step"] == "01_clarify":
            print(f"CLARIFY: {o['status']}")
            for a in o.get("answers", []):
                print(f"  {a}")
            if o["status"] == "WAITING_FOR_FOUNDER":
                print("  Stopped before step 2. Answer in a terminal, or add `answers:` to the request file.")
            print()
        if r["step"] == "01_spec" and o:
            print(f"SPEC {r['part'] or ''} ({o['parsed_by']}): {o['category']}, {o['units']} units -> "
                  f"{o['deliver_to']} by {o['deadline']}, data budget ₹{o['data_budget_inr']}")
            for s in o["specs"]:
                alts = ", ".join(a["mpn"] for a in s["alternates"])
                print(f"  [{s['line_id']}] {s['description']} -> {s['mpn']} ({s['maker']}) x{s['qty']}"
                      + (f"; alternates: {alts}" if alts else ""))
            for q in o["questions"]:
                print(f"  QUESTION [{q['line_id'] or 'request'}]: {q['question']}")
            for a in o["assumptions"]:
                print(f"  assumed: {a}")
            print()
        if r["step"] == "02_find" and o:
            print(f"FIND [{r['part']}] {o['status']} ({' | '.join(o['tried'])}): {o['reason']}")
        if r["step"] == "02b_investigate" and o:
            for ag in o["agents"]:
                rep = ag["report"] or {}
                print(f"  AGENT [{r['part']}] {ag['stop_reason']} in {ag['turns']} turns, {ag['credits']:.1f} credits: "
                      f"{rep.get('seller_name')} ({rep.get('seller_domain')}), {rep.get('seller_type')}, "
                      f"seller={rep.get('is_seller')}, exact part={rep.get('sells_exact_part')}, "
                      f"ships to India={rep.get('ships_to_india')}")
                for f in rep.get("red_flags") or []:
                    print(f"      red flag: {f}")
            for q in o["quotes"]:
                print(f"  -> agent quote: {q['vendor']} {q['status']} ₹{q['unit_price_inr']}/unit {q['note']}")
        if r["step"] == "03_check" and o:
            print()
            for c in o:
                print(f"CHECK {c['vendor']}: {c['status']} - {c['reason']}")
            print()
        if r["step"] == "05_decide" and o:
            print(f"DECIDE [{r['part']}] {o['status']}: {o['reason']}")
            for f in o["founder_review"]:
                print(f"    FOR FOUNDER: {f['vendor']} {f['maker']} {f['mpn']} landed ₹{f['landed_inr']} "
                      f"{f['flags']} -> {f['ask']}")
        if r["step"] == "06_order" and o:
            print(f"ORDER [{r['part']}] {o['state']} {o['vendor']} ₹{o['amount_inr']:,.2f} key {o['idempotency_key']}"
                  + (f" - HELD: {o['held_reason']}" if o["held_reason"] else "")
                  + (f" - {o['payment_id']}" if o["payment_id"] else ""))
        if r.get("error"):
            print(f"ERROR in {r['step']} [{r['part']}]: {r['error'].strip().splitlines()[-1]}")


def ask_founder(q) -> str:
    print(f"\nQUESTION [{q.line_id or 'request'}]: {q.question}")
    return input("> ")


def main() -> None:
    p = argparse.ArgumentParser(prog="desk")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("request")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--crash-after-charge", action="store_true",
                   help="crash right after the vendor charges, before we record it (re-run to resume)")
    s = sub.add_parser("show")
    s.add_argument("run_id", nargs="?")
    sub.add_parser("orders")
    ap = sub.add_parser("approve")
    ap.add_argument("order_key")
    a = p.parse_args()

    if a.cmd == "run":
        if a.crash_after_charge:
            os.environ["DESK_CRASH_AT"] = "after_charge"
        try:
            run = pipeline.run(a.request, use_cache=not a.no_cache, ask=ask_founder if sys.stdin.isatty() else None)
            records = run.records
            print(f"run: {run.dir}\n")
        except orders.SimulatedCrash as e:
            print(f"\nCRASHED (on purpose): {e}\nRe-run the same command without --crash-after-charge to resume.\n")
            records = load_run()
        show(records)
    elif a.cmd == "show":
        show(load_run(a.run_id))
    elif a.cmd == "orders":
        print(view.orders().to_string(index=False))
        print("\nvendor checkout (mock) charges:")
        print(pd.DataFrame(orders.checkout_payments()).to_string(index=False))
    elif a.cmd == "approve":
        o = orders.approve(a.order_key)
        print(f"{o.idempotency_key}: {o.state}. Re-run the request to pay it.")


if __name__ == "__main__":
    main()
