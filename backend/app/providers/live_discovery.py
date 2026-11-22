"""Live supplier discovery preserves unknown purchasing facts; no fixture fallback."""
from datetime import timedelta
import json
from urllib.parse import urlparse, parse_qs
from app.contracts import Supplier, Quote, Evidence, now
from app.config import pack_for


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
        query = (" ".join(s.description for s in specs) + " India buy distributor official catalogue" if request.category == "electronics" else
                 "leather goods manufacturers Uttar Pradesh factory official website")
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
        identified = self.client.identify(self.run_id, domains)
        fetched = [] if request.supplier_domains else self.client.fetch(self.run_id, pages[:2])
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
