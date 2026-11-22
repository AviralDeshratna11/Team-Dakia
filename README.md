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
uv run python -m desk rfq                                 # RFQ threads; `rfq <id>` shows the emails
uv run python -m desk rfq reply <id> "..."                # reply as the vendor (a teammate)
uv run python -m desk pack approve <name>                 # approve a pack an agent drafted for a new field
```

Examples: `examples/electronics_bom.yaml` (the brief's BOM, in the founder's words),
`examples/vague_request.yaml` (asks one round of questions), `examples/leather_request.yaml`
(a field with no pack: an agent drafts one).

## How it's built

A fixed six-step workflow (repeatable, auditable, bounded cost) with bounded agents inside it, only where the
path isn't known in advance. Agents gather facts and draft; plain code decides.

1. **Understand** (Gemini, structured output). A gate: anything unclear → one round of questions → re-read.
   1b. **Draft a pack** (agent) for a field we've never sourced; the founder approves it.
2. **Find**: catalogue distributors by fixed recipe (DigiKey India, LCSC), or makers by Crustdata Company Search.
   2b. **Investigate** unknown sellers from open search (agent).
3. **Check** each seller company with Crustdata Identify + Enrich, scored in code.
   3b. **RFQ** sellers with no price (agent, mock mailbox).
4. Buy data: not built yet.
5. **Decide**: rules in order, then lowest landed cost. Flagged options go to the founder, never picked silently.
6. **Order**: saved states, idempotency keys, approval limit, mock checkout; never pays twice.

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
| `desk/agent.py` | The bounded tool-calling agent loop (turns, credit budget, typed report, cached turns) |
| `desk/investigate.py` | Agent: investigate unknown sellers |
| `desk/rfq.py` | Agent: RFQ email threads (mock mailbox) |
| `desk/draft_pack.py` | Agent: draft a category pack for a new field |
| `desk/pipeline.py` | Runs the steps in order and records each one |
| `desk/crustdata.py` | Crustdata client: retry, backoff, disk cache |
| `desk/extract.py` | Reads price, stock and MOQ from fetched pages |
| `desk/trace.py` | Writes `runs/` |
| `desk/view.py` | Tables for the notebook and CLI |
| `desk/packs/*.yaml` | Per-field config: distributors, fx rates, discovery query |
| `examples/` | Sample requests |
| `NOTES.md` | What we tried, what worked, what didn't |
