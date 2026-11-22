"""Live client against the official 2025-11-01 API contract.

Source: https://docs.crustdata.com/general/permissions and linked endpoint docs.
Credentials stay in memory. Durable credit intents/cache prevent blind paid retries.
Every paid call records why it was made (``purpose``) so the run's credit ledger
shows the decision each purchase served.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path
import httpx
from sqlalchemy import create_engine
from app.providers.interfaces import IntegrationUnavailable

# Company profiles refresh every 1-30 days upstream; reuse an enrichment for a week.
SHARED_CACHE_TTL = timedelta(days=7)
SHARED_CACHE_PATHS = {"/company/enrich"}


class CreditLimitReached(IntegrationUnavailable):
    """The run's credit ceiling blocks this call. Callers may finish with gaps."""


class RateLimited(IntegrationUnavailable):
    """429 or 5xx before any work was done: safe to retry the same call on a later tick."""
    def __init__(self, message, wait=None):
        super().__init__(message)
        self.wait = wait


class CrustdataClient:
    def __init__(self, key=None, credit_limit="25", journal_path="crustdata-credits.db", client=None, sleep=time.sleep, rate_limit_retries=3):
        key = key or os.getenv("CRUSTDATA_API_KEY")
        if not key:
            raise IntegrationUnavailable("INTEGRATION_UNAVAILABLE: CRUSTDATA_API_KEY is missing")
        self.http = client or httpx.Client(base_url="https://api.crustdata.com", headers={
            "Authorization": "Bearer " + key, "x-api-version": "2025-11-01"}, timeout=45, follow_redirects=False)
        self.limit = Decimal(str(credit_limit))
        self.run_limits: dict[str, Decimal] = {}
        self.engine = create_engine("sqlite:///" + str(Path(journal_path).resolve()), connect_args={"check_same_thread": False, "timeout":30})
        self.permissions = None
        self.sleep, self.rate_limit_retries = sleep, rate_limit_retries
        with self.engine.begin() as c:
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, path TEXT NOT NULL, reserved TEXT NOT NULL, charged TEXT, state TEXT NOT NULL, http_status INTEGER, response TEXT)")
            columns = {row[1] for row in c.exec_driver_sql("PRAGMA table_info(calls)")}
            for column in ("purpose", "request_hash", "created_at"):
                if column not in columns:
                    c.exec_driver_sql(f"ALTER TABLE calls ADD COLUMN {column} TEXT")

    def free_get(self, path):
        try:
            response = self.http.get(path)
        except httpx.TransportError:
            raise IntegrationUnavailable("CRUSTDATA_NETWORK_ERROR: account preflight failed") from None
        if response.status_code != 200:
            raise IntegrationUnavailable(f"CRUSTDATA_HTTP_{response.status_code}: account preflight failed")
        try: return response.json()
        except ValueError: raise IntegrationUnavailable("CRUSTDATA_INVALID_RESPONSE") from None

    def credits(self):
        return self.free_get("/account/credits")

    def set_run_limit(self, run_id, amount):
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0 or amount > 1000:
            raise ValueError("provider credit limit must be between 0 and 1000")
        self.run_limits[run_id] = amount

    def spent(self, run_id) -> Decimal:
        with self.engine.connect() as c:
            rows = c.exec_driver_sql("SELECT reserved, charged FROM calls WHERE run_id=?", (run_id,)).all()
        return sum((Decimal(charged if charged is not None else amount) for amount, charged in rows), Decimal(0))

    def post(self, run_id, path, payload, units=1, purpose=""):
        """Wait for x-ratelimit-reset and retry on 429/5xx (nothing was charged), then give up to the caller."""
        for attempt in range(self.rate_limit_retries + 1):
            try:
                return self._post(run_id, path, payload, units, purpose)
            except RateLimited as exc:
                if attempt == self.rate_limit_retries or (exc.wait or 0) > 65:
                    raise
                self.sleep(max(1.0, min(65.0, exc.wait or 2 ** (attempt + 1))))

    def _post(self, run_id, path, payload, units=1, purpose=""):
        if self.permissions is None:
            self.permissions = self.free_get("/account/endpoints")
        endpoint = next((e for e in self.permissions.get("endpoints", []) if e.get("path") == path), None)
        if not endpoint or endpoint.get("status") != "enabled" or endpoint.get("base_credits") is None:
            raise IntegrationUnavailable("CRUSTDATA_ENDPOINT_UNAVAILABLE_OR_UNPRICED: " + path)
        # Reservation uses the account's own base price; the response header settles the actual charge.
        reserved = Decimal(str(endpoint["base_credits"])) * units
        request_hash = hashlib.sha256(json.dumps([path, payload], sort_keys=True).encode()).hexdigest()
        call_id = hashlib.sha256(json.dumps([run_id, path, payload], sort_keys=True).encode()).hexdigest()
        stamp = datetime.now(timezone.utc).isoformat()
        with self.engine.connect() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            prior = c.exec_driver_sql("SELECT * FROM calls WHERE id=?", (call_id,)).mappings().first()
            if prior:
                c.rollback()
                if prior["state"] in {"succeeded", "cached"}: return json.loads(prior["response"])
                raise IntegrationUnavailable("CRUSTDATA_PRIOR_CALL_UNRESOLVED: inspect provider credit journal; no automatic paid retry")
            if path in SHARED_CACHE_PATHS:
                cutoff = (datetime.now(timezone.utc) - SHARED_CACHE_TTL).isoformat()
                shared = c.exec_driver_sql("SELECT response FROM calls WHERE request_hash=? AND state='succeeded' AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                                           (request_hash, cutoff)).first()
                if shared:
                    c.exec_driver_sql("INSERT INTO calls (id,run_id,path,reserved,charged,state,http_status,response,purpose,request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (call_id, run_id, path, "0", "0", "cached", 200, shared[0], purpose + " (reused a profile fetched in the last 7 days: 0 credits)", request_hash, stamp))
                    c.commit()
                    return json.loads(shared[0])
            rows = c.exec_driver_sql("SELECT reserved, charged FROM calls WHERE run_id=?", (run_id,)).all()
            committed = sum((Decimal(charged if charged is not None else amount) for amount, charged in rows), Decimal(0))
            limit = self.run_limits.get(run_id, self.limit)
            if committed + reserved > limit:
                c.rollback()
                raise CreditLimitReached(f"CRUSTDATA_CREDIT_LIMIT: paid call blocked ({committed} spent + {reserved} needed > {limit} ceiling)")
            c.exec_driver_sql("INSERT INTO calls (id,run_id,path,reserved,charged,state,http_status,response,purpose,request_hash,created_at) VALUES(?,?,?,?,NULL,'pending',NULL,NULL,?,?,?)",
                (call_id, run_id, path, str(reserved), purpose, request_hash, stamp))
            c.commit()
        try:
            response = self.http.post(path, json=payload)
        except httpx.TransportError:
            # Leave pending, conservatively consuming reserved credits until reconciliation.
            raise IntegrationUnavailable("CRUSTDATA_NETWORK_ERROR: paid call outcome unknown; no blind retry") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        usage = response.headers.get("x-credits-used")
        try: charged = str(Decimal(usage)) if usage is not None else None
        except Exception: charged = None
        success = response.status_code == 200 and body is not None
        if response.status_code in {402, 429} or response.status_code >= 500:
            charged = charged or "0"  # Rejected before work: nothing consumed, safe to try again later.
        with self.engine.begin() as c:
            c.exec_driver_sql("UPDATE calls SET charged=?,state=?,http_status=?,response=? WHERE id=?",
                (charged, "succeeded" if success else "failed", response.status_code, json.dumps(body) if success else None, call_id))
            if response.status_code == 429 or response.status_code >= 500:
                # Allow a later retry of the same call after a rate limit or server error.
                c.exec_driver_sql("DELETE FROM calls WHERE id=? AND state='failed'", (call_id,))
        if response.status_code == 429 or response.status_code >= 500:
            wait = response.headers.get("x-ratelimit-reset") or response.headers.get("retry-after")
            try: seconds = float(wait) if wait is not None else None
            except ValueError: seconds = None
            if seconds is not None and seconds > 1e9:  # an epoch timestamp rather than a delay
                seconds = max(0.0, seconds - time.time())
            raise RateLimited(f"CRUSTDATA_HTTP_{response.status_code}: {path}; retry after {wait or 'a few'} seconds", seconds)
        if not success:
            raise IntegrationUnavailable(f"CRUSTDATA_HTTP_{response.status_code}: {path}; response body omitted to protect credentials")
        return body

    def usage(self, run_id):
        with self.engine.connect() as c:
            return [dict(r) for r in c.exec_driver_sql("SELECT id,path,reserved,charged,state,http_status,purpose,created_at FROM calls WHERE run_id=? ORDER BY created_at", (run_id,)).mappings()]

    # ---- Web ---------------------------------------------------------------
    def search(self, run_id, query, purpose="Find candidate supplier pages"):
        return self.post(run_id, "/web/search/live", {"query":query,"sources":["web"],"location":"IN","page":1}, purpose=purpose)

    def news_search(self, run_id, query, start_date=None, purpose="Check for complaints or bad news"):
        payload = {"query":query,"sources":["news"],"location":"IN","page":1}
        if start_date: payload["start_date"] = int(start_date)
        return self.post(run_id, "/web/search/live", payload, purpose=purpose)

    def fetch(self, run_id, urls, purpose="Read supplier pages"):
        return self.post(run_id, "/web/enrich/live", {"urls":urls}, units=len(urls), purpose=purpose)

    # ---- Companies -----------------------------------------------------------
    def identify(self, run_id, domains, purpose="Resolve supplier domains to one company"):
        return self.post(run_id, "/company/identify", {"domains":domains,"exact_match":True}, units=len(domains), purpose=purpose)

    def company_search(self, run_id, body, purpose="Find manufacturers in the company graph"):
        return self.post(run_id, "/company/search", body, units=int(body.get("limit", 20)), purpose=purpose)

    def enrich(self, run_id, company_ids, fields, purpose="Check the supplier is real, stable and a factory"):
        return self.post(run_id, "/company/enrich", {"crustdata_company_ids":[int(i) for i in company_ids],"fields":fields},
                         units=len(company_ids), purpose=purpose)

    def job_count(self, run_id, filters, purpose="Count job postings to tell a factory from a trader"):
        """Counts only (limit 0): search bills per returned row, so this returns no rows."""
        body = self.post(run_id, "/job/search", {"filters":filters,"limit":0,"aggregations":[{"type":"count"}]}, units=1, purpose=purpose)
        for agg in body.get("aggregations") or []:
            if agg.get("type") == "count" and agg.get("value") is not None:
                return int(agg["value"])
        total = body.get("total_count")
        return int(total) if total is not None else None
