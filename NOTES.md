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

## Later (not now)

- When data is missing or blocked, pay third-party data services for it.
- Payments infrastructure.
