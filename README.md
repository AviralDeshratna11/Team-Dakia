# Sourcing Desk

**An auditable procurement agent for founders.** Describe what you need in plain language. The agent parses the request, finds suppliers, verifies that they are real and active, screens them for compliance, compares landed cost, and returns a shortlist with unsent RFQ drafts or a purchase plan with simulated, idempotent checkout. Every decision cites its evidence, every credit spent is on a ledger, and anything uncertain comes back to you as a question.

LLMs gather facts and draft text. **Plain code decides.** Eligibility, budgets, approvals and payments are deterministic and never delegated to a model.

```
you ─▶ chat UI ─▶ FastAPI ─▶ SQLite (durable run state) ◀─▶ worker
                                                    │
         ┌──────────────┬──────────────┬────────────┼──────────────┬───────────────┐
         ▼              ▼              ▼            ▼              ▼               ▼
      PARSING      DISCOVERING     VERIFYING    RESEARCHING     DECIDING      SCREENING ─▶ EXECUTING
   Gemini + rules  Crustdata /    identity,     buy data only   landed cost,  compliance    RFQ drafts /
                   fixtures       factory,      if it changes   reliability,  (owners,      mock checkout
                                  stability     the decision    eligibility   adverse media)
         │                                                                        │
         └──── WAITING_FOR_CLARIFICATION · WAITING_FOR_APPROVAL · WAITING_FOR_HUMAN ┘  (asked in chat)
```

## Contents

- [Capabilities](#capabilities)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Testing](#testing)
- [Deployment](#deployment)
- [Safety model](#safety-model)
- [Limitations](#limitations)
- [Documentation](#documentation)

## Capabilities

| Area | What it does |
|---|---|
| **Conversational UI** | One prompt box. Live step-by-step trace of what the agent is doing and thinking, clarifying questions asked as replies, follow-ups and approvals in the same thread, and an evidence panel per supplier. |
| **Request understanding** | Gemini 2.5 Flash on Vertex AI produces a schema-validated `ParsedIntent` (items, parameters, quantities with verbatim excerpts). Code validates every field; anything missing becomes a single focused question. |
| **Live supplier discovery** | Crustdata Company Search with region filters, Company Enrich for headcount, locations, taxonomy, people and news, and count-only Job Search to tell factories from traders. Gemini can refine one follow-up search and read up to two supplier pages (URL Context). |
| **Verification** | Signals are `pass`, `fail` or `not confirmed`. A missing fact never counts as a pass, and conflicting records stay unconfirmed. Identities merge only on canonical company IDs. |
| **Compliance screen** | Owners and directors, adverse media with namesake clearing, and front-company signs (recent founding, tiny headcount, PO-box addresses, shared addresses). Verdict: block, hold for review, or approve. |
| **Decision engine** | Integer-paise landed cost (goods, shipping, duty, tax with `Decimal` rounding), quantity tiers, MOQ, deadline and stock checks. Reliability and evidence coverage are scored separately. Deterministic ordering. |
| **Spend control** | Every Crustdata credit is reserved in a persistent journal before the call, under a ceiling that can be lowered mid-run. Data is bought only when it can change the decision. Gemini is capped at 4 generations per run, with cached responses. |
| **Execution** | RFQ drafts addressed to a named decision-maker (never sent). Purchase orders above the approval threshold wait for an explicit, version-bound approval. Mock checkout with stable idempotency keys survives crashes and timeouts without double-paying. |
| **Human hand-off** | When verified sourcing cannot finish, the run pauses in `WAITING_FOR_HUMAN` with a durable task. The chat lets you add detail to retry, or cancel. |
| **Team CLI** | The `desk` package runs the same pipeline from a terminal: seller investigation, mock RFQ threads, and agent-drafted category packs. |

## Architecture

A modular monolith with a **separate durable worker**. The API only records intent. The worker advances each run one persisted step per transaction (`BEGIN IMMEDIATE` on SQLite), so a crash at any point resumes from the last committed state.

| Layer | Path | Responsibility |
|---|---|---|
| Contracts | `backend/app/contracts/` | Pydantic models: the source of truth, exported to [OpenAPI 3.1](docs/api/openapi.json) and generated TypeScript types |
| Domain | `backend/app/domain/` | Pure logic: decision engine, compliance, supplier signals, state machine. Imports no HTTP or SDK code. |
| Workflow | `backend/app/workflow/` | `Orchestrator` state transitions and the polling `worker` |
| Services | `backend/app/services/` | LLM extraction, specification parsing, supplier search, product pages, quotes, human review |
| Providers | `backend/app/providers/` | Crustdata (journaled, capped), Gemini via Vertex AI (Gemini-only guard), fixtures, mock payments, RazorpayX payout preview |
| Persistence | `backend/app/persistence/` | SQLite run documents, reservations, double-entry-style ledger |
| Packs | `backend/app/packs/` | Validated category policies (electronics, leather goods, general): scoring, freshness, demo tax and duty |
| API | `backend/app/main.py` | FastAPI routes, passcode gate and spend caps for hosted use, static UI serving |
| UI | `frontend/src/` | React 19, TypeScript, Vite and Tailwind chat client. Types generated from OpenAPI. |

Deep dive: [architecture](docs/design/architecture.md), [contracts](docs/design/contracts.md), [LLM layer](docs/design/llm-layer.md), [human review](docs/design/human-review.md).

## Repository layout

```
.
├── backend/            FastAPI service + workflow worker (Python 3.11+)
│   ├── app/            contracts · domain · workflow · services · providers · persistence · packs
│   └── tests/          200 offline tests: unit, API inventory, benchmark, hosted guards
├── frontend/           React chat UI (Vite, TypeScript, Tailwind)
├── desk/               Team CLI agent and category packs (Python 3.12+, uv)
├── tests/              CLI tests: the brief's hard cases
├── deploy/             Container entrypoint (API + supervised worker)
├── scripts/            OpenAPI export, live end-to-end runs, Vertex check, worker launcher
├── examples/           Sample requests for the CLI
├── fixtures/           Synthetic supplier catalogues for offline demos
├── notebooks/          Walkthrough notebook
├── docs/               design · api · guides · quality · planning · brief  (index: docs/README.md)
├── Dockerfile          Multi-stage image: UI build → Python runtime
├── render.yaml         Render blueprint (secrets entered in the dashboard, never committed)
└── Makefile            install · api · worker · web · serve · test · build · contracts · docker
```

## Quick start

Requires Python 3.11+ and Node.js 20.19+. Without any keys, the system runs fully offline on synthetic fixtures.

```bash
make install          # .venv + backend (with tests) + frontend deps
make api              # terminal 1: http://127.0.0.1:8000  (OpenAPI docs at /docs)
make worker           # terminal 2: advances runs, same SOURCING_DB as the API
make web              # terminal 3: chat UI at http://127.0.0.1:5173
```

Or run everything on one port, the way it's deployed: `make serve`, then open http://127.0.0.1:8000.

Try these in the chat:

> Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft the first email to each.

> Source ESP32-S3 modules, a 3.3V 500mA SOT-23 LDO regulator, 10uF 0805 capacitors and USB-C connectors. 500 of each, delivered to Bengaluru within three weeks.

## Configuration

All configuration is through environment variables (see [`.env.example`](.env.example)). The API and the worker must share `SOURCING_DB`.

| Variable | Default | Purpose |
|---|---|---|
| `SOURCING_DB` | `./sourcing.db` | Workflow database. Sibling files hold payments, provider credits and the LLM journal. |
| `GOOGLE_APPLICATION_CREDENTIALS` | unset | Service-account JSON for Gemini on Vertex AI. Keep it **outside** the repository. |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | from key / `global` | Vertex AI project and location |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Only bare `gemini-*` IDs are accepted |
| `GEMINI_BACKEND` | auto | `vertex` (service account) or `developer` (`GEMINI_API_KEY`) |
| `CRUSTDATA_API_KEY` | unset | Enables live supplier data. Without it, runs use fixtures. |
| `ACCESS_CODE` | unset | Hosted only: shared passcode, required as `X-Access-Code` on `/api/*` |
| `MAX_RUNS_PER_DAY` | unset | Hosted only: new requests per rolling 24 hours |
| `MAX_CREDIT_BUDGET` | unset | Hosted only: highest Crustdata ceiling a run may set |
| `FRONTEND_DIST` | unset | Serve the built UI from the API process |

Keys are read only by the worker and API processes. They are never sent to the browser, logged, or committed (`.gitignore` blocks `.env` files and service-account keys).

## Testing

```bash
make test        # backend: 200 passed, 3 xfailed (offline: temp SQLite, mocked HTTP, no keys)
make test-cli    # team CLI: the brief's hard cases
make build       # strict TypeScript check + production UI build
```

The backend suite covers state transitions, the decision engine, compliance, credit journaling, payment idempotency and crash recovery, Gemini output validation, and the human-review flow. It also includes an **inventory test that exercises every API route** and checks the checked-in OpenAPI spec. After changing a model, run `make contracts` to regenerate `docs/api/openapi.json` and `frontend/src/types/schema.d.ts`.

See [verification](docs/quality/verification.md) and the [benchmark](docs/quality/benchmark.md).

## Deployment

One container runs the API, a supervised worker and the built UI ([Dockerfile](Dockerfile), [deploy/start.sh](deploy/start.sh)). [render.yaml](render.yaml) deploys it to Render. The Vertex AI key is mounted as a secret file, and the passcode, run limit and credit cap are set as environment variables. Full steps: [deployment guide](docs/guides/deployment.md).

## Safety model

- **No real-world side effects.** RFQs are drafts and are never sent. Payments go to a local synthetic wallet. The RazorpayX payout is a preview only.
- **Models cannot authorize anything.** Gemini output is schema-validated, must cite its evidence, and is stored as `unverified`. It never changes eligibility, budgets or approvals.
- **Gemini only.** A model-ID guard rejects non-Gemini models before authentication.
- **Bounded spend.** Credits are journaled before each call and checked against a ceiling the user can lower. Ambiguous paid calls are never retried blindly.
- **Exactly-once intent.** `PAY_PENDING` is persisted before checkout. Retries reuse the same idempotency key, and approvals are bound to the plan version and amount.

## Limitations

- SQLite with a single writer lock suits a demo. Concurrent production use needs leased jobs and Postgres.
- Without live data, the fixture catalogue covers only the two reference requests above. Other products end in a human hand-off.
- Tax, duty and FX figures are demo assumptions in the category packs, not verified rules.
- Chat history is stored in the browser. The hosted passcode is a shared demo gate, not user authentication.

## Documentation

The full index is at [docs/README.md](docs/README.md): design, API, guides, quality, planning, and the original [project brief](docs/brief/sourcing-desk-project-brief.pdf).

## Team

Built at ContextCon 05 by Team Dakia: Palak Manocha, Aviral Deshratna Saxena and Siddharth Khare.
