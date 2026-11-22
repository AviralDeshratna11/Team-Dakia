"""Step 2b: investigate unknown sellers from the open web search, with a bounded agent.

Catalogue distributors have fixed recipes; leads don't (a Google redirect, a marketplace listing, a
datasheet...). For those, the agent decides what to look at next. It only gathers evidence:
- prices and stock come from read_product_page's structured output, not from the agent's words,
- the seller is then scored by the same step 3 code as every other seller,
- and step 5's rules decide. Red flags the agent finds go to the founder, never into a silent pick.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel

from desk import extract, llm
from desk.agent import AgentResult, Tool, run_agent
from desk.crustdata import Crustdata
from desk.schemas import FindResult, Lead, PriceTier, Quote, Spec, Status


# ---------- what the agent reports ----------

class InvestigationReport(BaseModel):
    is_seller: bool                  # False for datasheets, wikis, news, forums
    seller_name: str | None
    seller_domain: str | None        # the real domain (from the fetched page), never google.com
    product_url: str | None          # the page read with read_product_page, if any
    sells_exact_part: bool | None
    seller_type: str                 # manufacturer | authorized_distributor | independent_distributor | marketplace | unknown
    ships_to_india: bool | None      # None = no evidence either way
    red_flags: list[str]
    evidence: list[str]              # "fact (source)"
    summary: str


class InvestigateResult(BaseModel):
    line_id: str
    agents: list[AgentResult]
    quotes: list[Quote]              # one per lead that turned out to be a seller with a product page
    skipped: list[str]               # leads not investigated, and why


# ---------- what read_product_page extracts (one Gemini call) ----------

class _Tier(BaseModel):
    min_qty: int
    unit_price: float


class PageQuote(BaseModel):
    is_product_page: bool
    mpn: str | None
    manufacturer: str | None
    seller_name: str | None
    currency: str | None             # ISO code, e.g. USD, INR
    price_tiers: list[_Tier]
    stock: int | None
    moq: int | None
    ships_to_india: bool | None
    shipping_note: str | None


SYSTEM = """You are a sourcing investigator for a company in India buying electronic parts.
You get one lead from a web search. Find out, with evidence, whether it is a real seller of the exact
part that can deliver to India, and who is behind it. Use the tools; never guess.

- Every fact in your report must come from a tool result; each evidence line names its source (a URL
  or "Crustdata").
- Usually: fetch_page the lead (it follows redirects and tells you the real domain); if it's a product
  page, read_product_page; identify_company with the seller's real domain; enrich_company if found.
  Search only if something important is still unknown (e.g. "<seller> shipping India").
- If the lead isn't a seller (datasheet, wiki, news, forum), stop and report is_seller=false.
- Red flags: anonymous marketplace store, price far below the reference, "100% original" claims,
  no company record, a different manufacturer than requested, a page that doesn't match the part.
- Be brief: you have few turns and a small credit budget."""

TASK = """Part: {mpn} by {maker}, quantity {qty}.
Reference: the cheapest catalogue quote so far is {ref}.

Lead:
  title: {title}
  url: {url}
  snippet: {snippet}"""


def investigate_leads(spec: Spec, find: FindResult, pack: dict, cd: Crustdata) -> InvestigateResult:
    cfg = pack.get("investigate") or {}
    n = cfg.get("leads_per_part", 0)
    skip_words = [w.lower() for w in cfg.get("skip_if_title_has", [])]
    known = {d["domain"] for d in pack["distributors"]} | {d.get("company_domain") for d in pack["distributors"]}
    ref = min((q for q in find.quotes if q.unit_price_inr and q.status == Status.FOUND and not q.flags),
              key=lambda q: q.unit_price_inr, default=None)
    ref_text = f"₹{ref.unit_price_inr}/unit at {ref.vendor}" if ref else "none found"

    picked, skipped, seen = [], [], set()
    for lead in find.leads:
        if any(w in lead.title.lower() for w in skip_words):
            skipped.append(f"{lead.title[:60]}: title suggests not a seller")
        elif lead.domain and lead.domain in seen:
            skipped.append(f"{lead.title[:60]}: same domain as an earlier lead")
        elif len(picked) >= n:
            skipped.append(f"{lead.title[:60]}: over the {n}-lead limit")
        else:
            picked.append(lead)
            if lead.domain:
                seen.add(lead.domain)

    agents, quotes = [], []
    for lead in picked:
        pages: dict[str, PageQuote] = {}
        result = run_agent(
            "seller_investigator", SYSTEM,
            TASK.format(mpn=spec.mpn, maker=spec.maker or "any", qty=spec.qty, ref=ref_text,
                        title=lead.title, url=lead.url, snippet=lead.snippet),
            _tools(cd, spec, pages), InvestigationReport,
            max_turns=cfg.get("max_turns", 8), credit_budget=cfg.get("credit_budget", 8))
        agents.append(result)
        q = _to_quote(spec, lead, result, pages, pack, known)
        if q:
            quotes.append(q)
    return InvestigateResult(line_id=spec.line_id, agents=agents, quotes=quotes, skipped=skipped)


def _to_quote(spec: Spec, lead: Lead, result: AgentResult, pages: dict, pack: dict, known: set) -> Quote | None:
    """Code, not the agent, turns the evidence into a quote."""
    from desk.steps import assess_quote  # late import: steps imports this module

    rep = InvestigationReport.model_validate(result.report) if result.report else None
    if rep is None or not rep.is_seller or not rep.seller_domain:
        return None
    domain = rep.seller_domain.lower().removeprefix("www.")
    if domain in known or extract.is_google_redirect(domain):
        return None                  # a catalogue distributor (already quoted) or unresolved
    page = pages.get(rep.product_url or "") or next(iter(pages.values()), None)
    q = Quote(line_id=spec.line_id, vendor=rep.seller_name or domain, vendor_domain=domain,
              url=rep.product_url or lead.url, fetched_at=datetime.now(timezone.utc), status=Status.FOUND,
              extracted_by="agent:read_product_page", found_by="agent", seller_type=rep.seller_type,
              ships_to_india=rep.ships_to_india, evidence=rep.evidence,
              flags=[f"agent: {f}" for f in rep.red_flags])
    if rep.sells_exact_part is False and (page is None or not page.is_product_page):
        q.status, q.note = Status.NO_MATCH, "seller doesn't have the exact part"
        return q
    if page is None or not page.is_product_page:
        q.status, q.note = Status.GAP, "seller found, but no product page was read"
        return q
    q.mpn, q.manufacturer, q.stock, q.moq = page.mpn, page.manufacturer, page.stock, page.moq
    q.tiers = [PriceTier(min_qty=t.min_qty, unit_price=t.unit_price, currency=(page.currency or "USD").upper())
               for t in page.price_tiers]
    return assess_quote(q, spec, pack)


# ---------- tools ----------

def _tools(cd: Crustdata, spec: Spec, pages: dict) -> list[Tool]:
    def fetch_page(url: str) -> dict:
        page = cd.fetch([url])[0]
        html = page.get("content") or ""
        if extract.is_blocked(page.get("title"), html):
            return {"blocked": True, "title": page.get("title")}
        return {"title": page.get("title"), "real_url": _real_url(html) or page.get("url"),
                "real_domain": extract.domain(_real_url(html) or page.get("url") or url),
                "part_mentioned": spec.mpn.lower() in html.lower(), "text": _text(html, spec.mpn, 2500)}

    def read_product_page(url: str) -> dict:
        page = cd.fetch([url])[0]
        html = page.get("content") or ""
        if extract.is_blocked(page.get("title"), html):
            return {"blocked": True}
        pq = llm.ask("Extract the product offer on this page. Only facts on the page; null if absent. "
                     f"We want part {spec.mpn}.\n\nTITLE: {page.get('title')}\nURL: {url}\n\n"
                     + _text(html, spec.mpn, 9000), PageQuote)
        pages[url] = pq
        return pq.model_dump()

    def identify_company(domain: str) -> list[dict]:
        out = []
        for m in cd.identify(domain.lower().removeprefix("www."))[:3]:
            b = m["company_data"].get("basic_info") or {}
            out.append({"company_id": m["company_data"].get("crustdata_company_id"), "name": b.get("name"),
                        "domain": b.get("primary_domain"), "founded": b.get("year_founded"),
                        "size": b.get("employee_count_range"), "type": b.get("company_type"),
                        "confidence": m.get("confidence_score")})
        return out or [{"result": "no company record for this domain"}]

    def enrich_company(company_id: int) -> dict:
        e = cd.enrich(int(company_id), ["basic_info", "headcount", "locations", "web_traffic"]) or {}
        head = e.get("headcount") or {}
        traffic = (e.get("web_traffic") or {}).get("domain_traffic") or {}
        return {"name": (e.get("basic_info") or {}).get("name"), "headcount": head.get("total"),
                "headcount_yoy_pct": (head.get("growth_percent") or {}).get("yoy"),
                "hq": (e.get("locations") or {}).get("headquarters"),
                "monthly_visitors": {d: v.get("monthly_visitors") for d, v in traffic.items() if isinstance(v, dict)}}

    def web_search(query: str) -> list[dict]:
        return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("snippet")}
                for r in cd.search(query)[:6]]

    s = lambda **props: {"type": "object", "properties": props, "required": list(props)}  # noqa: E731
    return [
        Tool("fetch_page", "Fetch a URL (follows redirects). Returns title, the real URL/domain, whether the part "
             "number appears, and page text around it. 1 credit.", s(url={"type": "string"}), fetch_page, paid=True),
        Tool("read_product_page", "Read a product page into structured fields: part number, manufacturer, seller, "
             "price tiers, stock, MOQ, shipping to India. Use on the page that sells the part.",
             s(url={"type": "string"}), read_product_page, paid=True),
        Tool("identify_company", "Crustdata: which company owns this domain (name, id, founded, size). "
             "No record is a red flag. 0.2 credits.", s(domain={"type": "string"}), identify_company, paid=True),
        Tool("enrich_company", "Crustdata: headcount, growth, HQ and web traffic for a company id. 2 credits.",
             s(company_id={"type": "integer"}), enrich_company, paid=True),
        Tool("web_search", "Web search; top 6 results (title, url, snippet). 1 credit.",
             s(query={"type": "string"}), web_search, paid=True),
    ]


def _real_url(html: str) -> str | None:
    for pat in (r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)',
                r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical',
                r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, html, re.I)
        if m and m.group(1).startswith("http"):
            return m.group(1)
    return None


def _text(html: str, focus: str, chars: int) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", html, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    i = text.lower().find(focus.lower())
    start = max(0, i - chars // 3) if i >= 0 else 0
    return text[start:start + chars]
