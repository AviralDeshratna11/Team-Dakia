"""Helpers for looking at steps and runs, as tables. Used by the notebook and the CLI."""

from __future__ import annotations

import re

import pandas as pd

from desk.crustdata import read_cache
from desk.schemas import FindResult, SpecResult


def quotes(find: FindResult, alternatives: bool = False) -> pd.DataFrame:
    rows = find.alternatives if alternatives else find.quotes
    return pd.DataFrame([{
        "vendor": q.vendor, "status": q.status.value, "mpn": q.mpn, "maker": q.manufacturer,
        "stock": q.stock, "moq": q.moq, "buy_qty": q.buy_qty, "unit": q.unit_price, "cur": q.currency,
        "unit_inr": q.unit_price_inr, "flags": "; ".join(q.flags), "note": q.note, "by": q.extracted_by, "url": q.url,
        "cache_key": q.cache_key,
    } for q in rows])


def specs(sr: SpecResult) -> pd.DataFrame:
    return pd.DataFrame([{"line_id": s.line_id, "description": s.description, "mpn": s.mpn, "maker": s.maker,
                          "alternates": ", ".join(a.mpn for a in s.alternates), "qty": s.qty,
                          "params": s.params, "source": s.source} for s in sr.specs])


def tiers(find: FindResult) -> pd.DataFrame:
    return pd.DataFrame([{"vendor": q.vendor, **t.model_dump()} for q in find.quotes for t in q.tiers])


def leads(find: FindResult) -> pd.DataFrame:
    return pd.DataFrame([l.model_dump() for l in find.leads])


def steps(records: list[dict]) -> pd.DataFrame:
    """One row per recorded step: what ran, how long, what it cost."""
    return pd.DataFrame([{
        "step": r["step"], "part": r["part"], "ms": r["duration_ms"],
        "calls": len(r["calls"]), "cached": sum(c["cached"] for c in r["calls"]),
        "credits": sum(c["credits"] for c in r["calls"]),
        "error": (r["error"] or "").strip().splitlines()[-1] if r["error"] else "",
    } for r in records])


def calls(records: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"step": r["step"], "part": r["part"], **c} for r in records for c in r["calls"]])


def page(cache_key: str) -> dict:
    """The raw cached Crustdata response for a fetched page or search."""
    entry = read_cache(cache_key)
    if entry is None:
        raise KeyError(f"nothing cached under {cache_key}")
    return entry


def page_text(cache_key: str, chars: int = 3000, around: str | None = None) -> str:
    """Visible text of a cached page, optionally centred on a phrase."""
    html = page(cache_key)["response"].get("content") or ""
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    if around and (i := text.find(around)) >= 0:
        return text[max(0, i - chars // 2): i + chars // 2]
    return text[:chars]
