"""Small live client against the official 2025-11-01 API contract.

Source: https://docs.crustdata.com/general/permissions and linked endpoint docs.
Credentials stay in memory. Durable credit intents/cache prevent blind paid retries.
"""
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
import httpx
from sqlalchemy import create_engine
from app.providers.interfaces import IntegrationUnavailable


class CrustdataClient:
    def __init__(self, key=None, credit_limit="0.2", journal_path="crustdata-credits.db", client=None):
        key = key or os.getenv("CRUSTDATA_API_KEY")
        if not key:
            raise IntegrationUnavailable("INTEGRATION_UNAVAILABLE: CRUSTDATA_API_KEY is missing")
        self.http = client or httpx.Client(base_url="https://api.crustdata.com", headers={
            "Authorization": "Bearer " + key, "x-api-version": "2025-11-01"}, timeout=30, follow_redirects=False)
        self.limit = Decimal(str(credit_limit))
        self.run_limits = {}
        self.engine = create_engine("sqlite:///" + str(Path(journal_path).resolve()), connect_args={"check_same_thread": False, "timeout":30})
        self.permissions = None
        with self.engine.begin() as c:
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, path TEXT NOT NULL, reserved TEXT NOT NULL, charged TEXT, state TEXT NOT NULL, http_status INTEGER, response TEXT)")

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
        if not amount.is_finite() or amount < 0 or amount > 10:
            raise ValueError("provider credit limit must be between 0 and 10")
        self.run_limits[run_id] = amount

    def post(self, run_id, path, payload, units=1):
        if self.permissions is None:
            self.permissions = self.free_get("/account/endpoints")
        endpoint = next((e for e in self.permissions.get("endpoints", []) if e.get("path") == path), None)
        if not endpoint or endpoint.get("status") != "enabled" or endpoint.get("base_credits") is None:
            raise IntegrationUnavailable("CRUSTDATA_ENDPOINT_UNAVAILABLE_OR_UNPRICED: " + path)
        # Calls below request no premium fields, filters, or modes.
        reserved = Decimal(str(endpoint["base_credits"])) * units
        call_id = hashlib.sha256(json.dumps([run_id, path, payload], sort_keys=True).encode()).hexdigest()
        with self.engine.connect() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            prior = c.exec_driver_sql("SELECT * FROM calls WHERE id=?", (call_id,)).mappings().first()
            if prior:
                c.rollback()
                if prior["state"] == "succeeded": return json.loads(prior["response"])
                raise IntegrationUnavailable("CRUSTDATA_PRIOR_CALL_UNRESOLVED: inspect provider credit journal; no automatic paid retry")
            rows = c.exec_driver_sql("SELECT reserved, charged FROM calls WHERE run_id=?", (run_id,)).all()
            committed = sum((Decimal(charged if charged is not None else amount) for amount, charged in rows), Decimal(0))
            if committed + reserved > self.run_limits.get(run_id,self.limit):
                c.rollback()
                raise IntegrationUnavailable("CRUSTDATA_CREDIT_LIMIT: paid call blocked")
            c.exec_driver_sql("INSERT INTO calls VALUES(?,?,?,?,NULL,'pending',NULL,NULL)", (call_id, run_id, path, str(reserved)))
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
        with self.engine.begin() as c:
            c.exec_driver_sql("UPDATE calls SET charged=?,state=?,http_status=?,response=? WHERE id=?",
                (charged, "succeeded" if success else "failed", response.status_code, json.dumps(body) if success else None, call_id))
        if not success:
            raise IntegrationUnavailable(f"CRUSTDATA_HTTP_{response.status_code}: {path}; response body omitted to protect credentials")
        return body

    def usage(self, run_id):
        with self.engine.connect() as c:
            return [dict(r) for r in c.exec_driver_sql("SELECT id,path,reserved,charged,state,http_status FROM calls WHERE run_id=?", (run_id,)).mappings()]

    def search(self, run_id, query):
        return self.post(run_id, "/web/search/live", {"query":query,"sources":["web"],"page":1})

    def identify(self, run_id, domains):
        return self.post(run_id, "/company/identify", {"domains":domains,"exact_match":True}, units=len(domains))

    def fetch(self, run_id, urls):
        return self.post(run_id, "/web/enrich/live", {"urls":urls}, units=len(urls))
