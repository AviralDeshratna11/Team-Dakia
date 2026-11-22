"""python -m desk run examples/electronics_bom.yaml [--no-cache]
python -m desk show [run_id]          # latest run if omitted
"""

import argparse

import pandas as pd

from desk import pipeline, view
from desk.trace import load_run

pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 60)


def show(records: list[dict]) -> None:
    print(view.steps(records).to_string(index=False))
    print()
    for r in records:
        if r["step"] == "02_find" and r["output"]:
            o = r["output"]
            print(f"[{r['part']}] {o['status']}: {o['reason']}")
        if r["step"] == "05_decide" and r["output"]:
            print(f"[{r['part']}] DECISION: {r['output']['reason']}")


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
        run = pipeline.run(a.request, use_cache=not a.no_cache)
        print(f"run: {run.dir}\n")
        show(run.records)
    else:
        show(load_run(a.run_id))


if __name__ == "__main__":
    main()
