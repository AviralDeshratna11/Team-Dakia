# Team integration plan

Contracts first: `contracts/models.py` is the source of truth. Choose one lead to coordinate changes, export OpenAPI and regenerate frontend types. Modules return fixture-backed contracts so teammates can integrate independently.

| Work area | Ownership | Integration contract | Next task |
|---|---|---|---|
| Data | providers, resilience, verification evidence | Supplier, Quote, Evidence, SupplierCheck | Verify official Crustdata contracts; canonical identity/cache; explicit credit gates |
| Agent / lead | parser, packs, workflow, domain rules | Spec, Decision, WorkflowRun | Structured LLM parser; extraction with citations; preserve deterministic decisions |
| Money | reservations, payment provider, order lifecycle | ResearchPurchase, LedgerEntry, Approval, Order | Provider reconciliation, credit accounting, additional failure cases |
| Product | dashboard, tests, docs, demo | Generated OpenAPI TypeScript types | Split UI into focused panels; improve partial results; demo rehearsal |

This ownership table is a proposed human team split. The scaffold itself was implemented without delegated coding agents.

## Suggested sequence

1. Run the fixture vertical slice and all tests before editing integration boundaries.
2. Connect free identity lookup with verified documentation and credentials.
3. Add cited live discovery/verification with explicit cost authorization for every paid call.
4. Replace the bounded fixture parser with schema-constrained extraction. Ask about missing quantities/specs.
5. Re-run failure and crash tests; keep fixture mode as an offline demo fallback.
6. Freeze features, polish the README and record a two-minute request-to-result demo.

Avoid splitting the pipeline per category. Add rules to a validated category pack. Unknown category-pack generation, real money, actual vendor checkout, negotiation and shipping tracking remain deferred.

## Submission checklist from the supplied handbook

- Build-period work and commit history accurately represented; declare prewritten code, prompts or designs as required.
- Submit at the checkpoint and before the handbook's 6:00 PM IST code freeze; latest submission counts.
- Public repository and README with service, setup, architecture, tested cases and honest limitations.
- One playable two-minute video link, checked in a private window.
- Architecture diagram link accessible to judges; Mermaid source is provided, publishing is a separate manual step.
- Team details match registration and credit-account information.
- Show at least one difficult case, cited reasons, duplicate identity handling and recovery without duplicate effects.
- Keep agents' interactions within the event team; this skeleton stores drafts and uses simulated payments.

These are reference requirements from the supplied event handbook, not actions performed by this repository. No submission, publishing, GitHub creation or external communication has been performed.
