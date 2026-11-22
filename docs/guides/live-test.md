# Live test and minimal credit policy

## Latest combined live check

Run `9165fd19-605d-4923-80dd-5c8b37a77690` successfully exercised the running dashboard proxy -> FastAPI -> worker -> Vertex Gemini request parser -> Crustdata Company Identify -> Vertex Gemini evidence extractor -> persisted result. The supplier was **Mouser (`mouser.com`)**, resolved to its canonical company record. Both model calls used **Google `gemini-2.5-flash`**; Crustdata charged **0.2 credits**. The result was **COMPLETED_WITH_GAPS**, with no order, because purchasing facts remain unverified. No partner models or other Cloud workloads were called.

The real Vertex credentials stay outside the repository; ignored local `.env` supplies only their file path and project/location/model configuration. See [LLM layer](../design/llm-layer.md) for setup. Local raw results are ignored, not published.

The dashboard now lets the user edit the supplier domain and explicitly choose a per-run Crustdata budget (0 to 10 credits). The default is still **0.2**. Known-domain mode checks identity only; clearing the domain enables search, which requires enough explicitly authorized credits. If identification or page retrieval exceeds the remaining budget, obtained candidates are retained with those checks marked unverified. The application does not raise a limit automatically.

The remaining sections preserve the earlier connectivity test and its redirect defect for an honest history; they are superseded by this successful known-supplier check.

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

The **0.2-credit profile now passes against the live API**, as recorded above. The original connectivity run used 2.2 credits and the new combined check used another 0.2. LLM extraction is implemented and verified with Vertex Gemini; real price, stock, authorization, destination and delivery verification remain incomplete, so the final state is COMPLETED_WITH_GAPS with no order.

Official contracts consulted:
- https://docs.crustdata.com/general/permissions
- https://docs.crustdata.com/company-docs/identify/introduction
- https://docs.crustdata.com/web-docs/search/introduction
- https://docs.crustdata.com/web-docs/fetch/introduction
