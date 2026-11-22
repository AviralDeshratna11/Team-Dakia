# Sourcing Desk backend benchmark

The runnable benchmark uses the FastAPI application, worker, temporary SQLite databases, and mock payments on `main`. [Benchmark source](../backend/tests/test_benchmark.py)

## Run it

From `backend`, using the project's Python environment:

```powershell
python -m pip install -e '.[test]'
python -m pytest -q -rx tests/test_benchmark.py
python -m pytest -q -rx
```

Run known findings as hard failures when assessing whether the current revision meets the tested purchase rules:

```powershell
python -m pytest -q --runxfail tests/test_benchmark.py
```

The fixtures remove provider credentials, block real HTTPX transports, and use temporary databases. The separate `sid` CLI has its own report and archived cases. [CLI benchmark](sid-benchmark.md)

## Coverage

| Area | Assertions |
|---|---|
| API inventory | Successful calls to all 21 application operations and the documented FastAPI utility routes. The exercised operations must match the app routes and checked-in OpenAPI operation inventory. |
| Run and review state | Structured validation and missing-run errors, unchanged state after rejected requests, clarification, human-review queuing and resolution, and scope and evidence for submitted supplier quotes. |
| Cost, selection, and evidence | Fixed supplier and price outcomes for electronics and leather goods, per-tier price and rounding boundaries, evidence citations, category-specific requirements, and deterministic ranking. |
| Wallet and checkout | Exact-amount approvals, idempotent mock wallet funding, preview-only payout rendering, concurrent approval replays, and receipt and ledger reconciliation after a simulated failure. |
| Research and RFQs | Zero, insufficient, and exact budgets, preservation of committed spend, and RFQ drafts without orders or payment effects. |
| Additional category policies | Synthetic industrial parts, medical supplies, food packaging, and textiles packs applied directly to the decision engine. |

The full suite also exercises provider budgets and retry handling, compliance screening, and mocked Gemini Developer and Vertex responses. [OpenAPI specification](https://spec.openapis.org/oas/v3.1.1.html) · [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/) · [SQLite isolation](https://www.sqlite.org/isolation.html)

## Known findings and limits

Three cases are marked as strict expected failures. They cover missing quote citations, quote observations older than the configured freshness limit, and fixture parsing that substitutes connectors for USB-C cables. The negative-price case now runs as a regular regression test.

Every route is exercised with at least one successful request. The finite case set measures the recorded contracts and specific failure paths. Live supplier accuracy, tax and duty accuracy, real model quality, production performance, and actual settlement remain unmeasured. [NIST generative AI risk profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)
