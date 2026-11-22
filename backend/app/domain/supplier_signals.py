"""Deterministic supplier signals computed from live company data.

The LLM never decides these. Each verdict is True, False or None (not enough evidence),
and carries the human-readable reasons behind it so the trust card can cite them.
A None never becomes a pass: the decision engine treats it as unverified.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

RED_FLAG_TERMS = ("fraud", "scam", "cheat", "raid", "seized", "arrest", "duped", "evasion", "insolven",
                  "bankrupt", "shut down", "sealed", "blacklist", "counterfeit", "fake ", "default on", "penalty")
TRADE_INDUSTRY_TERMS = ("wholesale", "retail", "import and export", "international trade", "trading")
# "Enterprises" and "Impex" are common names for Indian manufacturers too, so they are not trader evidence.
TRADER_NAME_TERMS = ("trading", "traders", "trade links")
RFQ_TITLE_TERMS = ("export", "sales", "business development", "marketing", "merchandis")
OWNER_TITLE_TERMS = ("founder", "owner", "managing director", "director", "ceo", "partner", "proprietor")
OPS_ROLE_TERMS = ("operation", "manufactur", "production", "quality")
SALES_ROLE_TERMS = ("sales", "business_development", "business development", "marketing")


@dataclass
class Assessment:
    signals: dict[str, bool | None]
    kind: str
    region_match: bool | None
    in_country: bool | None
    relevant: bool | None
    locations: list[str]
    contact: dict[str, str] | None
    profile: dict[str, Any]
    reasons: dict[str, list[str]] = field(default_factory=dict)


def _get(data: dict, path: str):
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _texts(*values) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif isinstance(value, list):
            out.extend(_texts(*value))
    return out


def _has(texts: list[str], terms) -> str | None:
    for text in texts:
        low = text.lower()
        for term in terms:
            if term.lower() in low:
                return text
    return None


def _number(value) -> float | None:
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def factory_verdict(data: dict, job_counts: dict[str, int | None] | None) -> tuple[bool | None, list[str], list[str]]:
    """Factory (True), trader (False) or not enough evidence (None), with reasons for and against."""
    strong_for, strong_against, support_for, support_against = [], [], [], []
    naics = str(_get(data, "taxonomy.primary_naics_detail.naics_code") or "")
    naics_title = _get(data, "taxonomy.primary_naics_detail.industry") or _get(data, "taxonomy.primary_naics_detail.sector") or ""
    if naics[:2] in {"31", "32", "33"}:
        strong_for.append(f"Industry code NAICS {naics} {naics_title} is a manufacturing code".strip())
    elif naics[:2] == "42":
        strong_against.append(f"Industry code NAICS {naics} {naics_title} is wholesale trade".strip())
    elif naics[:2] in {"44", "45"}:
        strong_against.append(f"Industry code NAICS {naics} {naics_title} is retail".strip())
    industries = _texts(_get(data, "taxonomy.professional_network_industry"), _get(data, "taxonomy.professional_network_industries"),
                        _get(data, "basic_info.industries"))
    if (label := _has(industries, ("manufactur",))):
        strong_for.append(f"Listed industry: {label}")
    if (label := _has(industries, TRADE_INDUSTRY_TERMS)) and not _has(industries, ("manufactur",)):
        strong_against.append(f"Listed industry: {label}")
    if job_counts:
        production, sales = job_counts.get("production"), job_counts.get("sales")
        if production and production >= (sales or 0):
            support_for.append(f"{production} production or quality job posts vs {sales or 0} sales posts")
        elif sales and sales >= 3 and not production:
            support_against.append(f"{sales} sales job posts and no production or quality posts")
    roles = _get(data, "headcount.by_role_percent")
    if isinstance(roles, dict):
        shares = {k.lower(): _number(raw) or 0.0 for k, raw in roles.items()}
        ops = sum(share for k, share in shares.items() if any(t in k for t in OPS_ROLE_TERMS))
        sales = sum(share for k, share in shares.items() if any(t in k for t in SALES_ROLE_TERMS))
        if ops >= 20:
            support_for.append(f"{ops:.0f}% of staff in operations or production roles")
        elif sales >= 50 and ops < 10:
            support_against.append(f"{sales:.0f}% of staff in sales roles, under 10% in operations")
    description = _texts(_get(data, "basic_info.description"))
    if _has(description, ("manufactur", "factory", "tannery", "production unit", "in-house production")):
        support_for.append("Company description mentions manufacturing")
    name = _texts(_get(data, "basic_info.name"))
    if _has(name, TRADER_NAME_TERMS) or _has(description, ("trading company", "we are traders", "sourcing agent", "buying agent")):
        support_against.append("Name or description suggests a trading or sourcing company")
    reasons_for, reasons_against = strong_for + support_for, strong_against + support_against
    if strong_for and not strong_against:
        verdict = True
    elif strong_against and not strong_for:
        verdict = False
    elif strong_for and strong_against:
        verdict = None  # Conflicting records: say so, never guess.
    elif len(support_for) >= 2 and not support_against:
        verdict = True
    elif len(support_against) >= 2 and not support_for:
        verdict = False
    else:
        verdict = None
    return verdict, reasons_for, reasons_against


def stability_verdict(data: dict, today: datetime) -> tuple[bool | None, list[str]]:
    reasons = []
    status = _get(data, "basic_info.status")
    founded = _number(_get(data, "basic_info.year_founded"))
    headcount = _number(_get(data, "headcount.total"))
    growth = _get(data, "headcount.growth_percent") or {}
    change = _number(growth.get("six_months")) if isinstance(growth, dict) else None
    if change is None and isinstance(growth, dict):
        change = _number(growth.get("yoy"))
    age = today.year - int(founded) if founded else None
    if status and str(status).lower() != "active":
        return False, [f"Company status is '{status}'"]
    if age is not None:
        reasons.append(f"Founded {int(founded)} ({age} years)")
    if headcount is not None:
        reasons.append(f"{int(headcount)} employees on record")
    if change is not None:
        reasons.append(f"Headcount {'grew' if change >= 0 else 'fell'} {abs(change):.0f}% recently")
    if change is not None and change <= -25:
        return False, reasons + ["Headcount is shrinking fast"]
    if age is not None and age < 1:
        return False, reasons + ["Founded less than a year ago"]
    if age is not None and age >= 3 and headcount is not None and headcount >= 10:
        return True, reasons
    return None, reasons + ["Not enough age or headcount data to judge stability"]


def news_verdict(articles: list[dict]) -> tuple[bool | None, list[str], list[dict]]:
    items = [a for a in articles if isinstance(a, dict) and (a.get("article_title") or a.get("title"))]
    flagged = [a for a in items if _has([a.get("article_title") or a.get("title") or ""], RED_FLAG_TERMS)]
    if flagged:
        return False, [f"Red-flag headline: {a.get('article_title') or a.get('title')}" for a in flagged[:3]], items
    if items:
        return True, [f"{len(items)} recent news item(s), none with red-flag terms"], items
    return None, ["No news on record"], items


def named_people(data: dict) -> list[dict[str, str]]:
    people = []
    for group in ("decision_makers", "cxos", "founders"):
        for person in _get(data, f"people.{group}") or []:
            profile = person.get("basic_profile") if isinstance(person, dict) else None
            name = (profile or {}).get("name") or (person.get("name") if isinstance(person, dict) else None)
            if name:
                title = (profile or {}).get("current_title") or person.get("title") or ""
                people.append({"name": name, "title": title, "group": group})
    return people


def pick_contact(data: dict) -> dict[str, str] | None:
    people = named_people(data)
    for terms in (RFQ_TITLE_TERMS, OWNER_TITLE_TERMS):
        match = next((p for p in people if _has([p["title"]], terms)), None)
        if match:
            return {"name": match["name"], "title": match["title"]}
    return {"name": people[0]["name"], "title": people[0]["title"]} if people else None


def assess_company(data: dict, *, region: str | None, relevance_keywords: list[str], relevance_naics: list[str],
                   job_counts: dict[str, int | None] | None = None, today: datetime | None = None) -> Assessment:
    today = today or datetime.now(timezone.utc)
    factory, factory_for, factory_against = factory_verdict(data, job_counts)
    stable, stable_reasons = stability_verdict(data, today)
    clear, news_reasons, articles = news_verdict(_get(data, "news") or [])

    country = _get(data, "locations.country") or ""
    raw_hq = _get(data, "locations.headquarters") or ""
    parts = [p.strip() for p in raw_hq.split(",") if p.strip()]
    hq = ", ".join(parts[-3:]) if len(parts) > 4 else raw_hq  # some records list every local post office
    state = _get(data, "locations.state") or ""
    addresses = _texts(_get(data, "locations.all_office_addresses"))
    places = _texts(raw_hq, state, addresses)
    in_country = True if (str(country).lower() in {"india", "ind", "in"} or _has([hq], ("india",))) else (None if not country and not hq else False)
    if region:
        region_match = True if _has(places, (region,)) else (False if places else None)
    else:
        region_match = None

    texts = _texts(_get(data, "basic_info.name"), _get(data, "basic_info.description"), _get(data, "taxonomy.professional_network_industry"),
                   _get(data, "taxonomy.professional_network_specialities"), _get(data, "taxonomy.categories"), _get(data, "basic_info.industries"))
    naics = str(_get(data, "taxonomy.primary_naics_detail.naics_code") or "")
    relevant = True if (_has(texts, relevance_keywords) or any(naics.startswith(code) for code in relevance_naics)) else None

    revenue = _get(data, "revenue.estimated") or {}
    growth = _get(data, "headcount.growth_percent") or {}
    contact = pick_contact(data)
    profile = {
        "name": _get(data, "basic_info.name"), "domain": _get(data, "basic_info.primary_domain"),
        "description": (_get(data, "basic_info.description") or "")[:400],
        "founded": _get(data, "basic_info.year_founded"), "status": _get(data, "basic_info.status"),
        "headcount": _get(data, "headcount.total"),
        "headcount_change_6m": growth.get("six_months") if isinstance(growth, dict) else None,
        "headquarters": hq, "headquarters_raw": raw_hq if raw_hq != hq else None, "state": state, "country": country, "addresses": addresses[:3],
        "industry": _texts(_get(data, "taxonomy.professional_network_industry"))[:1],
        "naics": f"{naics} {_get(data, 'taxonomy.primary_naics_detail.industry') or ''}".strip() or None,
        "revenue_usd": [revenue.get("lower_bound_usd"), revenue.get("upper_bound_usd")] if revenue.get("upper_bound_usd") else None,
        "record_updated_at": data.get("updated_at"),
        "factory_for": factory_for, "factory_against": factory_against,
        "stable_reasons": stable_reasons, "news_reasons": news_reasons,
        "news": [{"title": a.get("article_title") or a.get("title"), "url": a.get("article_url") or a.get("url"),
                  "date": a.get("article_publish_date") or a.get("date")} for a in articles[:3]],
        "job_counts": job_counts or {},
        "contact": contact,
        "people": [{"name": p["name"], "title": p["title"]} for p in named_people(data)][:6],
    }
    return Assessment(
        signals={"identity": True, "factory": factory, "stable": stable, "complaints_clear": clear},
        kind="manufacturer" if factory else "trader" if factory is False else "unknown",
        region_match=region_match, in_country=in_country, relevant=relevant,
        locations=[p for p in [hq, *addresses] if p][:4], contact=contact, profile=profile,
        reasons={"factory": factory_for + [f"Against: {r}" for r in factory_against], "stable": stable_reasons, "complaints_clear": news_reasons})
