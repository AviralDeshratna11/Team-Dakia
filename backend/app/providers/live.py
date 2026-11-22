from app.providers.interfaces import IntegrationUnavailable


class LiveBoundary:
    """No guessed URLs, headers, payloads, prices, or free/paid classifications."""
    def unavailable(self, capability: str):
        raise IntegrationUnavailable(f"INTEGRATION_UNAVAILABLE: {capability}; live adapter requires verified official contracts, credentials and a credit authorization policy")

    def identify(self, name_or_domain): return self.unavailable("Company Identify")
    def enrich(self, canonical_id): return self.unavailable("Company Enrichment")
    def company_search(self, filters): return self.unavailable("Company Search")
    def job_search(self, canonical_id): return self.unavailable("Job Search")
    def person_search(self, canonical_id): return self.unavailable("Person Search")
    def credits(self): return self.unavailable("Credit balance")
    def search(self, query): return self.unavailable("Web Search")
    def fetch(self, url): return self.unavailable("Web Fetch")
    def parse(self, request): return self.unavailable("LLM structured specification extraction")
    def discover(self, request, specs): return self.unavailable("Supplier discovery")
