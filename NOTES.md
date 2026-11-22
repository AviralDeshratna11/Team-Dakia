# Team notes

One shared file for what we tried, what worked and what failed. Add to it as you go; it feeds the README and the judges' questions.

---

## 2026-11-22 · Step 2 (find suppliers): can Crustdata do it? (Sid)

**Question:** can we find sellers and read their price, stock, MOQ and lead time using only Crustdata Web Search and Web Fetch, without an agentic browsing loop?

**Test part:** AP2112K-3.3TRG1 (3.3V LDO, 600mA, SOT-23-5). Cost: 8 credits.

### Web Search (`POST /web/search/live`, 1 credit per page)

- `site:lcsc.com` in the query is **ignored**: results came back from DigiKey, AliExpress and Mouser.
- The `site` request parameter is **also ignored**: results included eBay and eecart.
- Most result URLs are **Google redirect links** (`google.com/goto?url=...`). Fetch follows them, but one landed on **digikey.jp, priced in JPY**.
- Some snippets carry price and stock already, e.g. Mouser: "$0.27 · 226,110 In Stock".

### Web Fetch (`POST /web/enrich/live`, 1 credit per URL, up to 10 URLs per call, about 7s for 5)

| Site | Result |
|---|---|
| DigiKey India (product page) | **Works well.** JSON-LD has INR price, `InStock` and the seller. `__NEXT_DATA__` has full tier pricing (1 → ₹27.58, 10 → ₹19.02, 25 → ₹16.78 …) and stock "4,01,929". |
| LCSC (search page) | **Works.** Tier pricing and stock are in minified Nuxt state, which is hard to parse with code. The visible text is clean ("77,465 In Stock"). Results also list equivalent parts from other makers (e.g. MSKSEMI), useful for substitutes. |
| Mouser | **Blocked** ("Access to this page has been denied"), also with `human_mode: true`. |
| eecart | **Blocked** by a Cloudflare challenge ("Just a moment..."). |

**Gotcha:** Fetch returns `success: true` even for blocked pages. Detect blocks ourselves, by page title or content.

### What this means for the design

1. **Don't use search to reach known distributors.** The category pack holds a URL pattern per distributor, and we fetch those directly.
2. **Use search only to discover unknown sellers**, with one open query per part. These are the sellers that need trust checks.
3. **Extract cheapest first:** JSON-LD → the site's embedded JSON → the LLM on stripped visible text.
4. **A blocked page is a data gap** (`BLOCKED`). It goes to step 4 (buy data, or mark unverified), not to another search.
5. **Normalise every quote:** store the currency, parse Indian number formatting, convert to INR with a rate from config, and don't trust redirects to land on the India site.

**Open:** we haven't tested the DigiKey India *keyword search* URL; only a product page with a known ID was fetched.

---

## Design decision · when does step 2 retry a search?

Plain code classifies each part after extraction; the LLM doesn't judge "good enough".

| Outcome | Meaning | Next step |
|---|---|---|
| `NOT_FOUND` | No results, or no product pages | Rewrite the query **once** |
| `NO_MATCH` | Product pages found, none match the spec | Rewrite the query **once**, trying substitutes from the pack |
| `GAP` / `BLOCKED` | Matching part, but price or stock missing, or page blocked | Step 4: buy the data or mark it unverified. **Never search again.** |
| `FOUND_UNFIT` | Matching part with a price, but out of stock or too slow | Step 5 rejects it with a reason |
| `FOUND` | At least one matching quote with a price | Continue |

After one rewrite, a part still at `NOT_FOUND` or `NO_MATCH` becomes "no supplier found" and the report suggests substitutes.

---

## 2026-11-22 · v0 pipeline: first full runs (Sid)

v0 runs the whole BOM end to end: steps 1–2 are real, 3, 4 and 6 are stubs, and 5 is a placeholder rule. Every step's input and output is in `runs/<run_id>/`; `notebooks/walkthrough.ipynb` steps through them.

**What we learned**

- **DigiKey India's keyword URL** (`/en/products/result?keywords=<mpn>`) goes straight to the product page on an exact match. Without one (ESP32-S3-WROOM-1-N8R8, CL21A106KAQNNNE, TYPE-C-31-M-12) it returns a search list, which we don't parse yet, so it shows as `NO_MATCH`.
- **DigiKey blocks intermittently** with Cloudflare ("Just a moment..."). The same LDO URL worked, then was blocked 30 minutes later. `human_mode: true` gets through at the same 1 credit, so DigiKey always uses it.
- **Same part number, different maker.** LCSC lists AP2112K-3.3TRG1 from Diodes Inc (₹8.32) *and* from TECH PUBLIC (₹6.27). v0's first run picked the TECH PUBLIC part as cheapest. We now match the maker as well as the part number, and a mismatch is rejected as "possible clone". This is a good demo case.
- **Near-miss part numbers are substitute candidates:** CL21A106KA**Y**NNNE for KA**Q**NNNE (a different voltage rating), TYPE-C-31-M-12**A**, AP2112K-3.3TRG1**(MS)**. They're kept as `alternatives`. Spec-based matching on parameters is next.
- **Google redirect links can't be decoded** (the `url=` value is encrypted), so leads from open search have no domain yet. Step 3 needs the real domain, probably from the fetched page's canonical URL.
- **Speed and cost:** a cold full BOM takes 1.5–3 minutes and ~20 credits; a cached re-run takes ~0.1s and 0 credits. Blocked pages are cached for 1 hour only.

---

## 2026-11-22 · Step 1 on Gemini, clones flagged instead of rejected (Sid)

- **Step 1 now reads the founder's own words** with `gemini-2.5-flash` on Vertex AI (temperature 0, cached). It returns request details, specs, up to 3 candidate part numbers per line, questions and assumptions. Hand-written `lines` in the request still skip the LLM (`examples/electronics_bom_manual.yaml`).
- **Gemini's structured output can't fill free-form dicts.** `params: dict[str, str]` came back empty every time, so we use a list of `{name, value}` pairs.
- **It gave a family name, not a part number:** `ESP32-S3-WROOM-1` instead of `-N8R8`. LCSC only lists the full variants (`-N8`, `-N4`, `-1U-N4`), so it was `NO_MATCH`. The prompt now demands the full orderable number with every suffix.
- **It's question-happy.** On the brief's request it asks about 3 of 4 parts (ESP32 memory, cap voltage rating, USB-C type); only the LDO goes through. The vague request ("a small regulator") correctly gets one question instead of a guess. **Open: when to assume a standard default vs. ask.**
- **Clones are flagged, not rejected.** The same part number from another maker gets a `possible clone` flag. Code never picks a flagged quote; if one is cheaper than the pick, or is the only option, the decision lists it under `founder_review` with the price difference. Example: TECH PUBLIC AP2112K-3.3TRG1 is ₹1,025 cheaper than the Diodes part for 500 units.
- **The retry now also covers blocked-only results.** Mouser is always blocked, so "no site has the part" showed as `BLOCKED` and the alternate part number was never tried.

---

## 2026-11-22 · Backbone complete: checks, landed cost, orders paid once (Sid)

- **Step 1 is a gate.** If anything is unclear, nothing reaches step 2. All questions go to the founder at once, and round 2 re-reads the request with the answers. Round 2 may not ask again: anything still open gets the most common choice, listed in assumptions.
- **Storefront domains don't always resolve to the seller.** `digikey.in` → "DigiKey Global Capability Center" (an Indian back office, founded 2021); `digikey.com` → DigiKey (founded 1972, ~2,700 staff). `mouser.in` → no record at all; `mouser.com` → Mouser (1964). The pack maps each storefront to its `company_domain`. Checks are per company, so two storefronts of one company become one record (hard case 3).
- **Step 3 scores sellers in code:** real company (Identify), age, size and growth (Enrich headcount), web traffic, and authorized distributor (pack). Results: LCSC 0.78 (not authorized), DigiKey 1.00. A made-up storefront domain gets no match and is rejected (hard case 2).
- **Crustdata data isn't always consistent:** LCSC shows headcount 27 but an employee range of 201–500.
- **Step 5 follows the brief's rule table**, with landed cost = goods + shipping + duty + GST. All rates are PLACEHOLDERs in the pack, and every decision says so. With placeholder rates LCSC still beats DigiKey India on the LDO (₹8,258 vs ~₹9,300 landed).
- **Step 6 never pays twice.** Order states are saved in `desk.db`. The idempotency key covers only what's bought, so re-runs find the same order. The mock checkout (`checkout_mock.db`) replays a repeated key instead of charging. Tested: crash after the vendor charged → re-run recovered the same payment ID; held order (₹2.05 lakh > ₹50k limit) → `approve` → next run paid it; a third run charged nothing. Three orders, three charges.
- **Not yet:** step 4 (buy data) is a stub. Leads from open search aren't checked (no domain behind Google redirects). Shipping is charged per line, not consolidated per vendor.

---

## 2026-11-22 · Agents inside the workflow (Sid)

**Architecture:** the pipeline is still a fixed workflow, so it's repeatable and auditable. Bounded tool-calling agents sit inside it only where the path isn't known in advance. All of them run on one loop, `desk/agent.py`:
- Gemini function calling, temperature 0.
- `max_turns`, with a forced `finish` on the last turn.
- A Crustdata credit budget: paid tools refuse once it's spent.
- A typed report as the only way to finish.
- Every turn is cached by the conversation so far (re-runs replay for free), and the transcript is saved in `runs/`.

Agents gather facts and draft; code decides.

| Agent | Where | Tools | Output → what code does with it |
|---|---|---|---|
| seller investigator | step 2b, open-search leads | fetch_page, read_product_page (one structured Gemini call), identify_company, enrich_company, web_search | report → quote whose prices come from the page read; red flags become quote flags (founder review) |
| RFQ agent | step 3b, sellers with no price | send_email (mock mailbox, one follow-up max) | quote → code checks every number appears in the vendor's email; advance-payment terms get flagged |
| pack drafter | step 1b, a field with no pack | web_search, fetch_page, company_search | pack YAML → `packs/drafts/`, used only after the founder approves; low confidence |

**What we saw**

- **Investigator:** eBay listings are blocked; the agent searched twice, then reported a marketplace with red flags. The Amazon India lead was a different variant (`-1U-`, external antenna) at ₹7,237 vs ₹312: flagged mismatch. Robu.in had `TYPE-C-31-M-35`, not `-M-12`, so it's a substitute candidate. Leads cost 0–6 credits and 2–8 turns each.
- **`amazon.in` resolves to an unrelated company** in Tamil Nadu in Crustdata: the same storefront-vs-company problem as `digikey.in`.
- **Pack drafter**, on the leather request: 3 turns, 1.2 credits. It tested its own Company Search query, which found 5 makers in Kanpur. It drafted RFQ questions (leather grade, debossing, samples, packaging), ordering by RFQ, and trust signals.
- **Company Search wants ISO-3 country codes** (`IND`). The drafter wrote "India", so code normalises it. Asking for `headcount` in search costs 0.2 credits extra per result, so we enrich only the shortlisted makers.
- **Checks have to come before RFQs.** The first try sent RFQs to the first three makers found, including one founded in 2024 (score 0.43). Now it's best score first, and never a REJECTED seller.
- **RFQ agent:** it followed up only for what was missing and recorded the quote with the vendor's exact words. NDA/advance → escalated. But on "50% advance with the PO" it followed up instead of escalating, so advance terms are now flagged by code, not left to the prompt.
- **Leather end to end:** drafted pack → approved → 5 makers found and checked → RFQs to the top 3 → a teammate replied as Arctic International (₹1,150/pc, pay on delivery) → picked, landed ₹4.09 lakh → order held above the ₹50k approval limit.

---

## Later (not now)

- When data is missing or blocked, pay third-party data services for it.
- Payments infrastructure.
