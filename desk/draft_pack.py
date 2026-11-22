"""Step 1b: a request for a field we have no pack for. An agent researches the field and drafts one.

The draft goes to desk/packs/drafts/<name>.yaml, marked draft and low confidence. The founder approves
it before it's used (moved to desk/packs/), and results from a drafted pack say so.
"""

from __future__ import annotations

import re
import shutil

import yaml
from pydantic import BaseModel

from desk.agent import AgentResult, Tool, run_agent
from desk.crustdata import Crustdata
from desk.investigate import _real_url, _text  # page helpers
from desk import extract
from desk.steps import PACKS_DIR

DRAFTS_DIR = PACKS_DIR / "drafts"

# Made-to-order fields: is it a real factory or a trader? (Job Search + Person Search titles; see check.py)
FACTORY_SIGNAL = {'weight': 2, 'max_people': 5, 'factory_keywords': ['operator', 'machine', 'production', 'stitch', 'tailor', 'cutting', 'quality', 'qc', 'supervisor', 'factory', 'plant', 'karigar', 'tannery', 'technician', 'maintenance', 'manufactur'], 'trader_keywords': ['sales', 'business development', 'export', 'merchandis', 'marketing', 'trader', 'trading', 'sourcing', 'account manager'], 'contact_keywords': ['sales', 'export', 'business development', 'marketing', 'proprietor', 'owner', 'director', 'founder', 'partner', 'manager']}


class PackDraft(BaseModel):
    name: str                        # snake_case field name, e.g. leather_goods
    summary: str                     # one paragraph: how sourcing works in this field
    spec_hint: str                   # which specs decide whether a product fits, as param names
    company_search_query: str        # Crustdata Company Search query that finds real makers, tested
    supplier_country: str | None     # ISO-3 country code for the search filter, e.g. IND
    ordering: str                    # rfq | checkout
    rfq_questions: list[str]         # field-specific things to ask a vendor (material grade, samples...)
    trust_signals: list[str]         # what tells a real maker from a trader in this field
    typical_moq: str | None
    gst_rate: float | None           # India GST on these goods, if found; else null
    sources: list[str]               # URLs or "Crustdata" behind the draft


SYSTEM = """You draft a "category pack" for a sourcing agent in India: the field-specific knowledge needed
to source a product it has never sourced before. Research quickly, then finish.
- Test company_search_query with company_search: it must return real makers of this product in the
  right region. Adjust it once or twice if results are off.
- ordering: "rfq" if makers in this field sell by quotation (most B2B manufacturing), else "checkout".
- rfq_questions: what a buyer in this field must ask a maker (material and grade, finish, sizes,
  branding, sample cost and time, MOQ, production lead time, packaging).
- trust_signals: how to tell a real factory from a trader in this field.
- Only state facts you saw in tool results; put tax rates as null if you didn't find them. Be brief."""


def draft_pack(category: str, request_text: str, cd: Crustdata, base: dict) -> tuple[dict, AgentResult]:
    """Run the drafting agent; save and return the draft pack (engine-ready) and the agent run."""
    def web_search(query: str) -> list[dict]:
        return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("snippet")} for r in cd.search(query)[:6]]

    def fetch_page(url: str) -> dict:
        page = cd.fetch([url])[0]
        html = page.get("content") or ""
        if extract.is_blocked(page.get("title"), html):
            return {"blocked": True}
        return {"title": page.get("title"), "real_url": _real_url(html) or page.get("url"), "text": _text(html, "", 3000)}

    def company_search(query: str) -> list[dict]:
        return [{"name": (c.get("basic_info") or {}).get("name"), "domain": (c.get("basic_info") or {}).get("primary_domain"),
                 "founded": (c.get("basic_info") or {}).get("year_founded"),
                 "hq": (c.get("locations") or {}).get("headquarters")} for c in cd.company_search(query, limit=8)]

    s = lambda **p: {"type": "object", "properties": p, "required": list(p)}  # noqa: E731
    tools = [
        Tool("web_search", "Web search; top 6 results. 1 credit.", s(query={"type": "string"}), web_search, paid=True),
        Tool("fetch_page", "Fetch a page's text. 1 credit.", s(url={"type": "string"}), fetch_page, paid=True),
        Tool("company_search", "Crustdata Company Search in plain words (e.g. 'leather bag manufacturer Kanpur'). "
             "Returns up to 8 companies. ~0.25 credits.", s(query={"type": "string"}), company_search, paid=True),
    ]
    result = run_agent("pack_drafter", SYSTEM, f"Field: {category}\nThe founder's request:\n{request_text}",
                       tools, PackDraft, max_turns=8, credit_budget=6)
    if not result.report:
        raise RuntimeError(f"pack drafter didn't finish ({result.stop_reason})")
    d = PackDraft.model_validate(result.report)
    name = category                  # saved under the request's category, so later runs find it
    pack = {
        "name": name, "draft": True, "confidence": "low",
        "drafted_from": request_text.strip()[:300], "summary": d.summary, "sources": d.sources,
        "spec_hint": d.spec_hint,
        "fx_to_inr": base["fx_to_inr"],
        "find_mode": "company_search",
        "company_search": {"query": d.company_search_query, "country": d.supplier_country, "max_companies": 5},
        "distributors": [],
        "ordering": d.ordering,
        "rfq": {"max_vendors": 3, "questions": d.rfq_questions},
        "trust_signals": d.trust_signals,
        "typical_moq": d.typical_moq,
        "factory_signal": FACTORY_SIGNAL,
        "investigate": {"leads_per_part": 0},
        "unknown_vendor": {"ships_from": "IN", "transit_days": 7, "shipping_inr": 1500, "free_shipping_over_inr": None},
        "reliability": {**base["reliability"], "min_score": 0.5,
                        # small factories rarely have web traffic data, and "authorized" is an electronics idea
                        "weights": {k: v for k, v in base["reliability"]["weights"].items()
                                    if k not in ("authorized", "web_traffic")}},
        "landed_cost": {"destination_country": "IN", "import_duty_rate": 0.0,
                        "gst_rate": d.gst_rate if d.gst_rate is not None else 0.18, "placeholder_rates": True},
    }
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    (DRAFTS_DIR / f"{name}.yaml").write_text(
        "# DRAFT pack written by the pack_drafter agent. Review, then approve:\n"
        f"#   python -m desk pack approve {name}\n" + yaml.safe_dump(pack, sort_keys=False, allow_unicode=True))
    return pack, result


def approve(name: str) -> None:
    """The founder approves a drafted pack: it becomes usable (still marked draft / low confidence)."""
    src = DRAFTS_DIR / f"{name}.yaml"
    if not src.exists():
        raise FileNotFoundError(src)
    shutil.copy(src, PACKS_DIR / f"{name}.yaml")
