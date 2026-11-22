# Verification record

General sourcing / human-review update: **82 offline tests pass; frontend production build passes.** Arbitrary contexts (including furniture, coffee, packaging, services and machinery) are accepted. Tests cover automatic durable handoff, paused worker behavior, review inbox, explicit escalation, budget-preserving retries, reference-required manual handling, idempotent resolution, cancellation and pending-payment reconciliation without duplicate payment effects.

The running app also passed an HTTP smoke check through Vite's proxy and the separate worker: synthetic cotton-bag request `7981d431-18e8-4545-8186-fa19a2fb5e0c` reached WAITING_FOR_HUMAN, appeared in the pending inbox, and was cancelled by a recorded reviewer action. It made **zero Gemini and zero paid Crustdata calls**. The raw test record is ignored at `tmp/general-review-check.json`. This verifies the service/UI-module/API boundary; browser interaction and visual QA remain unverified. No external human notification or real payment was sent.

Gemini clarification-recovery fix: **66 offline tests pass; frontend build passes.** The failed run `7ee41948-e599-4baa-a42a-9a7af514e2f7` was restored through the live HTTP API to WAITING_FOR_CLARIFICATION from its last validated intent. All three quantities remain 500. A repeated `500 of each` response is rejected before another generation. Recovery made **zero additional Gemini or Crustdata calls**. New tests cover safe optional-list/numeric representation normalization, field-only diagnostics, specific clarification questions, saved-intent recovery, structured-spec reuse, safety-block preservation and graceful insight call-limit handling. The failed historical response was not retained, so its exact original schema violation cannot be reconstructed; future failures record safe field/type and finish-reason diagnostics.

Vertex/live update, 22 November 2026: **58 offline tests pass; frontend production build passes.** Using the supplied external service-account setup, both extraction stages passed on Google `gemini-2.5-flash` through Vertex AI in `global`. The first structured request was rejected with HTTP 400; switching to Vertex's supported `response_schema` format resolved it. No non-Gemini models or other Cloud workloads were called.

A combined live run through Vite's HTTP proxy, the API and the separately running worker also passed: run `9165fd19-605d-4923-80dd-5c8b37a77690` resolved **Mouser / mouser.com** using Crustdata, completed both Gemini stages, and finished **COMPLETED_WITH_GAPS**. Crustdata charged **0.2 credits**; no order was created. Its raw result stays in ignored `tmp/live-app-check.json`. This verifies real request parsing, company identity and evidence synthesis; it does not verify live prices, stock, distributor authorization or delivery. Browser interaction/visual QA remains unverified.

The dashboard defaults to live data and Gemini enabled. The provider-credit limit remains 0.2 by default; broader search requires an explicit larger value in the request form. Model tokens and Crustdata credits remain separate, visible usage measures. Credentials and local `.env` are excluded from Git.

Gemini update: **45 offline tests pass**, including both LLM stages through the API/worker pipeline using mocked Gemini responses. Frontend API type generation and production build pass. Tests cover citation scope, unsupported/conflicting claims, numeric grounding, clarification, structured input precedence, cache reuse, model-call limits, truncation and ambiguous failures. No actual Gemini call has been made because GEMINI_API_KEY is not configured. No additional Crustdata credits were spent for this integration.

Verified locally on 22 November 2026. All test data and payments were synthetic. No deployment, external messages or real payment occurred.

Update after live integration: **27 offline tests pass**, and the regenerated API types plus frontend production build pass. Three actual Crustdata calls returned HTTP 200 and used 2.2 credits, but resolved a Google redirect rather than a supplier. This proves connectivity only. The redirect defect is fixed offline. The new default is a **0.2-credit identity-only cap**, with Search/Fetch blocked before execution; no additional paid call was made after that cap was requested. See `live-test.md` and `live-e2e-result.json` for the detailed result. The table below preserves the original fixture-only verification baseline.

## Results

| Check | Result | Evidence |
|---|---|---|
| Backend tests | PASS | `python -m pytest -q`: 21 passed in 2.25s |
| TypeScript and production bundle | PASS | `npm.cmd run build`: TypeScript succeeded; Vite emitted HTML, CSS and JS |
| OpenAPI generation | PASS | Exported FastAPI schema and regenerated frontend types |
| Frontend HTTP | PASS | GET `/` and `/src/main.tsx`: 200 |
| Frontend proxy to API | PASS | GET `/api/health`: 200, payments mock, live integrations unavailable |
| Electronics full HTTP flow | PASS | Vite proxy → API → separate worker → SQLite → cited decision, one PAID mock order and 4000-paise research ledger |
| Leather full HTTP flow | PASS | Same engine → three selected manufacturers → three unsent RFQ drafts and no orders |
| Actual subprocess crash/restart | PASS | First worker exited 42 after provider commit; restarted worker completed with one payment effect |
| Browser interaction / visual rendering | NOT VERIFIED | In-app browser host unavailable; Windows Computer Use then stopped because it could not determine the current browser URL confidently enough to enforce policy |

The backend test suite covers all ten requested scenarios, concurrent reservations, shipping/duty/tax arithmetic, incomplete/stale facts, deterministic ranking, exact approval amounts, stale plan invalidation, reconciliation before replanning, API errors, bounded rate-limit retries, cache behavior and secret-safe error text. It also checks that unsupported fixture specifications are not falsely matched.

The environment's preinstalled Starlette dependency emitted one PendingDeprecationWarning concerning `python_multipart`. It did not affect the tests. The scaffold does not use multipart uploads.

## Reproduce

From `backend`, run `python -m pytest -q`. From `frontend`, run `npm.cmd ci`, `npm.cmd run generate:api`, and `npm.cmd run build`.

With the API, worker and Vite development server running, execute `python scripts/smoke_http.py` from the root. This creates two synthetic demonstration runs in the configured database and checks both completed flows through the frontend proxy.

The initial sandboxed Vite commands were blocked by parent-directory read restrictions. Running the same build and preview with approved escalation succeeded. This is an execution-environment restriction, not an application build error.

Browser usability, visual layout and keyboard behavior still require manual review. HTTP, type and backend tests do not substitute for browser interaction testing. No claims about live supplier verification, real API connectivity, current taxes or real payment safety are made.
