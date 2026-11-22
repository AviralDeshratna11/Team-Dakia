# Sourcing Desk

Tell it what you need to buy. It finds suppliers that deliver to India, checks they are real factories and not middlemen, screens who runs them, and prepares the order or RFQ, asking a person only when the answer would change the result.

This repository has two parts built by the team on the same day:

| Part | Folder | What it does |
|---|---|---|
| Agent pipeline | `desk/`, `tests/`, `notebooks/` | Step-by-step sourcing agents on Gemini (Vertex AI): spec gate, supplier search, seller investigator, RFQ agent, mock payments. See part 1. |
| Web app | `backend/`, `frontend/`, `scripts/` | FastAPI + worker + React dashboard: live Crustdata supplier checks, compliance screen on finalists, credit ledger, human review, idempotent mock orders. See part 2. |

## Part 1: agent pipeline (`desk/`)

A founder hands over a list of parts. The agent finds suppliers, checks they're real, picks the best price it can trust, and places the orders, with a cited reason for every pick.

Work in progress (ContextCon 05, BITS Pilani). The full README comes before submission.

### Run it

```bash
uv sync
echo "CRUSTDATA_API_KEY=..." > .env
# Gemini on Vertex AI (steps 1, 1b, 2b, 3b): a service-account key kept OUTSIDE the repo
echo "GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json" >> .env
echo "GOOGLE_CLOUD_PROJECT=your-project" >> .env

uv run python -m desk run examples/electronics_bom.yaml   # whole pipeline (asks if anything is unclear)
uv run python -m desk show                                # last run, step by step
uv run python -m desk orders                              # orders, their state, and what checkout charged
uv run python -m desk approve <order_key>                 # approve an order held above the limit
uv run python -m desk run <request> --crash-after-charge  # crash mid-payment; re-run to see it resume
uv run python -m desk rfq                                 # RFQ threads; `rfq <id>` shows the emails
uv run python -m desk rfq reply <id> "..."                # reply as the vendor (a teammate)
uv run python -m desk pack approve <name>                 # approve a pack an agent drafted for a new field
uv run python -m desk wallet [fund <inr> | limit <inr>]   # the agent's wallet: balance, limit, ledger, events
uv run pytest -q                                          # the brief's hard cases (offline, no keys needed)
```

Examples: `examples/electronics_bom.yaml` (the brief's BOM, in the founder's words),
`examples/vague_request.yaml` (asks one round of questions), `examples/leather_request.yaml`
(a field with no pack: an agent drafts one).

### How it's built

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
5b. **Combine shipping** per vendor across the lines picked from it.
6. **Order**: saved states, idempotency keys, approval limit; never pays twice.

**Payments (mocked, real-gateway shaped)** in `desk/payments.py`: a `PaymentProvider` interface; a mock gateway
with idempotent payment intents, retries, refunds and an event log (the webhooks); an agent wallet with a balance,
a per-payment limit and a ledger. A real provider (e.g. Razorpay) is one class implementing the same interface.

### Tests

`tests/test_hard_cases.py` runs the brief's ten hard cases offline (fake Crustdata, fake Gemini, temp
databases): clean pick cited and paid once; storefront with no company record rejected; two storefronts,
one company; trader caught by its hiring; missing stock never picked; wallet ceiling cut mid-run; 429 retried
and an unrunnable check never picked; crash after charge paid once; vague spec asks once; leather on the same
engine. Case 6 (a data budget cut) is skipped: buying data is deferred.

To step through it yourself, open `notebooks/walkthrough.ipynb` in VS Code and pick the `.venv` kernel.

Every step's input, output, API calls and timing are saved to `runs/<run_id>/<step>__<part>.json`. Crustdata responses are cached in `.cache/`, so re-runs are free.

### Layout

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
| `desk/payments.py` | Mock payment provider + agent wallet (real-gateway shaped) |
| `tests/` | The hard cases, offline |
| `desk/pipeline.py` | Runs the steps in order and records each one |
| `desk/crustdata.py` | Crustdata client: retry, backoff, disk cache |
| `desk/extract.py` | Reads price, stock and MOQ from fetched pages |
| `desk/trace.py` | Writes `runs/` |
| `desk/view.py` | Tables for the notebook and CLI |
| `desk/packs/*.yaml` | Per-field config: distributors, fx rates, discovery query |
| `examples/` | Sample requests |
| `NOTES.md` | What we tried, what worked, what didn't |

## Part 2: web app (`backend/`, `frontend/`)

### General sourcing and human assistance

The main form now accepts any product or service, with optional free-text context instead of fixed category choices. Choose **Purchase plan** or **Supplier shortlist and RFQ drafts** independently. There is no preselected electronics supplier. Existing fixture catalogues remain limited offline examples.

When verified sourcing cannot finish, a durable task appears in the **Human review inbox** and the run pauses in `WAITING_FOR_HUMAN`. A reviewer can revise and retry, record manual handling with a reference, or cancel. The same budgets, caches and payment identities are retained. Human-reported outcomes are separate from automated purchases; real payments and outside notifications are not implemented. See [the general sourcing and human-review workflow](docs/general-and-human-review.md).

A local procurement-agent skeleton for founders who currently compare suppliers, verify companies and calculate landed costs themselves. It turns a reference electronics BOM into an auditable plan and simulated orders, or a leather-goods request into a manufacturer shortlist and unsent RFQ drafts.

**Live mode finds and checks real suppliers through Crustdata.** For leather goods it searches Crustdata's company graph by state, checks each candidate with Company Enrich and count-only Job Search, and decides in plain code whether it is a real, active factory or a trader. It stops paying once the shortlist is full, buys a 1-credit news check only when it could change the shortlist, and drafts an RFQ to a named decision-maker at each chosen maker. Every credit is on the run's ledger with the reason it was spent, under a ceiling the founder can lower mid-run (default 25 credits). Sample-data mode still runs offline with synthetic companies. Payments stay simulated and RFQs stay drafts: no vendor is contacted and no real money moves.

### Live supplier checks (Crustdata)

| Step | Crustdata call | Cost | What it decides |
|---|---|---|---|
| Find candidates | Company Search, ranked query + `locations.state` filter | 0.03 per result | Real companies with canonical IDs from the start |
| Check each one | Company Enrich (basic info, headcount, locations, taxonomy, people, news, revenue) | 2 per company, reused free for 7 days | Active and stable, office in the region, named contact |
| Factory or trader | Job Search, `limit: 0` counts of production vs sales posts | ~0 | Supporting evidence next to NAICS code and listed industry |
| Only if it changes the result | Web Search, news, India, last 12 months | 1 | Complaints or bad news for a borderline maker |

Signals are True, False or "not confirmed"; a missing fact never counts as a pass. Conflicting records (a manufacturing industry with a wholesale NAICS code) stay "not confirmed". A 429 retries on the next worker step, up to 3 times; a failed check leaves that supplier unverified instead of failing the run.

**Live result, 22 Nov 2026** — the handbook's sample request ("Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft the first email to each"):

- 12 real Uttar Pradesh companies found in the company graph.
- **5 rejected as traders or retailers.** Their industry records read Wholesale Import and Export, Retail Apparel and similar, even though their descriptions claim manufacturing.
- **AKI India Ltd rejected for a red-flag headline.** It's a 32-year-old Kanpur tannery that passed every other check, but news showed a SEBI penalty for disclosure violations.
- **Cinni Enterprises rejected as too new.** It was founded in 2026.
- **Shortlist:** Movement Sparkle, TOGASAB and Ibbu Lifestyle. All three are listed as leather product manufacturers. A news check, bought only because it could change the shortlist, came back clean for each. Their stability is shown as *not confirmed*, because the records have too little age and headcount data.
- **Cost:** 3 RFQ drafts for 8.36 credits. Eight profiles were reused from an earlier run at 0 credits. Two news checks were skipped because the shortlist was already full.

### Compliance screen on finalists (Trust and compliance, themes 5.1 and 5.2)

Before any RFQ or order goes out, each finalist gets a vendor check. It covers the gap that has cost Indian buyers most: a front company that looks real on paper.

1. **Who runs it.** Owners and directors come from Company Enrich `people`. If the profile has none, Person Search looks for current employees with founder, owner, proprietor, director, partner or CEO titles.
2. **Adverse media on each director.** An Indian news search covers the last 3 years. **Namesakes are cleared**: an article counts only if it also names the company, its domain or its city. A "fraud" headline about someone else with the same name is listed as cleared, not counted.
3. **Front-company signs.** These are: founded under 2 years ago; 3 or fewer staff for a supposed factory; an address on record that is a list of post offices rather than a street address; the same street address as another candidate.
4. **Verdict.**
   - **Block:** adverse news tied to the company's leadership. The supplier leaves the shortlist and the run goes back to research to qualify a replacement.
   - **Hold:** the owner can't be confirmed, or there are front-company signs. The supplier stays on the shortlist, but its RFQ is marked "held for your review before sending".
   - **Approve:** none of the above.

On the live Uttar Pradesh request, all three finalists were **held**. Crustdata has no named owner for any of them, and their address records are post-office lists. The agent says so instead of letting an unverified vendor through. The screen added 0.36 credits: Person Search and news searches that found nothing are free.

Run one live request from the command line (reads `CRUSTDATA_API_KEY` from `.env`, never printed):

```bash
.venv/bin/python scripts/live_leather.py "Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft the first email to each." --credits 25
```

For the dashboard, start the worker with the key in its environment: `set -a; source .env; set +a; .venv/bin/python -m app.workflow.worker` from `backend`.

### Gemini: two extraction stages

**Configured locally with the supplied Vertex AI service account:** Google Gemini only, `gemini-2.5-flash`, location `global`. The external credential is not copied into the project. `scripts/start_worker.py` loads the ignored `.env` setup, and the model ID guard rejects non-Gemini models before authentication. Two actual Vertex extraction checks passed. See [Vertex setup and verification](docs/llm-layer.md).

The dashboard defaults to **Live** and **Use Gemini** enabled. Keep a supplier domain and the default **0.2-credit** budget for the minimal identity test. To search for suppliers instead, clear the domain and explicitly choose a larger credit budget. The application never increases it automatically. Configure delivery/category/budget form fields consistently with the request text; conflicts require clarification. Fixture mode remains available for offline demos.

Enable the Gemini checkbox in a new dashboard request to use an LLM for (1) parsing the founder's requirements and (2) extracting useful facts, summaries and gaps from Crustdata evidence. Outputs are validated and saved; model-derived facts never authorize spending or silently become verified quotes. Normally two model calls are made, with four attempts maximum per run and persistent response caching.

Stop any existing worker first. From `backend`, start a replacement with hidden key prompts:

```powershell
python ../scripts/start_worker.py --gemini --crustdata
```

For Vertex, set `GEMINI_BACKEND=vertex`, `GOOGLE_APPLICATION_CREDENTIALS` to the external JSON, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global`, and `GEMINI_MODEL=gemini-2.5-flash` in the ignored `.env`; no Gemini API-key prompt is needed. `CRUSTDATA_API_KEY` can be entered at the worker's hidden prompt. A separate `GEMINI_BACKEND=developer` configuration supports an actual `GEMINI_API_KEY`, but that route cannot use the service-account JSON. Keep API and worker in the same working directory. The frontend never receives these keys. Fixture mode with Gemini off still needs no key. The Crustdata credit budget and Gemini token usage are separate.

### Run locally

Requires Python 3.11+ and Node.js 20.19+ or 22.12+. Commands below assume PowerShell, starting at the repository root. Use `npm.cmd` on Windows if execution policy blocks `npm.ps1`; on macOS/Linux use `npm`.

Create and install the backend environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[test]"
```

Terminal 1, from the root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Terminal 2, from the root:

```powershell
.\.venv\Scripts\python.exe -m app.workflow.worker
```

Terminal 3:

```powershell
cd frontend
npm.cmd ci
npm.cmd run dev
```

Open the Vite Local URL, normally **http://127.0.0.1:5173**. API documentation: **http://127.0.0.1:8000/docs**. The dashboard proxies `/api` to port 8000. API and worker must use the same working directory (root above), so both find the same database.

Python imports work from the root after the editable backend installation. Alternatively, with the dependencies already installed, run both Python commands from `backend`, omitting `--app-dir backend`.

### First demo

1. Leave the electronics example and choose **Missing stock** to demonstrate purchased research.
2. Create the request. The separate worker advances one saved step per second.
3. Inspect Plan for eligible candidates, the cheaper unresolved storefront, landed costs and evidence.
4. Inspect Research for the mock ₹40 purchase and whether it changed the decision.
5. The ₹100,000 default approval threshold is below the 500-piece ESP32 order. Approve its exact amount under Orders & RFQs. Other orders below the threshold are automatically authorized by policy.
6. Choose Leather goods in a new request to run the same engine and produce three unsent RFQs.

For an uninterrupted auto-authorized demo, set the approval threshold to ₹1,000,000. This is an explicit simulation policy, not a recommendation for real purchases.

### Checks and contract generation

```powershell
cd backend
python -m pytest -q
python ../scripts/export_openapi.py
cd ../frontend
npm.cmd run generate:api
npm.cmd run build
```

Use your virtual environment's Python if the shell is not activated. Backend tests use temporary SQLite databases and an in-memory HTTP mock transport; they need neither keys nor internet. Frontend installation needs internet once. The generated `docs/openapi.json` and `frontend/src/types/schema.d.ts` are checked-in contracts. Regenerate both after changing a backend model.

### Configuration and storage

`SOURCING_DB` optionally specifies an absolute SQLite file path. Set it in the environment of **both** API and worker. `.env.example` documents configuration; `.env` files are not loaded automatically.

```powershell
$env:SOURCING_DB = 'C:\path\to\sourcing.db'
```

- Workflow documents, events, reservations and ledger: `sourcing.db`.
- Independent mock payment provider: `sourcing.db.payments.db`.
- All currency amounts are integer paise, reported in INR. Provider credit reservations and actual usage have a separate persistent journal; no credit-to-INR conversion is invented.
- UTC is used internally. Dashboard dates are displayed in Asia/Kolkata.
- Pack files under `backend/app/packs` contain validated scoring rules, freshness and **demo** tax/duty assumptions.

### What is implemented

- FastAPI/OpenAPI contracts and React/TypeScript/Vite/Tailwind dashboard.
- Explicit state transitions and a separate resumable worker.
- Two validated category packs sharing one engine.
- Fixture alias deduplication using canonical company IDs.
- Separate hard eligibility, reliability score and evidence coverage.
- Quantity tiers, shipment/unit shipping, configurable duty/tax and Decimal rounding.
- Research value heuristic, atomic reservations, budget reductions and separate spending accounts.
- Version-bound approvals, durable payment intent and independent mock-provider idempotency.
- Timeout, rate-limit, missing-stock and crash demo scenarios.
- RFQ drafts with stable identities; repeated resume does not duplicate work.

### Limits and next steps

With Gemini disabled, the fixture parser recognizes only the reference ESP32-S3, 3.3V/500mA/SOT-23 LDO, 10uF/0805 capacitor and USB-C examples. Gemini supports structured extraction from broader wording when enabled; a BOM build count without line quantities still triggers clarification. Structured specs are supported, but unmatched fixture specifications remain ineligible; no arbitrary product matching is claimed. The leather fixture covers Uttar Pradesh manufacturer shortlists. Gemini extraction does not expand the fixture catalogue.

The worker serializes API mutations and workflow advancement in SQLite write transactions. Live calls currently hold that lock during bounded network I/O; separate leased jobs are needed before concurrent production use. This is a local hackathon demonstration. The UI lists complete runs without pagination. There is no production authentication; bind locally.

Each item quote has its own shipment and shipping charge; multi-item shipment consolidation is deferred. Taxes and FX require verified rules before real use. The research heuristic buys missing stock data only when it could make a candidate eligible and cheaper; it does not estimate a precise economic value of information. The paid-data response is local simulation, not an implementation of the x402 wire protocol.

Next: independently verify extracted live quote facts, add production tax/shipping calculation, then a deployment/authentication plan. Live workers read `CRUSTDATA_API_KEY` from their environment. Supply `supplier_domains: ["mouser.com"]` for the minimum-cost identity-only API flow; Search/Fetch exceed the default 0.2 cap and are blocked. `python scripts/live_e2e.py` prompts for the key securely and runs that minimal profile without Gemini. Real payments, outreach, automatic pack generation, negotiation and delivery tracking are outside this skeleton.

See [architecture](docs/architecture.md), [contracts](docs/contracts.md), [demo](docs/demo.md), [team plan](docs/implementation-plan.md), and [verification](docs/verification.md).
