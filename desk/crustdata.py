"""Crustdata client: retry on 429/5xx, and a disk cache so re-running a notebook cell costs nothing.

Cache files live in .cache/crustdata/<key>.json. Every call (hit or miss) is appended to trace.CALL_LOG.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

import httpx

from desk import ROOT
from desk.extract import is_blocked
from desk.trace import CALL_LOG

BASE = "https://api.crustdata.com"
CACHE_DIR = ROOT / ".cache" / "crustdata"
BLOCKED_TTL_S = 3600   # blocked pages are cached briefly, so re-running a cell doesn't pay to be refused again


class CrustdataError(Exception):
    pass


def cache_key(*parts: object) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def read_cache(key: str) -> dict | None:
    p = CACHE_DIR / f"{key}.json"
    if not p.exists():
        return None
    entry = json.loads(p.read_text())
    if entry.get("blocked"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"])).total_seconds()
        if age > BLOCKED_TTL_S:
            return None
    return entry


class Crustdata:
    def __init__(self, api_key: str | None = None, use_cache: bool = True, max_tries: int = 4):
        self.api_key = api_key or os.environ.get("CRUSTDATA_API_KEY")
        if not self.api_key:
            raise CrustdataError("CRUSTDATA_API_KEY is not set (put it in .env)")
        self.use_cache = use_cache
        self.max_tries = max_tries
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ---------- public ----------

    def search(self, query: str, sources: list[str] | None = None, location: str | None = None) -> list[dict]:
        body = {"query": query, "sources": sources or ["web"]}
        if location:
            body["location"] = location
        key = cache_key("search", body)
        entry = self._cached_or_call(key, "/web/search/live", body, label=f"search: {query}")
        return entry["response"].get("results", [])

    def fetch(self, urls: list[str], human_mode: bool = False) -> list[dict]:
        """Fetch pages. Returns one dict per URL, in order, each with an added 'cache_key'.
        Blocked or failed pages are cached for BLOCKED_TTL_S only, so a later run tries again."""
        out: dict[str, dict] = {}
        todo = []
        for url in urls:
            key = cache_key("fetch", url, human_mode)
            hit = read_cache(key) if self.use_cache else None
            if hit:
                CALL_LOG.append(self._log("/web/enrich/live", f"fetch: {url}", key, cached=True))
                out[url] = {**hit["response"], "cache_key": key}
            else:
                todo.append(url)
        mode = " [human_mode]" if human_mode else ""
        for i in range(0, len(todo), 10):
            batch = todo[i:i + 10]
            t0 = time.monotonic()
            body = {"urls": batch, "human_mode": True} if human_mode else {"urls": batch}
            resp, credits = self._post("/web/enrich/live", body)
            items = resp if isinstance(resp, list) else resp.get("results", [])
            for url, item in zip(batch, items):
                key = cache_key("fetch", url, human_mode)
                blocked = not item.get("success") or is_blocked(item.get("title"), item.get("content"))
                self._write_cache(key, "/web/enrich/live", {"urls": [url]}, item, credits / len(batch),
                                  blocked=blocked)
                out[url] = {**item, "cache_key": key}
            CALL_LOG.append(self._log("/web/enrich/live", f"fetch x{len(batch)}{mode}: " + ", ".join(batch),
                                      None, cached=False, credits=credits,
                                      ms=int((time.monotonic() - t0) * 1000)))
        return [out[u] for u in urls]

    def identify(self, domain: str) -> list[dict]:
        """Resolve a domain to companies. [] = no company record. 0.2 credits per match, misses free."""
        body = {"domains": [domain], "fields": ["crustdata_company_id", "basic_info"]}
        entry = self._cached_or_call(cache_key("identify", body), "/company/identify", body,
                                     label=f"identify: {domain}")
        resp = entry["response"]
        return (resp[0].get("matches") or []) if isinstance(resp, list) and resp else []

    def enrich(self, company_id: int, fields: list[str]) -> dict | None:
        body = {"crustdata_company_ids": [company_id], "fields": fields}
        entry = self._cached_or_call(cache_key("enrich", body), "/company/enrich", body,
                                     label=f"enrich: {company_id} {fields}")
        resp = entry["response"]
        if isinstance(resp, list):
            resp = resp[0] if resp else None
        if isinstance(resp, dict) and "matches" in resp:      # same envelope as identify
            m = resp.get("matches") or []
            resp = m[0].get("company_data") if m else None
        return resp

    def credits_left(self) -> dict:
        r = httpx.get(f"{BASE}/account/credits", headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    # ---------- internals ----------

    def _headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "x-api-version": "2025-11-01",
        }

    def _cached_or_call(self, key: str, path: str, body: dict, label: str) -> dict:
        hit = read_cache(key) if self.use_cache else None
        if hit:
            CALL_LOG.append(self._log(path, label, key, cached=True))
            return hit
        t0 = time.monotonic()
        resp, credits = self._post(path, body)
        entry = self._write_cache(key, path, body, resp, credits)
        CALL_LOG.append(self._log(path, label, key, cached=False, credits=credits,
                                  ms=int((time.monotonic() - t0) * 1000)))
        return entry

    def _post(self, path: str, body: dict) -> tuple[dict | list, float]:
        last = None
        for attempt in range(self.max_tries):
            try:
                r = httpx.post(BASE + path, headers=self._headers(), json=body, timeout=120)
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json(), float(r.headers.get("x-credits-used", 0) or 0)
            if r.status_code == 429:
                wait = min(int(r.headers.get("x-ratelimit-reset", "5") or 5), 60)
                last = f"429, waited {wait}s"
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                last = f"{r.status_code}"
                time.sleep(2 ** attempt)
                continue
            raise CrustdataError(f"{path} -> {r.status_code}: {r.text[:300]}")
        raise CrustdataError(f"{path} failed after {self.max_tries} tries ({last})")

    def _write_cache(self, key: str, path: str, body: dict, response, credits: float,
                     blocked: bool = False) -> dict:
        entry = {
            "blocked": blocked,
            "path": path,
            "body": body,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "credits": credits,
            "response": response,
        }
        (CACHE_DIR / f"{key}.json").write_text(json.dumps(entry, ensure_ascii=False))
        return entry

    @staticmethod
    def _log(path: str, label: str, key: str | None, cached: bool, credits: float = 0, ms: int = 0) -> dict:
        return {"endpoint": path, "what": label, "cache_key": key, "cached": cached,
                "credits": credits, "ms": ms}
