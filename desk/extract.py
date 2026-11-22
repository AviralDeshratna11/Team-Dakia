"""Read product facts out of fetched HTML with plain code. Cheapest first: the site's own
embedded JSON, then JSON-LD. Each extractor returns a dict of Quote fields, or None if the
page has no product on it. The LLM fallback for unknown sites comes later.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

BLOCK_TITLES = ["access to this page has been denied", "just a moment", "attention required",
                "access denied", "are you a robot", "security check"]

CURRENCY_SYMBOLS = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP", "¥": "JPY"}


def is_blocked(title: str | None, html: str | None) -> bool:
    t = (title or "").lower()
    if any(m in t for m in BLOCK_TITLES):
        return True
    return not html or len(html) < 2000


def num(s) -> float | None:
    """'₹27.58000' -> 27.58, '4,01,929' -> 401929. None if no number."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"\d[\d,]*\.?\d*", str(s))
    return float(m.group().replace(",", "")) if m else None


def norm_mpn(s: str | None) -> str:
    return re.sub(r"[\s]", "", (s or "")).upper()


def same_maker(a: str | None, b: str | None) -> bool:
    """Loose: 'DIODES' matches 'Diodes Incorporated' (first word). Aliases go in the pack later."""
    ta, tb = re.findall(r"[a-z0-9]+", (a or "").lower()), re.findall(r"[a-z0-9]+", (b or "").lower())
    return bool(ta and tb and ta[0] == tb[0])


def is_google_redirect(url: str) -> bool:
    return "google.com/goto" in url or "google.com/url" in url


def domain(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.")


def next_data(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except json.JSONDecodeError:
        return None


def jsonld_products(html: str) -> list[dict]:
    out = []
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else data.get("@graph", [data]):
            if isinstance(obj, dict) and obj.get("@type") == "Product":
                out.append(obj)
    return out


# ---------- DigiKey ----------

def digikey(html: str) -> dict | None:
    data = (((next_data(html) or {}).get("props") or {}).get("pageProps") or {}).get("envelope", {}).get("data") or {}
    pq, ov = data.get("priceQuantity"), data.get("productOverview")
    if pq and ov:
        tiers, moqs = [], []
        for pack in pq.get("pricing") or []:
            pack_moq = int(num(pack.get("minOrderQuantity")) or 1)
            moqs.append(pack_moq)
            for t in pack.get("mergedPricingTiers") or []:
                price, brk = num(t.get("unitPrice")), num(t.get("brkQty"))
                if price is None or brk is None:
                    continue
                tiers.append({"min_qty": max(int(brk), pack_moq), "unit_price": price,
                              "currency": currency_of(t.get("unitPrice")) or "INR",
                              "packaging": pack.get("packaging")})
        stock = num(pq.get("qtyAvailable"))
        return {"extracted_by": "digikey:__NEXT_DATA__", "mpn": ov.get("manufacturerProductNumber"),
                "manufacturer": ov.get("manufacturer"), "stock": int(stock) if stock is not None else None,
                "moq": min(moqs) if moqs else None, "tiers": tiers}
    return jsonld_fallback(html, "digikey:json-ld")


# ---------- LCSC ----------

def lcsc_product(html: str) -> dict | None:
    wd = (((next_data(html) or {}).get("props") or {}).get("pageProps") or {}).get("webData") or {}
    if not wd.get("productModel"):
        return jsonld_fallback(html, "lcsc:json-ld")
    tiers = []
    for t in wd.get("productPriceList") or []:
        price = num(t.get("currencyPrice") if t.get("currencyPrice") is not None else t.get("usdPrice"))
        if price is None or t.get("ladder") is None:
            continue
        tiers.append({"min_qty": int(t["ladder"]), "unit_price": price,
                      "currency": CURRENCY_SYMBOLS.get(t.get("currencySymbol") or "$", "USD")})
    params = {p.get("paramNameEn"): p.get("paramValueEn") for p in wd.get("paramVOList") or []
              if p.get("paramNameEn")}
    return {"extracted_by": "lcsc:__NEXT_DATA__", "mpn": wd.get("productModel"),
            "manufacturer": wd.get("brandNameEn"), "package": wd.get("encapStandard"),
            "stock": wd.get("stockNumber"), "moq": wd.get("minBuyNumber"), "tiers": tiers,
            "params": params}


def lcsc_search_links(html: str, limit: int) -> list[str]:
    links = []
    for href in re.findall(r'href="(https://www\.lcsc\.com/product-detail/[^"?#]+)', html):
        if href not in links:
            links.append(href)
    return links[:limit]


# ---------- shared ----------

def currency_of(s) -> str | None:
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in str(s or ""):
            return code
    return None


def jsonld_fallback(html: str, label: str) -> dict | None:
    """One price, no tiers or stock count: enough to show the part exists."""
    for p in jsonld_products(html):
        offer = p.get("offers") or {}
        offer = offer[0] if isinstance(offer, list) and offer else offer
        price = num(offer.get("price"))
        brand = p.get("brand")
        return {"extracted_by": label, "mpn": p.get("mpn") or p.get("name"),
                "manufacturer": brand.get("name") if isinstance(brand, dict) else brand,
                "stock": None, "moq": None,
                "tiers": [{"min_qty": 1, "unit_price": price, "currency": offer.get("priceCurrency") or "INR"}]
                if price is not None else []}
    return None


EXTRACTORS = {"digikey": digikey, "lcsc": lcsc_product}
