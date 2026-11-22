"""The six steps. Each is a plain function: typed input in, typed output out.

v0: steps 1-2 are real; 3, 4 and 6 are stubs; 5 is a placeholder rule (cheapest FOUND quote).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as urlquote

import yaml

from desk import extract
from desk.crustdata import Crustdata
from desk.schemas import (STATUS_RANK, Check, Decision, FindResult, Lead, Order, PriceTier, Quote,
                          Request, Spec, Status)

PACKS_DIR = Path(__file__).parent / "packs"


def load_pack(category: str) -> dict:
    return yaml.safe_load((PACKS_DIR / f"{category}.yaml").read_text())


# ---------- step 1: understand the specs ----------

def understand_specs(req: Request) -> list[Spec]:
    """v0: no LLM. Every line must already carry an exact part number."""
    specs = []
    for line in req.lines:
        if not line.mpn:
            raise ValueError(f"line {line.line_id!r} has no mpn; v0 can't pick a part from a description yet")
        specs.append(Spec(line_id=line.line_id, description=line.description, mpn=line.mpn,
                          maker=line.maker, params=line.params, qty=req.units * line.qty_per_unit))
    return specs


# ---------- step 2: find suppliers ----------

def find_suppliers(spec: Spec, pack: dict, cd: Crustdata) -> FindResult:
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

    exact = [q for q in quotes if q.status != Status.NO_MATCH]
    alternatives = [q for q in quotes if q.status == Status.NO_MATCH]
    leads = _discover(spec, pack, cd)

    statuses = [q.status for q in exact] or [Status.NO_MATCH if alternatives else Status.NOT_FOUND]
    best = min(statuses, key=STATUS_RANK.index)
    reason = "; ".join(f"{q.vendor}: {q.status.value}" + (f" ({q.note})" if q.note else "") for q in quotes)
    return FindResult(line_id=spec.line_id, status=best, reason=reason, quotes=exact,
                      alternatives=alternatives, leads=leads)


def _quote(spec: Spec, dist: dict, page: dict, pack: dict) -> Quote:
    if not page.get("success") or extract.is_blocked(page.get("title"), page.get("content")):
        return _bare(spec, dist, page, Status.BLOCKED, f"page title: {page.get('title')!r}")
    fn = extract.EXTRACTORS.get(dist.get("extractor") or "")
    if fn is None:
        return _bare(spec, dist, page, Status.GAP, "no extractor for this site yet")
    fields = fn(page.get("content") or "")
    if fields is None:
        return _bare(spec, dist, page, Status.NO_MATCH, "no product data on page (no exact match?)")

    q = _bare(spec, dist, page, Status.FOUND, "")
    q = q.model_copy(update={k: v for k, v in fields.items() if k != "tiers"})
    q.tiers = [PriceTier(**t) for t in fields.get("tiers", [])]

    if extract.norm_mpn(q.mpn) != extract.norm_mpn(spec.mpn):
        q.status, q.note = Status.NO_MATCH, f"page is for {q.mpn}, not {spec.mpn}"
        return q
    if spec.maker and q.manufacturer and not extract.same_maker(q.manufacturer, spec.maker):
        q.status = Status.NO_MATCH
        q.note = f"same part number but made by {q.manufacturer}, not {spec.maker}: possible clone"
        return q

    _price_at_qty(q, spec.qty, pack["fx_to_inr"])
    if q.unit_price is None:
        q.status, q.note = Status.GAP, "no price on page"
    elif q.stock is None:
        q.status, q.note = Status.GAP, "stock not on page"
    elif q.stock < spec.qty:
        q.status, q.note = Status.FOUND_UNFIT, f"stock {q.stock} < need {spec.qty}"
    elif q.buy_qty and q.buy_qty > spec.qty:
        q.note = f"MOQ forces buying {q.buy_qty}"
    return q


def _price_at_qty(q: Quote, qty: int, fx: dict) -> None:
    """Cheapest tier we qualify for at qty; if MOQ is above qty, buy the smallest tier."""
    if not q.tiers:
        return
    ok = [t for t in q.tiers if t.min_qty <= qty]
    if ok:
        tier, q.buy_qty = min(ok, key=lambda t: t.unit_price), qty
    else:
        tier = min(q.tiers, key=lambda t: (t.min_qty, t.unit_price))
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


# ---------- step 3: check suppliers (STUB) ----------

def check_suppliers(finds: list[FindResult]) -> list[Check]:
    seen: dict[str, Check] = {}
    for f in finds:
        for q in f.quotes:
            seen.setdefault(q.vendor_domain, Check(vendor=q.vendor, vendor_domain=q.vendor_domain,
                                                   status="NOT_RUN", note="STUB: step 3 not built yet"))
    return list(seen.values())


# ---------- step 4: buy data (STUB) ----------

def buy_data(finds: list[FindResult], budget_inr: float) -> list[dict]:
    gaps = [{"line_id": f.line_id, "vendor": q.vendor, "status": q.status.value, "note": q.note}
            for f in finds for q in f.quotes if q.status in (Status.GAP, Status.BLOCKED)]
    return [{**g, "action": "SKIPPED", "why": "STUB: step 4 not built yet"} for g in gaps]


# ---------- step 5: decide (placeholder rule) ----------

def decide(spec: Spec, find: FindResult, checks: list[Check]) -> Decision:
    rule = "v0: cheapest FOUND quote by item cost (no landed cost, no reliability yet)"
    considered = [{"vendor": q.vendor, "status": q.status.value, "unit_price_inr": q.unit_price_inr,
                   "buy_qty": q.buy_qty, "note": q.note, "url": q.url} for q in find.quotes]
    ok = [q for q in find.quotes if q.status == Status.FOUND and q.unit_price_inr is not None]
    if not ok:
        return Decision(line_id=spec.line_id, rule=rule, picked=None, total_inr=None, considered=considered,
                        reason=f"no vendor qualifies ({find.status.value}): {find.reason}")
    best = min(ok, key=lambda q: q.unit_price_inr * q.buy_qty)
    total = round(best.unit_price_inr * best.buy_qty, 2)
    reason = (f"{best.vendor}: {best.buy_qty} x ₹{best.unit_price_inr} = ₹{total}; stock {best.stock}; "
              f"source {best.url} fetched {best.fetched_at:%Y-%m-%d %H:%M} UTC")
    return Decision(line_id=spec.line_id, rule=rule, picked=best, total_inr=total, reason=reason,
                    considered=considered)


# ---------- step 6: order (STUB) ----------

def place_order(decision: Decision, run_id: str) -> Order | None:
    q = decision.picked
    if q is None:
        return None
    key = hashlib.sha256(f"{run_id}:{decision.line_id}:{q.vendor_domain}".encode()).hexdigest()[:16]
    return Order(line_id=decision.line_id, vendor=q.vendor, qty=q.buy_qty, amount_inr=decision.total_inr,
                 state="PLANNED", idempotency_key=key)
