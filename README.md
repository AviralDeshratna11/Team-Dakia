# Sourcing Desk

A founder hands over a list of parts. The agent finds suppliers, checks they're real, picks the best price it can trust, and places the orders, with a cited reason for every pick.

Work in progress (ContextCon 05, BITS Pilani). The full README comes before submission.

## Run it

```bash
uv sync
echo "CRUSTDATA_API_KEY=..." > .env

uv run python -m desk run examples/electronics_bom.yaml   # whole pipeline (asks if anything is unclear)
uv run python -m desk show                                # last run, step by step
uv run python -m desk orders                              # orders, their state, and what checkout charged
uv run python -m desk approve <order_key>                 # approve an order held above the limit
uv run python -m desk run <request> --crash-after-charge  # crash mid-payment; re-run to see it resume
```

To step through it yourself, open `notebooks/walkthrough.ipynb` in VS Code and pick the `.venv` kernel.

Every step's input, output, API calls and timing are saved to `runs/<run_id>/<step>__<part>.json`. Crustdata responses are cached in `.cache/`, so re-runs are free.

## Layout

| Path | What |
|---|---|
| `desk/schemas.py` | The shared contract: Request, Spec, Quote, FindResult, Decision, Order |
| `desk/understand.py` | Step 1: founder's words → specs (Gemini), with the clarification gate |
| `desk/steps.py` | Steps 2 and 5: find suppliers, decide (rules + landed cost) |
| `desk/check.py` | Step 3: is each seller a real, stable company (Crustdata Identify + Enrich) |
| `desk/orders.py` | Step 6: orders paid exactly once (SQLite states, idempotency keys, mock checkout) |
| `desk/llm.py` | Gemini on Vertex AI, cached and traced |
| `desk/pipeline.py` | Runs the steps in order and records each one |
| `desk/crustdata.py` | Crustdata client: retry, backoff, disk cache |
| `desk/extract.py` | Reads price, stock and MOQ from fetched pages |
| `desk/trace.py` | Writes `runs/` |
| `desk/view.py` | Tables for the notebook and CLI |
| `desk/packs/*.yaml` | Per-field config: distributors, fx rates, discovery query |
| `examples/` | Sample requests |
| `NOTES.md` | What we tried, what worked, what didn't |
