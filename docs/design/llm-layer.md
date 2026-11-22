# Gemini integration

## Vertex AI service-account setup

The supplied access README specifies Google Gemini through Vertex AI. The integration now supports that route with `google-genai`, using `vertexai=True`, the credential's own project and location `global`. The configured/tested model is `gemini-2.5-flash`. Model IDs must be bare `gemini-*` names; other families and publisher/resource paths are rejected before authentication. Only synchronous Gemini content generation is exposed: no project changes, resource creation, partner models, tuning, batch jobs, deployment, Secret Manager or other Cloud workloads.

The service-account JSON stays at its original external location. The ignored local `.env` contains only configuration and the path to it, not the private key. `scripts/start_worker.py` loads that file, respecting existing process environment values. The SDK receives explicit service-account credentials and uses the necessary OAuth authentication flow; it does not use the JSON as a Gemini Developer API key.

Copy the non-secret template settings from `.env.example` to a local `.env`, specifying `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global`, `GEMINI_BACKEND=vertex` and `GEMINI_MODEL=gemini-2.5-flash`. From `backend`, start `python ../scripts/start_worker.py --crustdata`. Only the Crustdata key needs a hidden prompt; Gemini uses the external JSON. No key is sent to the frontend.

Two real Vertex Gemini checks passed: request parsing and evidence extraction using synthetic supplier evidence. The initial JSON-schema request was rejected with HTTP 400; translating to Vertex's supported `response_schema` format resolved it. Full Pydantic validation still runs locally. SDK generation retries are disabled (`attempts=1`), as is automatic function calling. No non-Gemini model was called. `python scripts/verify_vertex.py` explicitly runs these two billed checks again; it is not part of automated pytest.

The dashboard now defaults to live data and Gemini enabled. A known supplier domain uses the minimal 0.2-credit identity flow. Leave it blank to request broader supplier search and explicitly set an adequate provider-credit budget in the form. The default never rises automatically. Gemini token usage remains separate. Live purchasing claims remain unverified, so missing evidence still blocks orders.

Combined live validation also passed: a real request through the running API resolved Mouser with Crustdata for 0.2 credits and completed both Vertex Gemini extraction stages. It produced cited supplier insights and explicit verification gaps, with no order. The raw result is local and ignored. The credential was not used for any other model family or Cloud workload.

The following original API-key setup remains available only for a separate Google Gemini Developer API account; it is not used for the supplied service-account credential.

The application has two opt-in model stages. Set `llm_enabled: true` on a request, or select the Gemini checkbox in the dashboard. Fixture mode with the checkbox off still needs no keys and makes no model calls.

## Stage 1: user request -> structured intent

During PARSING, Gemini receives the user's text, explicit structured specs, and category/destination/deadline context. It returns a validated `ParsedIntent`: items, part numbers, parameter pairs, quantities with verbatim excerpts, substitutions, textual delivery/budget needs and a focused clarification question.

Code assigns stable item IDs, preserves explicit specs and executable form limits, rejects unsupported numeric quantities and bare build counts, and pauses on missing quantities/specs or conflicting category/destination/type. Form budgets are never rewritten by the model. The interpretation is saved under `WorkflowRun.request_interpretation` and displayed in the dashboard.

## Stage 2: Crustdata evidence -> useful supplier insights

During VERIFYING, one batched model call receives the saved suppliers, items and evidence records. It returns a summary, missing information and structured claims for each supplier. Each claim must identify the source evidence and include a verbatim supporting excerpt.

Code rejects invented IDs, out-of-scope citations, absent excerpts, conflicting claims, unsupported numeric values and malformed types. Purchasing facts require an item ID and a fetched supplier page; Company Identify responses or search snippets cannot establish stock, price, delivery or authorization. Search-engine redirect filtering and canonical company resolution remain outside the model.

Accepted claims populate live quote fields and create derived evidence with `provider=gemini_extraction` and `status=unverified`. **LLM extraction is not independent verification**: it never changes a live quote to verified, rewrites company IDs, ranks suppliers, spends money or approves an order. Summaries are labelled model-derived. A future independent quote-verification adapter is needed before automatic live purchasing.

With the default 0.2 Crustdata-credit profile, only company identity evidence is available. Gemini can summarize that evidence and explain missing data; it cannot manufacture prices or stock. The integration does not raise the Crustdata budget or add provider calls.

## Setup

The existing Python/httpx backend calls Gemini's REST `generateContent` endpoint with JSON-schema structured output; no additional JS service or SDK is required. Model selection is configurable using `GEMINI_MODEL`, defaulting to `gemini-3.1-flash-lite`.

Stop the old worker before starting its replacement. From `backend`:

```powershell
python ../scripts/start_worker.py --gemini --crustdata
```

Enter the Gemini and Crustdata keys at the hidden prompts. They remain in that process's environment and are not written to disk. Alternatively, set `GEMINI_API_KEY`, optional `GEMINI_MODEL`, and `CRUSTDATA_API_KEY` in your worker environment and run `python -m app.workflow.worker`. API and worker must use the same working directory or absolute `SOURCING_DB`.

The API process and frontend never receive these keys. A Crustdata key is not a Gemini key. Missing Gemini configuration produces a saved `GEMINI_NOT_CONFIGURED` failure for an enabled run; the system does not silently substitute a fixture model response.

Then open the dashboard, enable **Use Gemini to parse the request and extract supplier insights**, and submit a new request. Parsed requirements, supplier summaries, extracted claims, rejected-claim warnings and model token usage appear in the Gemini intelligence panel.

## Cost and recovery controls

- Normally two model generations per request, one at each stage; clarification answers require another parse.
- Hard maximum of four generation attempts per run, including failures and clarification rounds.
- At most 60,000 input characters and 16,384 output tokens per structured call.
- Persistent response cache in `<workflow-db>.llm.db`, keyed by run, model, stage, input, prompt and schema. Replaying identical work reuses its validated response.
- Intent is committed before contacting Gemini. An ambiguous network outcome remains pending and is not automatically retried. Refused, truncated, malformed or schema-invalid output is not applied.
- Prompt/provider error bodies and authentication headers are not logged. Usage records contain stage, model, state and token counts.
- Gemini tokens are a separate provider cost. The 0.2 Crustdata-credit cap does **not** cap Gemini billing, and no guessed monetary price is recorded.

The model has no tools, browsing, code execution or payment functions. Retrieved pages are explicitly treated as untrusted data. Citation validation improves traceability but does not prove a model's interpretation is correct; that is why purchasing claims remain unverified.

## Validation

Run `python -m pytest -q` from `backend`. The model tests use mocked Gemini HTTP responses, including a complete API/worker workflow with both stages, citation failures, request clarification, blocked/truncated output, persistent caching, call limits and ambiguous network failure. They consume no Gemini or Crustdata credits.

Real Vertex Gemini checks now pass as described above. The earlier API-key configuration was verified only with mocks. The previous browser interaction/visual verification limitation remains.

Official API references consulted:
- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/gemini-api/docs/models
