"""Live supplier discovery preserves unknown purchasing facts; no fixture fallback."""
from datetime import timedelta
import json
from urllib.parse import urlparse, parse_qs
from app.contracts import Supplier, Quote, Evidence, now
from app.config import pack_for
from app.providers.interfaces import IntegrationUnavailable


def supplier_url(url):
    """Unwrap ordinary search redirects; reject opaque redirect URLs and search engines."""
    for _ in range(3):
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower().removeprefix("www.")
        if domain.startswith("google.") or domain.endswith(".google.com") or domain in {"bing.com", "duckduckgo.com"}:
            params = parse_qs(parsed.query)
            target = next((x for key in ("url", "q") for x in params.get(key, []) if x.startswith("https://")), None)
            if not target: return None
            url = target
            continue
        return url if parsed.scheme == "https" and domain else None
    return None


class LiveDiscovery:
    def __init__(self, client, run_id):
        self.client, self.run_id = client, run_id

    def discover(self, request, specs):
        # Search is grounded in the actual requested items; company matching is exact-domain.
        requirements = " ".join(s.description+" "+" ".join(f"{k}: {v}" for k,v in s.parameters.items()) for s in specs)
        query = f"{requirements[:1500]} suppliers official website deliver to {request.destination}"
        evidence = []
        if request.supplier_domains:
            # Minimum-cost profile: user-selected domain -> Identify only. No search/fetch.
            results = [{"url":"https://"+d, "title":"User-selected supplier domain"} for d in request.supplier_domains]
        else:
            result = self.client.search(self.run_id, query)
            results = result.get("results", [])
            evidence.append(Evidence(reference="https://api.crustdata.com/web/search/live", provider="crustdata_web_search", mode="live",
                fact="supplier discovery search", value=json.dumps({"query":query,"results":results})[:24000]))
        # Prefer established catalogue candidates discovered in the live result set; otherwise
        # keep actual result domains as unclassified candidates, never call them factories.
        domains, pages = [], []
        excluded = {"youtube.com", "facebook.com", "linkedin.com", "indiamart.com", "justdial.com", "amazon.in", "amazon.com", "reddit.com"}
        for row in results:
            url = supplier_url(row.get("url", ""))
            if url is None: continue
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower().removeprefix("www.")
            if parsed.scheme != "https" or not domain or any(domain == d or domain.endswith("."+d) for d in excluded):
                continue
            if domain not in domains:
                domains.append(domain)
                pages.append(url)
            if len(domains) == 3: break
        if not domains:
            return [], [], evidence
        try:
            identified = self.client.identify(self.run_id, domains)
        except IntegrationUnavailable as exc:
            if "CRUSTDATA_CREDIT_LIMIT" not in str(exc): raise
            identified = []
            evidence.append(Evidence(reference="https://api.crustdata.com/company/identify",provider="credit_policy",mode="live",status="unverified",
                fact="identity resolution skipped",value="Remaining provider-credit budget cannot cover company identification. Search candidates remain unverified."))
        try:
            fetched = [] if request.supplier_domains else self.client.fetch(self.run_id, pages[:2])
        except IntegrationUnavailable as exc:
            if "CRUSTDATA_CREDIT_LIMIT" not in str(exc): raise
            fetched = []
            evidence.append(Evidence(reference="https://api.crustdata.com/web/enrich/live",provider="credit_policy",mode="live",status="unverified",
                fact="page retrieval skipped",value="Remaining provider-credit budget cannot cover page retrieval. Identity evidence is preserved; purchasing facts remain unknown."))
        fetched_by_url = {r.get("url"):r for r in fetched if r.get("success") is True and r.get("url")}
        suppliers, quotes = [], []
        seen = set()
        pack = pack_for(request.category)
        for domain, page in zip(domains, pages):
            identity = next((r for r in identified if r.get("matched_on", "").lower().removeprefix("www.") == domain), {})
            matches = identity.get("matches", [])
            # Exact domain plus a unique canonical company, rather than taking a fuzzy top match.
            matching = []
            for match in matches:
                company = match.get("company_data", {})
                basic = company.get("basic_info", {})
                known = [basic.get("primary_domain", "")] + basic.get("all_domains", [])
                if domain in [str(x).lower().removeprefix("www.") for x in known]: matching.append(company)
            ids = {str(m.get("crustdata_company_id") or m.get("basic_info", {}).get("crustdata_company_id")) for m in matching}
            resolved = len(ids) == 1 and "None" not in ids
            company = matching[0] if resolved else {}
            basic = company.get("basic_info", {})
            canonical = next(iter(ids)) if resolved else None
            sid = "crustdata:"+canonical if canonical else "domain:"+domain
            if sid in seen:
                next(s for s in suppliers if s.id == sid).aliases.append(domain)
                continue
            seen.add(sid)
            supplier = Supplier(id=sid, canonical_id=canonical, name=basic.get("name", domain), aliases=[domain], domain=domain,
                locations=[], kind="unknown", identity="resolved" if resolved else "unverified")
            suppliers.append(supplier)
            fact = Evidence(reference="https://api.crustdata.com/company/identify", provider="crustdata_company_identify", mode="live",
                fact="canonical company identity", value=json.dumps(identity)[:20000], status="verified" if resolved else "unverified")
            evidence.append(fact)
            refs = [fact.id] + [e.id for e in evidence if e.provider == "crustdata_web_search"]
            fetched_page = fetched_by_url.get(page)
            if fetched_page:
                content = fetched_page.get("content") or ""
                source = Evidence(reference=page, provider="crustdata_web_fetch", mode="live", fact="supplier page content (untrusted source text)",
                    value=json.dumps({"title":fetched_page.get("title"),"timestamp":fetched_page.get("timestamp"),"content":content[:16000]}))
                evidence.append(source)
                refs.append(source.id)
            for spec in specs:
                quotes.append(Quote(id=f"{spec.id}:{sid}", supplier_id=sid, spec_id=spec.id, currency="UNK", tiers={}, quantity=spec.quantity or 1,
                    stock=None, shipping_minor=None, lead_days=None, delivery_at=None, serves_destination=None,
                    matches_spec=None, observed_at=now(), expires_at=now()+timedelta(hours=pack.freshness_hours),
                    evidence_ids=refs, status="unverified", signals={k:None for k in pack.trust_signals}))
        return suppliers, quotes, evidence


# ---------------------------------------------------------------------------
# Company-graph-first discovery (category packs with live_discovery="company_search")
# ---------------------------------------------------------------------------
SEARCH_FIELDS = ["crustdata_company_id", "basic_info.name", "basic_info.primary_domain", "basic_info.description",
                 "locations.headquarters", "locations.state", "locations.country"]


class LiveCompanyDiscovery:
    """Find candidate makers in Crustdata's company graph: real companies with canonical IDs from the start."""
    def __init__(self, client, run_id):
        self.client, self.run_id = client, run_id

    def discover(self, request, specs):
        pack = pack_for(request.category)
        spec = specs[0]
        region = spec.parameters.get("location")
        product = spec.parameters.get("product")
        query = " ".join(x for x in [product, pack.search_query] if x)
        country = {"field": "locations.country", "type": "=", "value": "India"}
        attempts = []
        if region:
            attempts.append({"op": "and", "conditions": [country, {"field": "locations.state", "type": "=", "value": region}]})
            # State names are normalised upstream; fall back to matching the address text.
            attempts.append({"op": "and", "conditions": [country, {"field": "locations.all_office_addresses", "type": "(.)", "value": region}]})
        else:
            attempts.append(country)
        evidence, companies, body = [], [], None
        for filters in attempts:
            body = {"search": {"query": query, "mode": "hybrid"}, "filters": filters, "fields": SEARCH_FIELDS, "limit": pack.max_candidates}
            result = self.client.company_search(self.run_id, body, purpose=f"Find '{query}' companies in {region or 'India'}")
            companies = [c for c in result.get("companies") or [] if c.get("crustdata_company_id")]
            evidence.append(Evidence(reference="https://api.crustdata.com/company/search", provider="crustdata_company_search", mode="live",
                fact=f"candidate manufacturers for '{query}' in {region or 'India'}",
                value=json.dumps({"request": body, "companies": companies})[:24000]))
            if companies:
                break
        suppliers, quotes, by_domain = [], [], {}
        for company in companies:
            basic = company.get("basic_info") or {}
            canonical = str(company["crustdata_company_id"])
            sid = "crustdata:" + canonical
            domain = (basic.get("primary_domain") or "").lower().removeprefix("www.")
            name = basic.get("name") or domain or sid
            existing = next((s for s in suppliers if s.id == sid), None) or (by_domain.get(domain) if domain else None)
            if existing:  # Same company under two names stays one record.
                if name not in existing.aliases and name != existing.name:
                    existing.aliases.append(name)
                continue
            locations = company.get("locations") or {}
            supplier = Supplier(id=sid, canonical_id=canonical, name=name, aliases=[], domain=domain or "unknown",
                                locations=[x for x in [locations.get("headquarters")] if x], kind="unknown", identity="resolved")
            suppliers.append(supplier)
            if domain:
                by_domain[domain] = supplier
            for s in specs:
                quotes.append(Quote(id=f"{s.id}:{sid}", supplier_id=sid, spec_id=s.id, currency="INR", tiers={}, quantity=s.quantity or 1,
                    observed_at=now(), expires_at=now() + timedelta(hours=pack.freshness_hours), evidence_ids=[evidence[-1].id],
                    status="unverified", signals={k: (True if k == "identity" else None) for k in pack.trust_signals}))
        return suppliers, quotes, evidence


class LiveCompanyChecks:
    """One supplier's checks: Company Enrich plus two count-only Job Search calls."""
    def __init__(self, client, run_id):
        self.client, self.run_id = client, run_id

    def job_counts(self, company_id, pack):
        from app.providers.crustdata import CreditLimitReached, RateLimited
        from app.providers.interfaces import IntegrationUnavailable
        counts = {}
        for key, terms in (("production", pack.production_job_terms), ("sales", pack.sales_job_terms)):
            if not terms:
                continue
            filters = {"op": "and", "conditions": [
                {"field": "company.basic_info.company_id", "type": "=", "value": int(company_id)},
                {"op": "or", "conditions": [{"field": "job_details.title", "type": "(.)", "value": t} for t in terms]}]}
            try:
                counts[key] = self.client.job_count(self.run_id, filters, purpose=f"Count {key} job posts (factory vs trader)")
            except (CreditLimitReached, RateLimited):
                raise
            except IntegrationUnavailable:
                counts[key] = None  # Best effort: one failed signal leaves it unverified, not the whole check.
        return counts

    def check(self, supplier, request, specs, pack):
        from app.domain.supplier_signals import assess_company
        region = specs[0].parameters.get("location") if specs else None
        result = self.client.enrich(self.run_id, [supplier.canonical_id], pack.enrich_fields,
                                    purpose=f"Check {supplier.name} is real, stable and a factory")
        entry = result[0] if isinstance(result, list) and result else {}
        matches = entry.get("matches") or []
        data = (matches[0] or {}).get("company_data") if matches else None
        if not data:
            return None, [Evidence(reference="https://api.crustdata.com/company/enrich", provider="crustdata_company_enrich", mode="live",
                                   fact=f"company profile for {supplier.name}", value="No profile returned", status="unverified")]
        report = assess_company(data, region=region, relevance_keywords=pack.relevance_keywords, relevance_naics=pack.relevance_naics)
        counts = {}
        if report.signals["factory"] is None:
            # Only pay for job counts when the company record does not already settle factory vs trader.
            counts = self.job_counts(supplier.canonical_id, pack)
            report = assess_company(data, region=region, relevance_keywords=pack.relevance_keywords,
                                    relevance_naics=pack.relevance_naics, job_counts=counts)
        ref = f"https://api.crustdata.com/company/enrich#crustdata_company_id={supplier.canonical_id}"
        evidence = [
            Evidence(reference=ref, provider="crustdata_company_enrich", mode="live", fact="identity and status",
                     value=json.dumps({k: report.profile.get(k) for k in ("name", "domain", "founded", "status", "record_updated_at")})),
            Evidence(reference=ref, provider="crustdata_company_enrich", mode="live", fact="locations",
                     value=json.dumps({k: report.profile.get(k) for k in ("headquarters", "state", "country", "addresses")}),
                     status="verified" if report.region_match is not None else "unverified"),
            Evidence(reference=ref, provider="crustdata_company_enrich", mode="live", fact="factory or trader",
                     value=json.dumps({"verdict": report.signals["factory"], "for": report.profile["factory_for"], "against": report.profile["factory_against"],
                                       "naics": report.profile["naics"], "industry": report.profile["industry"]}),
                     status="verified" if report.signals["factory"] is not None else "unverified"),
            Evidence(reference=ref, provider="crustdata_company_enrich", mode="live", fact="age, size and growth",
                     value=json.dumps({"verdict": report.signals["stable"], "reasons": report.profile["stable_reasons"]}),
                     status="verified" if report.signals["stable"] is not None else "unverified"),
            Evidence(reference=ref, provider="crustdata_company_enrich", mode="live", fact="news and complaints",
                     value=json.dumps({"verdict": report.signals["complaints_clear"], "news": report.profile["news"]}),
                     status="verified" if report.signals["complaints_clear"] is not None else "unverified"),
        ]
        if any(v is not None for v in counts.values()):
            evidence.append(Evidence(reference="https://api.crustdata.com/job/search", provider="crustdata_job_search", mode="live",
                                     fact="job posts by type", value=json.dumps(counts)))
        return report, evidence
