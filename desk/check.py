"""Step 3: check each seller is a real, stable company. Crustdata supplies the facts; code scores them.

One check per company, not per storefront: digikey.in and digikey.com resolve to the same record.
A seller with no company record is REJECTED. A check that couldn't run is UNVERIFIED, never a pass.
"""

from __future__ import annotations

from datetime import date

from desk.crustdata import Crustdata, CrustdataError
from desk.schemas import Check, FindResult, Signal, Status

PRICED = (Status.FOUND, Status.FOUND_UNFIT, Status.GAP)
ENRICH_FIELDS = ["basic_info", "headcount", "locations", "web_traffic"]


def check_suppliers(finds: list[FindResult], pack: dict, cd: Crustdata) -> list[Check]:
    dists = {d["domain"]: d for d in pack["distributors"]}
    vendors = {q.vendor_domain: (q.vendor, q.company_id) for f in finds for q in f.quotes if q.status in PRICED}
    checks, by_company = [], {}
    for domain, (vendor, company_id) in vendors.items():
        dist = dists.get(domain, {})
        c = _check_one(vendor, domain, dist.get("company_domain") or domain, dist, pack, cd, company_id)
        if c.company_id is not None and c.company_id in by_company:
            first = by_company[c.company_id]
            c.reason = f"same company as {first.vendor} ({first.vendor_domain}); " + c.reason
        elif c.company_id is not None:
            by_company[c.company_id] = c
        checks.append(c)
    return checks


def _check_one(vendor: str, domain: str, seller_domain: str, dist: dict, pack: dict, cd: Crustdata,
               company_id: int | None = None) -> Check:
    rules = pack["reliability"]
    w = rules["weights"]
    base = dict(vendor=vendor, vendor_domain=domain, seller_domain=seller_domain)
    src = f"Crustdata Company Identify ({seller_domain})"

    try:
        if company_id is not None and seller_domain.startswith("crustdata-"):
            # found by Company Search with no domain: the search result itself is the company record
            e0 = cd.enrich(company_id, ENRICH_FIELDS) or {}     # same call as below: cached, charged once
            matches = [{"confidence_score": 1.0, "company_data": {"crustdata_company_id": company_id,
                                                                  "basic_info": e0.get("basic_info") or {}}}]
            src = f"Crustdata Company Search (id {company_id})"
        else:
            matches = cd.identify(seller_domain)
    except CrustdataError as e:
        return Check(**base, company_id=None, company_name=None, status="UNVERIFIED", score=None, signals=[],
                     reason=f"identify failed: {e}")
    good = [m for m in matches if (m.get("confidence_score") or 0) >= rules["min_identify_confidence"]]
    if not good:
        return Check(**base, company_id=None, company_name=None, status="REJECTED", score=0.0,
                     signals=[Signal(name="real_company", value="no company record", ok=False,
                                     weight=w["real_company"], source=src)],
                     reason=f"{seller_domain} doesn't resolve to any company")
    if company_id is None or not seller_domain.startswith("crustdata-"):
        # Confidence is 1.0 even for wrong matches (amazon.in -> 'Indian.amazon', 'BioLaughs'), so the
        # company must list this domain as its own, and all such matches must be the same company.
        owned = [m for m in good if seller_domain in _domains(m["company_data"])]
        ids = {m["company_data"].get("crustdata_company_id") for m in owned}
        if len(ids) != 1:
            names = ", ".join((m["company_data"].get("basic_info") or {}).get("name") or "?" for m in good[:3])
            why = (f"{len(ids)} different companies list {seller_domain} as their own ({names})" if ids
                   else f"Crustdata matched {seller_domain} to {names}, but none lists it as its own domain")
            return Check(**base, company_id=None, company_name=None, status="UNVERIFIED", score=None,
                         signals=[Signal(name="real_company", value=why, ok=None, weight=w["real_company"],
                                         source=src)],
                         reason=f"can't tell which company is behind {seller_domain}: {why}")
        good = owned

    co = good[0]["company_data"]
    info = co.get("basic_info") or {}
    cid = co.get("crustdata_company_id")
    name = info.get("name")
    signals = [Signal(name="real_company", value=f"{name} (id {cid}, confidence {good[0]['confidence_score']:.2f})",
                      ok=True, weight=w["real_company"], source=src)]

    try:
        e = cd.enrich(cid, ENRICH_FIELDS) or {}
        esrc = f"Crustdata Company Enrich (id {cid})"
    except CrustdataError as err:
        e, esrc = None, f"Company Enrich failed: {err}"

    founded = info.get("year_founded")
    signals.append(Signal(name="age", value=f"founded {founded}" if founded else None,
                          ok=(date.today().year - founded >= rules["min_age_years"]) if founded else None,
                          weight=w["age"], source=src))

    head = (e or {}).get("headcount") or {}
    total = head.get("total")
    size_ok = total >= rules["min_headcount"] if total is not None else None
    signals.append(Signal(name="size", value=f"headcount {total} ({info.get('employee_count_range')})"
                          if total is not None else None, ok=size_ok, weight=w["size"], source=esrc))

    yoy = (head.get("growth_percent") or {}).get("yoy")
    signals.append(Signal(name="growth", value=f"headcount {yoy:+.1f}% yoy" if yoy is not None else None,
                          ok=(yoy >= rules["min_headcount_yoy_pct"]) if yoy is not None else None,
                          weight=w["growth"], source=esrc))

    traffic = ((e or {}).get("web_traffic") or {}).get("domain_traffic") or {}
    visitors = max((v.get("monthly_visitors") or 0 for v in traffic.values() if isinstance(v, dict)), default=None)
    signals.append(Signal(name="web_traffic", value=f"{visitors:,.0f} monthly visitors" if visitors else None,
                          ok=(visitors >= rules["min_monthly_visitors"]) if visitors else None,
                          weight=w.get("web_traffic", 0), source=esrc))

    if "web_traffic" not in w:
        signals = [s for s in signals if s.name != "web_traffic"]
    if "authorized" in dist:
        signals.append(Signal(name="authorized", value="authorized distributor" if dist["authorized"]
                              else "not an authorized distributor", ok=bool(dist["authorized"]),
                              weight=w["authorized"], source="category pack"))

    contacts = []
    fs = pack.get("factory_signal")
    if fs:
        sig, contacts = _factory_signal(cd, cid, fs, ((e or {}).get("locations") or {}).get("headquarters"))
        signals.append(sig)

    checked = [s for s in signals if s.ok is not None]
    score = round(sum(s.weight for s in checked if s.ok) / sum(s.weight for s in signals), 3)
    missing = [s.name for s in signals if s.ok is None]
    failed = [f"{s.name} ({s.value})" for s in signals if s.ok is False]
    hq = ((e or {}).get("locations") or {}).get("headquarters")
    alias = f"{domain} sells as {name} ({seller_domain}); " if seller_domain != domain else ""
    reason = (f"{alias}{name}, HQ {hq or 'unknown'}; score {score:.2f}"
              + (f"; failed: {', '.join(failed)}" if failed else "")
              + (f"; couldn't check: {', '.join(missing)}" if missing else ""))
    return Check(**base, company_id=cid, company_name=name, status="UNVERIFIED" if missing else "VERIFIED",
                 score=score, signals=signals, reason=reason, contacts=contacts)


def _factory_signal(cd: Crustdata, cid: int, cfg: dict, hq: str | None) -> tuple[Signal, list[dict]]:
    """Factory or trader? A factory hires and employs production roles; a trader mostly sales/export.
    Evidence: the company's job postings and current staff titles (Crustdata Job Search + Person Search).
    Staff outside the company's HQ country are dropped (namesake matches)."""
    factory_kw = [k.lower() for k in cfg["factory_keywords"]]
    trader_kw = [k.lower() for k in cfg["trader_keywords"]]
    try:
        jobs = [((j.get("job_details") or {}).get("title") or "") for j in cd.job_search(cid)]
        people = cd.person_search(cid, limit=cfg.get("max_people", 5))
    except CrustdataError as err:
        return Signal(name="factory", value=None, ok=None, weight=cfg["weight"], source=f"Job/Person Search failed: {err}"), []
    staff, contacts = [], []
    for p in people:
        bp = p.get("basic_profile") or {}
        country = (bp.get("location") or {}).get("country")
        if hq and country and country.lower() not in hq.lower():
            continue                     # e.g. a namesake in another country
        title = bp.get("current_title") or ""
        staff.append(title)
        url = ((p.get("social_handles") or {}).get("professional_network_identifier") or {}).get("profile_url")
        contacts.append({"name": bp.get("name"), "title": title, "profile_url": url})
    contact_kw = [k.lower() for k in cfg.get("contact_keywords", [])]
    contacts.sort(key=lambda c: not any(k in (c["title"] or "").lower() for k in contact_kw))

    titles = [t for t in jobs + staff if t]
    fac = [t for t in titles if any(k in t.lower() for k in factory_kw)]
    trd = [t for t in titles if any(k in t.lower() for k in trader_kw)]
    src = f"Crustdata Job Search ({len(jobs)} postings) + Person Search ({len(staff)} staff)"
    if fac:
        return Signal(name="factory", value=f"production roles: {', '.join(fac[:4])}", ok=True,
                      weight=cfg["weight"], source=src), contacts
    if trd:
        return Signal(name="factory", value=f"likely a trader: only sales/export roles ({', '.join(trd[:4])})",
                      ok=False, weight=cfg["weight"], source=src), contacts
    return Signal(name="factory", value=f"no production or sales roles seen ({len(titles)} titles)" if titles else None,
                  ok=None, weight=cfg["weight"], source=src), contacts
