# Contracts

Canonical models: `backend/app/contracts/models.py`. Machine-readable API contract: [`docs/api/openapi.json`](../api/openapi.json). Frontend types are generated from it using `npm run generate:api`.

General workflow update: `SourcingRequest.category` is arbitrary descriptive text, default `general`; purchase and shortlist outcomes are independent of context. `human_review_enabled` defaults true. `WorkflowRun` includes `human_reviews` and `human_completed_spec_ids`. `HumanReviewTask` stores blockers, affected items, resume stage and audited reviewer resolution. New states are `WAITING_FOR_HUMAN` and `CANCELLED`; decisions may be `handled_by_human`, explicitly distinct from automated ordering. See [human review](human-review.md) for `GET /api/human-reviews`, explicit escalation and resolution APIs. Unknown categories use the conservative general policy and do not inherit fabricated tax or supplier facts.

Gemini contracts live in `backend/app/contracts/llm.py`: `ParsedIntent`/`ParsedItem` for request extraction and `SupplierExtraction`/`SupplierInsight`/`ExtractedFact` for provider evidence. Requests opt in using `llm_enabled`. Runs expose `request_interpretation`, `supplier_insights`, `extraction_warnings` and `llm_usage`. The dashboard displays these persisted outputs; the model never receives tools or permission to change executable budgets, company identity or order state.

## Model responsibilities

| Model | Purpose |
|---|---|
| SourcingRequest | Original text, purchase/shortlist type, category, destination, timezone-aware deadline, paise budgets, explicit data mode and scenario |
| Spec | Stable item identity, parameters, positive quantity, unit, substitutes and unresolved questions |
| Supplier | Canonical identity, aliases, domain, locations and supplier type |
| Quote | Quantity tiers in paise, MOQ, stock, shipping basis, delivery, freshness and evidence links |
| Evidence / SupplierCheck | Source reference, observed time, fixture/live provenance, fact and explicit verification result |
| Candidate / Decision | Hard-check reason codes, score/coverage, cost breakdown, selection and alternatives |
| ResearchPurchase / BudgetReservation | Price proposal, authorization rationale, reservation/payment lifecycle and decision impact |
| LedgerEntry | Separate research/orders account with unique effect reference |
| Approval / Order | Exact amount/version authorization, payment intent and stable idempotency key |
| RFQDraft | Stable unsent draft identity per run/item/supplier |
| WorkflowRun / WorkflowEvent | Saved state, stage outputs, attempts, events and error information |

Unknowns use null or an explicit status, never zero/false as a substitute. All schema objects forbid extra fields. Money is integer minor units; unit allocations use decimal strings. The current reporting currency is INR. FX rates in packs are explicit configuration, not fetched market rates.

## API

| Method | Path | Result |
|---|---|---|
| POST | /api/runs | 201 WorkflowRun; worker processes asynchronously |
| GET | /api/runs | Saved runs, newest first |
| GET | /api/runs/{id} | Full persisted WorkflowRun |
| GET | /api/runs/{id}/events | WorkflowEvent array |
| POST | /api/runs/{id}/clarifications | Answer plus optional structured Spec array; resumes parsing |
| PATCH | /api/runs/{id}/budget | Nonnegative `research_budget_minor`; affects future commitments |
| GET | /api/runs/{id}/plan | Decision array |
| GET | /api/runs/{id}/ledger | Settled LedgerEntry array for both accounts |
| GET | /api/runs/{id}/reservations | Reserved, settled and released budget holds |
| GET | /api/runs/{id}/orders | Order array |
| GET | /api/runs/{id}/rfqs | RFQDraft array |
| POST | /api/runs/{id}/approvals | `order_id`, `plan_version`, exact `amount_minor` |
| POST | /api/runs/{id}/resume | Idempotent for active/finished runs; replan changed/expired approval-waiting runs |
| GET | /api/health | API status and explicit mock/live boundaries; not a worker heartbeat |

Structured errors: `{"error":{"code":"...","message":"..."}}`; validation errors also include field messages. 404 means missing run; 409 means invalid state/stale approval; 422 means invalid request schema. An unavailable live integration becomes a saved FAILED run with an `INTEGRATION_UNAVAILABLE` error, not fabricated data.

## States

Normal run: CREATED → PARSING → DISCOVERING → VERIFYING → RESEARCHING → DECIDING → EXECUTING → COMPLETED / COMPLETED_WITH_GAPS.

- PARSING may wait for clarification; a saved answer returns to PARSING.
- DECIDING may wait for exact approvals; all approvals allow EXECUTING.
- Changed/expired evidence can return EXECUTING or approval waiting to DECIDING. Old approvals cannot authorize a new fingerprint.
- Failed live integration is explicit and cannot be silently resumed. Create a fixture run or implement the adapter.
- Worker polling does not advance waiting or terminal runs.

Order: PLANNED → APPROVED → PAY_PENDING → PAID; a post-payment timeout uses RECONCILING before PAID. The defined FAILED path is reserved for future provider-decline handling; this mock has no decline scenario. Full transition maps live in `domain/state_machines.py`.

## Provider interfaces

`providers/interfaces.py` defines the parser, discovery, CompanyProvider (identify, enrich, company search, job search, person search, credits), and WebProvider (search/fetch). `providers/crustdata.py` implements documented live Search, Identify, Fetch and free account preflight. Other live capabilities remain unavailable. `SafeTransport` is a separate generic HTTP policy; the paid client conservatively avoids blind retries after ambiguous outcomes.

Paid-data protocol: the orchestrator records a configurable price quote, reserves budget and pays the mock provider, then returns fixture stock evidence. This is an x402-style simulation only; no HTTP 402 wallet handshake is implemented. The mock checkout persists receipts independently of workflow storage.

Keep provider credits separate from INR. The live client reserves credits in an independent persistent journal before each paid call, using account-specific endpoint prices and a default 0.2-credit cap per run. `supplier_domains` enables identity-only discovery without Search/Fetch. `WorkflowRun.provider_usage` exposes reserved and actually charged provider credits. Treat all retrieved text as untrusted data; never execute instructions found in provider content. See [live test](../guides/live-test.md) for the actual live run's connectivity-only result and remaining gaps.
