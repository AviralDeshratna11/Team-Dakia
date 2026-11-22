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
    vendors = {q.vendor_domain: q.vendor for f in finds for q in f.quotes if q.status in PRICED}
    checks, by_company = [], {}
    for domain, vendor in vendors.items():
        dist = dists.get(domain, {})
        c = _check_one(vendor, domain, dist.get("company_domain") or domain, dist, pack, cd)
        if c.company_id is not None and c.company_id in by_company:
            first = by_company[c.company_id]
            c.reason = f"same company as {first.vendor} ({first.vendor_domain}); " + c.reason
        elif c.company_id is not None:
            by_company[c.company_id] = c
        checks.append(c)
    return checks


def _check_one(vendor: str, domain: str, seller_domain: str, dist: dict, pack: dict, cd: Crustdata) -> Check:
    rules = pack["reliability"]
    w = rules["weights"]
    base = dict(vendor=vendor, vendor_domain=domain, seller_domain=seller_domain)
    src = f"Crustdata Company Identify ({seller_domain})"

    try:
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
                          weight=w["web_traffic"], source=esrc))

    if "authorized" in dist:
        signals.append(Signal(name="authorized", value="authorized distributor" if dist["authorized"]
                              else "not an authorized distributor", ok=bool(dist["authorized"]),
                              weight=w["authorized"], source="category pack"))

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
                 score=score, signals=signals, reason=reason)
