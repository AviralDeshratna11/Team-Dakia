"""The six steps. Each is a plain function: typed input in, typed output out.

Step 1 lives in understand.py, step 3 in check.py, step 6 in orders.py. Step 4 (buy data) is a stub.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from urllib.parse import quote as urlquote

import yaml

from desk import extract
from desk.crustdata import Crustdata
from desk.check import check_suppliers  # noqa: F401  (step 3)
from desk.orders import place_order  # noqa: F401  (step 6)
from desk.schemas import (STATUS_RANK, Check, Decision, FindResult, LandedCost, Lead, PartCandidate, PriceTier,
                          Quote, Spec, Status)
from desk.understand import understand_specs  # noqa: F401  (step 1)

PACKS_DIR = Path(__file__).parent / "packs"


def load_pack(category: str) -> dict:
    return yaml.safe_load((PACKS_DIR / f"{category}.yaml").read_text())


def load_packs() -> dict[str, dict]:
    return {p.stem: yaml.safe_load(p.read_text()) for p in sorted(PACKS_DIR.glob("*.yaml"))}


# ---------- step 2: find suppliers ----------

def find_suppliers(spec: Spec, pack: dict, cd: Crustdata) -> FindResult:
    """Search the first part number; if no site has it (only BLOCKED, NO_MATCH or NOT_FOUND),
    retry once with the next candidate and keep whichever attempt did better.
    Packs with find_mode: company_search (made-to-order fields) shortlist makers instead."""
    if pack.get("find_mode") == "company_search":
        return _find_makers(spec, pack, cd)
    tried, best = [], None
    for cand in [PartCandidate(mpn=spec.mpn, maker=spec.maker), *spec.alternates][:2]:
        res = _find_one(spec.model_copy(update={"mpn": cand.mpn, "maker": cand.maker}), pack, cd)
        tried.append(f"{cand.mpn}: {res.status.value}")
        if best is None or STATUS_RANK.index(res.status) < STATUS_RANK.index(best.status):
            best = res
        if STATUS_RANK.index(res.status) < STATUS_RANK.index(Status.BLOCKED):
            break
    best.tried = tried
    return best


def _find_makers(spec: Spec, pack: dict, cd: Crustdata) -> FindResult:
    """Made-to-order fields have no catalogue prices: find real makers with Crustdata Company Search.
    Each becomes a GAP quote (no price yet); the RFQ step asks them for one."""
    cfg = pack["company_search"]
    country = {"india": "IND"}.get(str(cfg.get("country") or "").lower(), cfg.get("country"))   # Crustdata uses ISO-3
    filters = {"field": "locations.country", "type": "=", "value": country} if country else None
    quotes = []
    for c in cd.company_search(cfg["query"], filters=filters, limit=cfg.get("max_companies", 5)):
        b, cid = c.get("basic_info") or {}, c.get("crustdata_company_id")
        quotes.append(Quote(line_id=spec.line_id, vendor=b.get("name") or f"company {cid}",
                            vendor_domain=b.get("primary_domain") or f"crustdata-{cid}", company_id=cid,
                            url=b.get("website") or "", fetched_at=datetime.now(timezone.utc), status=Status.GAP,
                            found_by="search", note="maker found; no price yet (needs an RFQ)",
                            evidence=[f"Crustdata Company Search '{cfg['query']}': {b.get('name')}, "
                                      f"{(c.get('locations') or {}).get('headquarters')}"]))
    return FindResult(line_id=spec.line_id, mpn=spec.mpn, maker=spec.maker, tried=[f"company search: {cfg['query']}"],
                      status=Status.GAP if quotes else Status.NOT_FOUND,
                      reason=f"{len(quotes)} makers found by Crustdata Company Search", quotes=quotes,
                      alternatives=[], leads=[])


def _find_one(spec: Spec, pack: dict, cd: Crustdata) -> FindResult:
    mpn = urlquote(spec.mpn, safe="")
    dists = pack["distributors"]

    # hop 1: every distributor's product or search URL, in one batch
    first = {d["name"]: (d.get("url") or d["search_url"]).format(mpn=mpn) for d in dists}
    pages = {}
    for human in (False, True):
        names = [d["name"] for d in dists if bool(d.get("human_mode")) == human]
        if names:
            pages.update(zip(names, cd.fetch([first[n] for n in names], human_mode=human)))

    # hop 2: product pages linked from search pages
    follow: dict[str, list[str]] = {}
    for d in dists:
        page = pages[d["name"]]
        if d.get("search_url") and not extract.is_blocked(page.get("title"), page.get("content")):
            follow[d["name"]] = extract.lcsc_search_links(page.get("content") or "", d.get("max_products", 3))
    flat = [u for urls in follow.values() for u in urls]
    second = dict(zip(flat, cd.fetch(flat))) if flat else {}

    quotes: list[Quote] = []
    for d in dists:
        if d["name"] in follow:
            if not follow[d["name"]]:
                quotes.append(_bare(spec, d, pages[d["name"]], Status.NOT_FOUND, "search page had no product links"))
            for url in follow[d["name"]]:
                quotes.append(_quote(spec, d, second[url], pack))
        else:
            quotes.append(_quote(spec, d, pages[d["name"]], pack))

    suggested = sorted({q.mpn for q in quotes if q.status == Status.NOT_FOUND and q.mpn})
    for q in quotes:
        if q.status == Status.NOT_FOUND:
            q.mpn = None
    exact = [q for q in quotes if q.status != Status.NO_MATCH]
    alternatives = [q for q in quotes if q.status == Status.NO_MATCH]
    leads = _discover(spec, pack, cd)

    statuses = [q.status for q in exact] or [Status.NO_MATCH if alternatives else Status.NOT_FOUND]
    best = min(statuses, key=STATUS_RANK.index)
    reason = "; ".join(f"{q.vendor}: {q.status.value}" + (f" ({q.note})" if q.note else "")
                       + (" [FLAGGED]" if q.flags else "") for q in quotes)
    return FindResult(line_id=spec.line_id, mpn=spec.mpn, maker=spec.maker, status=best, reason=reason,
                      quotes=exact, alternatives=alternatives, leads=leads, suggested_mpns=suggested)


def _quote(spec: Spec, dist: dict, page: dict, pack: dict) -> Quote:
    if not page.get("success") or extract.is_blocked(page.get("title"), page.get("content")):
        return _bare(spec, dist, page, Status.BLOCKED, f"page title: {page.get('title')!r}")
    fn = extract.EXTRACTORS.get(dist.get("extractor") or "")
    if fn is None:
        return _bare(spec, dist, page, Status.GAP, "no extractor for this site yet")
    fields = fn(page.get("content") or "")
    if fields is None and dist.get("extractor") == "digikey":
        found = extract.digikey_search(page.get("content") or "")
        if found and found["product_count"] == 0:
            q = _bare(spec, dist, page, Status.NOT_FOUND, f"{dist['name']} doesn't list it"
                      + (f"; it suggests {found['suggestion']}" if found["suggestion"] else ""))
            q.mpn = found["suggestion"]          # carried to suggested_mpns, not quoted
            return q
        if found:
            return _bare(spec, dist, page, Status.NO_MATCH,
                         f"no exact product; {found['product_count']} matches only in: {', '.join(found['categories'][:3])}")
    if fields is None:
        return _bare(spec, dist, page, Status.NO_MATCH, "no product data on page (no exact match?)")

    q = _bare(spec, dist, page, Status.FOUND, "")
    q = q.model_copy(update={k: v for k, v in fields.items() if k != "tiers"})
    q.tiers = [PriceTier(**t) for t in fields.get("tiers", [])]
    return assess_quote(q, spec, pack)


def assess_quote(q: Quote, spec: Spec, pack: dict) -> Quote:
    """Same rules for every quote, however it was found: exact part? same maker? price, stock."""
    if extract.norm_mpn(q.mpn) != extract.norm_mpn(spec.mpn):
        q.status, q.note = Status.NO_MATCH, f"page is for {q.mpn}, not {spec.mpn}"
        return q
    if spec.maker and q.manufacturer and not extract.same_maker(q.manufacturer, spec.maker):
        q.flags.append(f"possible clone: same part number, made by {q.manufacturer}, not {spec.maker}")

    _price_at_qty(q, spec.qty, pack["fx_to_inr"])
    if not _valid_price(q.unit_price) or not _valid_price(q.unit_price_inr):
        q.status, q.note = Status.GAP, "no valid price in INR for the purchase quantity"
    elif q.ships_to_india is False:
        q.status, q.note = Status.FOUND_UNFIT, "doesn't ship to India"
    elif q.stock is not None and q.stock < q.buy_qty:
        q.status, q.note = Status.FOUND_UNFIT, f"stock {q.stock} < purchase quantity {q.buy_qty}"
    elif q.stock is None and q.lead_time_days is None:
        q.status, q.note = Status.GAP, "stock not on page"
    elif q.stock is None:
        q.note = f"no stock figure; vendor commits to {q.lead_time_days} days lead time"
    elif q.buy_qty and q.buy_qty > spec.qty:
        q.note = f"MOQ forces buying {q.buy_qty}"
    return q


def _valid_price(price: float | None) -> bool:
    return price is not None and isfinite(price) and price >= 0


def _price_at_qty(q: Quote, qty: int, fx: dict) -> None:
    """Cheapest tier we qualify for at qty; if MOQ is above qty, buy the smallest tier."""
    q.buy_qty = max(qty, q.moq or 0)
    if not q.tiers:
        return
    tiers = [tier for tier in q.tiers if tier.min_qty > 0 and _valid_price(tier.unit_price)]
    if not tiers:
        q.unit_price = q.unit_price_inr = None
        return
    ok = [tier for tier in tiers if tier.min_qty <= q.buy_qty]
    if ok:
        tier = min(ok, key=lambda t: t.unit_price)
    else:
        tier = min(tiers, key=lambda t: (t.min_qty, t.unit_price))
        q.buy_qty = tier.min_qty
    q.unit_price, q.currency = tier.unit_price, tier.currency
    rate = fx.get(tier.currency)
    q.unit_price_inr = round(tier.unit_price * rate, 4) if rate else None
    if rate is None:
        q.note = f"no fx rate for {tier.currency} in pack"


def _bare(spec: Spec, dist: dict, page: dict, status: Status, note: str) -> Quote:
    ts = page.get("timestamp")
    return Quote(line_id=spec.line_id, vendor=dist["name"], vendor_domain=dist["domain"],
                 url=page.get("url") or "", status=status, note=note, cache_key=page.get("cache_key"),
                 fetched_at=datetime.fromtimestamp(ts, timezone.utc) if ts else None)


def _discover(spec: Spec, pack: dict, cd: Crustdata) -> list[Lead]:
    cfg = pack.get("discovery")
    if not cfg:
        return []
    known = {d["domain"] for d in pack["distributors"]}
    leads = []
    for r in cd.search(cfg["query"].format(mpn=spec.mpn)):
        url = r.get("url", "")
        dom = None if extract.is_google_redirect(url) else extract.domain(url)
        if dom and any(dom.endswith(k) for k in known):
            continue
        leads.append(Lead(title=r.get("title", ""), url=r.get("url", ""), domain=dom,
                          snippet=r.get("snippet", "")))
    return leads[:cfg.get("max_leads", 8)]


# ---------- step 3: check suppliers: see check.py ----------


# ---------- step 4: buy data (STUB) ----------

def buy_data(finds: list[FindResult], budget_inr: float) -> list[dict]:
    gaps = [{"line_id": f.line_id, "vendor": q.vendor, "status": q.status.value, "note": q.note}
            for f in finds for q in f.quotes if q.status in (Status.GAP, Status.BLOCKED)]
    return [{**g, "action": "SKIPPED", "why": "STUB: step 4 not built yet"} for g in gaps]


# ---------- step 5: decide ----------

def decide(spec: Spec, find: FindResult, checks: list[Check], pack: dict,
           deadline: date | None) -> Decision:
    """Plain rules, in order (the brief's table):
    1. seller doesn't resolve to a real company          -> reject
    2. doesn't ship to India, or can't arrive by deadline -> reject
    3. below the reliability bar                          -> reject, shown as "cheaper but untrusted"
    4. check couldn't run, or quote is flagged (clone)    -> never picked silently: founder review
    5. among the rest                                     -> lowest landed cost
    """
    rule = "real company > ships in time > reliability bar > unflagged > lowest landed cost"
    by_domain = {c.vendor_domain: c for c in checks}
    dists = {d["domain"]: d for d in pack["distributors"]}
    min_score = pack["reliability"]["min_score"]
    considered, rejected, ok, review = [], [], [], []

    for q in find.quotes:
        issue = None
        if q.status != Status.FOUND:
            issue = f"{q.status.value} {q.note}".strip()
        elif not _valid_price(q.unit_price_inr) or (q.unit_price is not None and not _valid_price(q.unit_price)):
            issue = "invalid unit price"
        elif q.buy_qty is None or q.buy_qty < max(spec.qty, q.moq or 0):
            issue = "price does not cover the required purchase quantity"
        elif q.stock is not None and q.stock < q.buy_qty:
            issue = f"stock {q.stock} < purchase quantity {q.buy_qty}"
        if issue:
            considered.append(_row(q, None, None, f"not a usable quote: {issue}"))
            continue
        c = by_domain.get(q.vendor_domain)
        d = dists.get(q.vendor_domain) or {**pack.get("unknown_vendor", {}), "ships_to_india": q.ships_to_india}
        lc = landed_cost(q, spec.qty, d, pack)
        row = _row(q, c, lc, "")
        considered.append(row)
        if c is None or c.status == "REJECTED":
            rejected.append({**row, "rule": "not a real company", "why": c.reason if c else "not checked"})
        elif d.get("ships_to_india") is False:
            rejected.append({**row, "rule": "doesn't ship to India", "why": f"{q.vendor} not marked ships_to_india"})
        elif deadline and lc.arrives_by is None:
            rejected.append({**row, "rule": "arrival date unknown", "why": f"no confirmed arrival date for deadline {deadline}"})
        elif deadline and lc.arrives_by > deadline:
            rejected.append({**row, "rule": "misses the deadline", "why": f"arrives {lc.arrives_by}, need {deadline}"})
        elif c.score is not None and c.score < min_score:
            rejected.append({**row, "rule": "cheaper but untrusted", "why": f"score {c.score} < {min_score}: {c.reason}"})
        elif c.status == "UNVERIFIED" or q.flags or d.get("ships_to_india") is None:
            review.append({**row, "flags": q.flags
                           + ([f"unverified seller: {c.reason}"] if c.status == "UNVERIFIED" else [])
                           + (["no evidence it ships to India"] if d.get("ships_to_india") is None else [])})
        else:
            ok.append((q, c, lc))

    best = min(ok, key=lambda t: t[2].total_inr) if ok else None
    review = [{**r, "ask": (f"Cheaper than the pick by ₹{round(best[2].total_inr - r['landed_inr'], 2)}, but flagged. "
                            "Accept it?" if best else "The only option left, but flagged. Accept it?")}
              for r in review if best is None or r["landed_inr"] < best[2].total_inr]
    substitutes = sorted({q.mpn for q in find.alternatives if q.mpn} | set(find.suggested_mpns))

    waiting = [q for q in find.quotes if q.note.startswith("RFQ ")]
    if best is None and not review and waiting:
        return Decision(line_id=spec.line_id, status="WAITING_FOR_QUOTES", rule=rule, picked=None, total_inr=None,
                        reason=f"RFQs out to {len(waiting)} vendor(s): " + "; ".join(f"{q.vendor} ({q.note})" for q in waiting),
                        rejected=rejected, considered=considered)
    if best is None:
        status = "NEEDS_FOUNDER" if review else "NONE"
        if review:
            reason = "only flagged options left; waiting for the founder"
        else:
            reason = f"no vendor qualifies ({find.status.value})"
            if rejected:
                reason += "; rejected: " + "; ".join(f"{r['vendor']}: {r['rule']}" for r in rejected)
            if substitutes:
                reason += f"; possible substitutes: {', '.join(substitutes)}"
        return Decision(line_id=spec.line_id, status=status, rule=rule, picked=None, total_inr=None, reason=reason,
                        founder_review=review, rejected=rejected, considered=considered,
                        substitutes=substitutes if status == "NONE" else [])

    q, c, lc = best
    reason = (f"{q.vendor}: {q.manufacturer or ''} {q.mpn}, {q.buy_qty} x ₹{q.unit_price_inr} -> landed ₹{lc.total_inr:,.2f} "
              f"(₹{lc.per_unit_inr}/unit, arrives by {lc.arrives_by}); seller {c.company_name}, score {c.score}; "
              f"stock {q.stock}; source {q.url} fetched {q.fetched_at:%Y-%m-%d %H:%M} UTC")
    if lc.placeholder_rates:
        reason += "; shipping/duty/GST rates are placeholders"
    if rejected:
        reason += "; rejected: " + "; ".join(f"{r['vendor']} ({r['rule']})" for r in rejected)
    if review:
        reason += f"; {len(review)} cheaper flagged option(s) sent to the founder"
    return Decision(line_id=spec.line_id, status="PICKED", rule=rule, picked=q, landed=lc, total_inr=lc.total_inr,
                    reason=reason, founder_review=review, rejected=rejected, considered=considered)


def consolidate_shipping(decisions: list[Decision], pack: dict) -> list[Decision]:
    """Step 5b: lines picked from the same vendor ship together. One shipping fee per vendor (free if the
    vendor's combined order clears its threshold), split across the lines by goods value; duty and GST
    recomputed. (Per-line picks aren't revisited: a vendor that gets cheaper once combined isn't re-chosen.)"""
    cfg = pack["landed_cost"]
    dists = {d["domain"]: d for d in pack["distributors"]}
    groups: dict[str, list[Decision]] = {}
    for d in decisions:
        if d.status == "PICKED" and d.picked and d.landed:
            groups.setdefault(d.picked.vendor_domain, []).append(d)
    for domain, ds in groups.items():
        dist = dists.get(domain) or pack.get("unknown_vendor", {})
        goods = sum(d.landed.goods_inr for d in ds)
        free_over = dist.get("free_shipping_over_inr")
        fee = 0.0 if free_over is not None and goods >= free_over else float(dist.get("shipping_inr") or 0)
        imported = dist.get("ships_from") != cfg["destination_country"]
        for d in ds:
            lc = d.landed
            ship = fee * lc.goods_inr / goods if goods else 0.0
            duty = cfg["import_duty_rate"] * (lc.goods_inr + ship) if imported else 0.0
            gst = cfg["gst_rate"] * (lc.goods_inr + ship + duty)
            total = lc.goods_inr + ship + duty + gst
            need = round(lc.total_inr / lc.per_unit_inr) if lc.per_unit_inr else 1
            saved = round(lc.total_inr - total, 2)
            d.landed = lc.model_copy(update={"shipping_inr": round(ship, 2), "duty_inr": round(duty, 2),
                                             "gst_inr": round(gst, 2), "total_inr": round(total, 2),
                                             "per_unit_inr": round(total / need, 4)})
            d.total_inr = d.landed.total_inr
            if len(ds) > 1:
                d.reason += (f"; ships with {len(ds) - 1} other line(s) from {d.picked.vendor}: one ₹{fee:,.0f} "
                             f"shipping fee shared, landed now ₹{d.total_inr:,.2f} (saves ₹{saved:,.2f})")
    return decisions


def landed_cost(q: Quote, need_qty: int, dist: dict, pack: dict) -> LandedCost:
    """goods + shipping + import duty (if shipped from abroad) + GST, all in INR."""
    cfg = pack["landed_cost"]
    goods = q.unit_price_inr * q.buy_qty
    free_over = dist.get("free_shipping_over_inr")
    shipping = 0.0 if free_over is not None and goods >= free_over else float(dist.get("shipping_inr") or 0)
    imported = dist.get("ships_from") != cfg["destination_country"]
    duty = cfg["import_duty_rate"] * (goods + shipping) if imported else 0.0
    gst = cfg["gst_rate"] * (goods + shipping + duty)
    total = goods + shipping + duty + gst
    days = dist.get("transit_days")
    if days is not None and q.lead_time_days:
        days += q.lead_time_days
    return LandedCost(goods_inr=round(goods, 2), shipping_inr=round(shipping, 2), duty_inr=round(duty, 2),
                      gst_inr=round(gst, 2), total_inr=round(total, 2), per_unit_inr=round(total / need_qty, 4),
                      arrives_by=date.today() + timedelta(days=days) if days is not None else None,
                      placeholder_rates=bool(cfg.get("placeholder_rates")))


def _row(q: Quote, c: Check | None, lc: LandedCost | None, note: str) -> dict:
    return {"vendor": q.vendor, "found_by": q.found_by, "mpn": q.mpn, "maker": q.manufacturer, "status": q.status.value,
            "unit_price_inr": q.unit_price_inr, "buy_qty": q.buy_qty,
            "landed_inr": lc.total_inr if lc else None, "arrives_by": str(lc.arrives_by) if lc and lc.arrives_by else None,
            "seller_score": c.score if c else None, "seller_status": c.status if c else None,
            "flags": q.flags, "note": note or q.note, "url": q.url}


# ---------- step 6: order: see orders.py ----------
