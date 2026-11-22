# Two-minute demo

Before recording: run API, worker and frontend. Keep the worker terminal available. Set an order threshold high enough for the chosen demo if approval is not part of the recording. Use a fresh run, not edited results. Explain that all companies and prices are synthetic.

| Time | Action / narration |
|---|---|
| 0:00–0:15 | “Sourcing Desk does the founder's sourcing work: specifications, supplier verification, a reasoned plan and simulated orders.” Submit the reference electronics request with explicit `500 of each`, scenario Missing stock. |
| 0:15–0:40 | Show persisted stages and supplier comparison. The cheap storefront lacks resolved identity; Bright Circuit aliases are one company. |
| 0:40–1:00 | Show Research: a ₹40 mock stock purchase, its rationale, budget and resulting evidence. Mention the budget can be reduced before commitment. |
| 1:00–1:20 | Show the final selected plan, landed-cost arithmetic and source timestamps. Show mock PAID receipts or approve the exact order amount if using a low threshold. |
| 1:20–1:40 | Show the recovery case below using a second short one-item run. |
| 1:40–2:00 | Show a leather shortlist with three unsent drafts from the same engine. Finish with the boundaries: real adapters, real money and external vendor contact are not implemented. |

## Recovery case

Create a one-item request: `Source ESP32-S3 modules, 500 of each`. Choose **Checkout crash**, and set approval threshold to ₹1,000,000. The worker exits with code 42 after the independent mock provider commits. The UI remains EXECUTING, with a saved PAY_PENDING intent. Restart the worker using the same database and working directory. It looks up the original idempotency key, records the existing receipt and completes without a second payment.

For a quicker failure demo without restarting a process, choose **Checkout timeout**. The workflow records a timeout and reconciles the original payment on its next tick.

## Budget reduction case

Choose Missing stock. Open Research while the run is RESEARCHING. Once a reservation appears, set the ceiling to zero before payment. The reservation is released, stock remains unknown, and the next eligible supplier is selected. If the purchase already committed, the ledger remains intact and the lower ceiling blocks new spending. The tests cover the exact interleaving even when clicking manually is too slow.

## Other cases

- `a small regulator`: waits for a focused clarification. Reply with `3.3V, 500mA, SOT-23, 500 of each`.
- `ESP32-S3 and USB-C for 500 builds`: asks for per-line quantities.
- Provider timeout / 429: three persisted attempts, then a plan with explicit verification gaps and no orders.
- Leather goods example: same pipeline, manufacturing eligibility, trader rejected, three draft RFQs. No emails are sent.

## Honest claims

Fixture verification means facts are internally consistent within a synthetic scenario. It does not verify real suppliers. Payment idempotency is demonstrated against a persistent simulator. Tax, duty, price and FX configuration are demo inputs. The parser is bounded and deterministic; no LLM extraction is implemented. Live mode supports capped Crustdata identity/search/fetch calls, while purchasing facts remain unverified. The default 0.2-credit initial test uses one explicit supplier domain and Identify only. The initial full live test proved connectivity but resolved a Google redirect instead of a supplier; see [live test](live-test.md) for the correction. Do not present it as verified sourcing.
