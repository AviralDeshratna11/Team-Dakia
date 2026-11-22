"""python -m desk run examples/electronics_bom.yaml [--no-cache]
python -m desk show [run_id]          # latest run if omitted
"""

import argparse
import sys

import pandas as pd

from desk import pipeline, view
from desk.trace import load_run

pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 60)


def show(records: list[dict]) -> None:
    print(view.steps(records).to_string(index=False))
    print()
    for r in records:
        if r["step"] == "01_clarify":
            o = r["output"]
            print(f"CLARIFY: {o['status']}")
            for a in o.get("answers", []):
                print(f"  {a}")
            if o["status"] == "WAITING_FOR_FOUNDER":
                print("  Stopped before step 2. Answer in a terminal, or add `answers:` to the request file.")
            print()
        if r["step"] == "01_spec" and r["output"]:
            o = r["output"]
            print(f"SPEC {r['part'] or ''} ({o['parsed_by']}): {o['category']}, {o['units']} units -> {o['deliver_to']} "
                  f"by {o['deadline']}, data budget ₹{o['data_budget_inr']}")
            for s in o["specs"]:
                alts = ", ".join(a["mpn"] for a in s["alternates"])
                print(f"  [{s['line_id']}] {s['description']} -> {s['mpn']} ({s['maker']}) x{s['qty']}"
                      + (f"; alternates: {alts}" if alts else ""))
            for q in o["questions"]:
                print(f"  QUESTION [{q['line_id'] or 'request'}]: {q['question']}")
            for a in o["assumptions"]:
                print(f"  assumed: {a}")
            print()
        if r["step"] == "02_find" and r["output"]:
            o = r["output"]
            print(f"[{r['part']}] {o['status']} ({' | '.join(o['tried'])}): {o['reason']}")
        if r["step"] == "05_decide" and r["output"]:
            o = r["output"]
            print(f"[{r['part']}] DECISION {o['status']}: {o['reason']}")
            for f in o["founder_review"]:
                print(f"    FOR FOUNDER: {f['vendor']} {f['maker']} {f['mpn']} ₹{f['total_inr']} "
                      f"{f['flags']} -> {f['ask']}")


def ask_founder(q) -> str:
    print(f"\nQUESTION [{q.line_id or 'request'}]: {q.question}")
    return input("> ")


def main() -> None:
    p = argparse.ArgumentParser(prog="desk")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("request")
    r.add_argument("--no-cache", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("run_id", nargs="?")
    a = p.parse_args()

    if a.cmd == "run":
        run = pipeline.run(a.request, use_cache=not a.no_cache, ask=ask_founder if sys.stdin.isatty() else None)
        print(f"run: {run.dir}\n")
        show(run.records)
    else:
        show(load_run(a.run_id))


if __name__ == "__main__":
    main()
