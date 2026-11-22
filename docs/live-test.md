# Live test and minimal credit policy

The initial live run exercised actual HTTP API -> workflow -> Crustdata Search, Identify and Fetch -> persisted decisions. All three provider calls returned HTTP 200; response usage headers totalled **2.2 credits**, matching the account balance change. No payments, orders or external messages occurred.

Inspection revealed that Search returned an opaque Google redirect. The adapter incorrectly resolved the redirect host (Google) instead of a supplier. Consequently this run demonstrates **API connectivity only**, not successful supplier discovery or verification. The saved raw result is `live-e2e-result.json`; its diagnostic fields preserve this finding. The workflow correctly blocked orders because all purchasing facts were missing.

The adapter now unwraps ordinary search redirects and rejects opaque search-engine URLs rather than treating them as suppliers. Offline regression tests cover both cases. No further paid calls were made after the user requested the minimal cap.

## New defaults

- Hard limit: **0.2 provider credits per run**.
- Initial test supplies one known supplier domain, `mouser.com`, and calls Company Identify once.
- No paid Web Search, Web Fetch, Enrich or Job Search in this minimal profile.
- Account balance and endpoint-price checks are free. The provider's account-specific price is checked before reserving credits. A higher effective price is blocked before calling Identify.
- Request/response cache and credit reservations persist in a separate SQLite journal. Repeating an identical call for the same run uses the cached response; ambiguous outcomes are not automatically retried.
- INR mock research spending remains separate and is zero in the live test.

The default cap is enforced in `CrustdataClient`. `supplier_domains` on the live request enables identity-only discovery. A normal search costs more than the default cap and is deliberately blocked. Do not raise the cap or remove the journal to force retries without considering previous usage.

To rerun the minimal live HTTP test, run `python scripts/live_e2e.py`. It prompts for the API key without displaying or saving it, starts a temporary loopback API, runs the pipeline, saves `docs/live-e2e-identity-result.json`, and stops the API. This is an operator-triggered paid test and is not part of pytest.

The **0.2-credit profile has been tested offline but has not been rerun against the live API** after the budget change. The first run's actual spend remains 2.2 credits. Real price, stock, authorization, destination and delivery verification, plus LLM extraction, are still incomplete; the intended final state is COMPLETED_WITH_GAPS with no order.

Official contracts consulted:
- https://docs.crustdata.com/general/permissions
- https://docs.crustdata.com/company-docs/identify/introduction
- https://docs.crustdata.com/web-docs/search/introduction
- https://docs.crustdata.com/web-docs/fetch/introduction
