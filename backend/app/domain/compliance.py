"""Compliance screen for finalists: who runs it, adverse media on them, front-company flags.

Deterministic rules only. The verdict is Approve, Hold (a person reviews before anything
goes out) or Block (removed from the shortlist; the next-best supplier takes its place).
"""
from datetime import datetime, timezone
import re

from app.domain.supplier_signals import RED_FLAG_TERMS

OWNER_TITLES = ("founder", "owner", "proprietor", "partner", "managing director", "director", "chief executive", "ceo", "chairman", "md")
GENERIC_NAME_WORDS = {"pvt", "ltd", "private", "limited", "india", "the", "and", "co", "company", "enterprises", "international",
                      "industries", "exports", "export", "overseas", "group", "llp", "inc", "corporation", "leather", "leathers"}


def is_owner(title: str | None) -> bool:
    low = (title or "").lower()
    return any(re.search(r"\b" + re.escape(t) + r"\b", low) for t in OWNER_TITLES)


def entity_tokens(supplier) -> list[str]:
    """Words that tie an article to this company rather than to a namesake."""
    words = [w for w in re.findall(r"[a-z]+", supplier.name.lower()) if len(w) >= 4 and w not in GENERIC_NAME_WORDS]
    places = []
    for text in [supplier.profile.get("headquarters_raw") or "", supplier.profile.get("headquarters") or "", *supplier.locations]:
        for hub in ("kanpur", "agra", "noida", "unnao", "lucknow", "ambur", "chennai", "vaniyambadi", "ranipet", "kolkata", "jalandhar"):
            if hub in text.lower():
                places.append(hub)
    domain = (supplier.domain or "").split(".")[0].lower()
    if len(domain) >= 4 and domain not in GENERIC_NAME_WORDS and domain != "unknown":
        words.append(domain)
    return list(dict.fromkeys(words + places))


def classify_articles(person: str, supplier, articles: list[dict]) -> dict[str, list[dict]]:
    """Split news about a person into: tied to this company (red flag or not) vs namesakes cleared."""
    surname = person.split()[-1].lower() if person.split() else ""
    tokens = entity_tokens(supplier)
    out = {"adverse": [], "tied_clean": [], "namesakes_cleared": []}
    for a in articles:
        text = f"{a.get('title') or ''} {a.get('snippet') or ''}".lower()
        if surname and surname not in text:
            continue  # the search matched something else entirely
        tied = any(re.search(r"\b" + re.escape(t) + r"\b", text) for t in tokens)
        flagged = any(term in text for term in RED_FLAG_TERMS)
        item = {"title": a.get("title"), "url": a.get("url")}
        if tied and flagged:
            out["adverse"].append(item)
        elif tied:
            out["tied_clean"].append(item)
        else:
            out["namesakes_cleared"].append(item)
    return out


def front_company_flags(supplier, others, today: datetime | None = None) -> list[str]:
    """Signals that a business may not be what its papers say. Each one sends it to a person, never blocks alone."""
    today = today or datetime.now(timezone.utc)
    p = supplier.profile or {}
    flags = []
    founded = p.get("founded")
    if isinstance(founded, (int, float)) and today.year - int(founded) < 2:
        flags.append(f"Founded {int(founded)}: under two years of trading history")
    headcount = p.get("headcount")
    if supplier.kind == "manufacturer" and isinstance(headcount, (int, float)) and headcount <= 3:
        flags.append(f"Only {int(headcount)} staff on record for a factory")
    if p.get("headquarters_raw"):
        flags.append("Address on record is a list of local post offices, not a street address: confirm the factory address")
    address = p.get("headquarters") or ""
    if not p.get("headquarters_raw") and address.count(",") >= 3:
        shared = [o.name for o in others if o.id != supplier.id and (o.profile or {}).get("headquarters") == address]
        if shared:
            flags.append(f"Same street address as {', '.join(shared[:3])}")
    return flags


def verdict(directors: list[dict], flags: list[str], adverse: list[dict], lookup_failed: str | None = None) -> tuple[str, list[str]]:
    if adverse:
        return "block", [f"Adverse news tied to this company's leadership: {a['title']}" for a in adverse[:3]]
    reasons = []
    if lookup_failed:
        reasons.append(lookup_failed)
    if not directors:
        reasons.append("Could not confirm who owns or runs it")
    reasons.extend(flags)
    return ("hold", reasons) if reasons else ("approve", ["Owner identified and screened; no adverse news; no front-company signals"])
