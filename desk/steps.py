"""The six steps. Each is a plain function: typed input in, typed output out.

v0: steps 1-2 are real (step 1 lives in understand.py); 3, 4 and 6 are stubs; 5 is a placeholder
rule (cheapest unflagged FOUND quote).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as urlquote

import yaml

from desk import extract
from desk.crustdata import Crustdata
from desk.schemas import (STATUS_RANK, Check, Decision, FindResult, Lead, Order, PartCandidate, PriceTier,
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
    retry once with the next candidate and keep whichever attempt did better."""
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

    exact = [q for q in quotes if q.status != Status.NO_MATCH]
    alternatives = [q for q in quotes if q.status == Status.NO_MATCH]
    leads = _discover(spec, pack, cd)

    statuses = [q.status for q in exact] or [Status.NO_MATCH if alternatives else Status.NOT_FOUND]
    best = min(statuses, key=STATUS_RANK.index)
    reason = "; ".join(f"{q.vendor}: {q.status.value}" + (f" ({q.note})" if q.note else "")
                       + (" [FLAGGED]" if q.flags else "") for q in quotes)
    return FindResult(line_id=spec.line_id, mpn=spec.mpn, maker=spec.maker, status=best, reason=reason,
                      quotes=exact, alternatives=alternatives, leads=leads)


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
        q.flags.append(f"possible clone: same part number, made by {q.manufacturer}, not {spec.maker}")

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
    """Cheapest unflagged FOUND quote. Flagged quotes (e.g. possible clones) are never picked by code:
    they go to the founder, but only when they'd change the result (cheaper, or the only option)."""
    rule = "v0: cheapest unflagged FOUND quote by item cost (no landed cost, no reliability yet)"
    considered = [{"vendor": q.vendor, "mpn": q.mpn, "maker": q.manufacturer, "status": q.status.value,
                   "unit_price_inr": q.unit_price_inr, "buy_qty": q.buy_qty, "flags": q.flags,
                   "note": q.note, "url": q.url} for q in find.quotes]
    found = [q for q in find.quotes if q.status == Status.FOUND and q.unit_price_inr is not None]

    def total(q: Quote) -> float:
        return round(q.unit_price_inr * q.buy_qty, 2)

    clean = [q for q in found if not q.flags]
    best = min(clean, key=total) if clean else None
    review = [{"vendor": q.vendor, "mpn": q.mpn, "maker": q.manufacturer, "flags": q.flags,
               "unit_price_inr": q.unit_price_inr, "total_inr": total(q), "url": q.url,
               "ask": (f"Cheaper than the pick by ₹{round(total(best) - total(q), 2)}, but flagged. Accept it?"
                       if best else "The only option found, but flagged. Accept it?")}
              for q in found if q.flags and (best is None or total(q) < total(best))]

    if best is None:
        status = "NEEDS_FOUNDER" if review else "NONE"
        reason = ("only flagged options found; waiting for the founder" if review
                  else f"no vendor qualifies ({find.status.value}): {find.reason}")
        return Decision(line_id=spec.line_id, status=status, rule=rule, picked=None, total_inr=None,
                        reason=reason, founder_review=review, considered=considered)

    reason = (f"{best.vendor}: {best.buy_qty} x ₹{best.unit_price_inr} = ₹{total(best)}; {best.manufacturer} "
              f"{best.mpn}; stock {best.stock}; source {best.url} fetched {best.fetched_at:%Y-%m-%d %H:%M} UTC")
    if review:
        reason += f"; {len(review)} cheaper flagged option(s) sent to the founder"
    return Decision(line_id=spec.line_id, status="PICKED", rule=rule, picked=best, total_inr=total(best),
                    reason=reason, founder_review=review, considered=considered)


# ---------- step 6: order (STUB) ----------

def place_order(decision: Decision, run_id: str) -> Order | None:
    q = decision.picked
    if decision.status != "PICKED" or q is None:
        return None
    key = hashlib.sha256(f"{run_id}:{decision.line_id}:{q.vendor_domain}".encode()).hexdigest()[:16]
    return Order(line_id=decision.line_id, vendor=q.vendor, qty=q.buy_qty, amount_inr=decision.total_inr,
                 state="PLANNED", idempotency_key=key)
