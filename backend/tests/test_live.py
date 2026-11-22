from decimal import Decimal
import httpx
import pytest
from app.providers.crustdata import CrustdataClient
from app.providers.interfaces import IntegrationUnavailable
from app.providers.live_discovery import LiveDiscovery
from app.contracts import now, SourcingRequest, Spec
from datetime import timedelta


def client(tmp_path, handler, limit="10"):
    def dispatch(request):
        if request.url.path == "/account/endpoints":
            return httpx.Response(200, json={"endpoints":[
                {"path":"/web/search/live", "status":"enabled", "base_credits":1},
                {"path":"/company/identify", "status":"enabled", "base_credits":.2},
                {"path":"/web/enrich/live", "status":"enabled", "base_credits":1}]})
        return handler(request)
    return CrustdataClient(key="unit-test-placeholder", journal_path=tmp_path/"credits.db", credit_limit=limit,
        client=httpx.Client(base_url="https://api.crustdata.com", transport=httpx.MockTransport(dispatch)))


def test_live_credit_cap_and_persistent_response_cache(tmp_path):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200, headers={"x-credits-used":"1"}, json={"results":[]})
    first=client(tmp_path, handler, "1")
    first.search("run", "query")
    restarted=client(tmp_path, handler, "1")
    restarted.search("run", "query")
    assert len(calls)==1
    with pytest.raises(IntegrationUnavailable, match="CREDIT_LIMIT"):
        restarted.search("run", "new query")
    assert len(calls)==1
    assert Decimal(restarted.usage("run")[0]["charged"])==1


def test_ambiguous_paid_request_not_retried(tmp_path):
    calls=[]
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("simulated outcome unknown")
    provider=client(tmp_path,handler)
    with pytest.raises(IntegrationUnavailable, match="outcome unknown"):
        provider.search("run", "query")
    restarted=client(tmp_path,handler)
    with pytest.raises(IntegrationUnavailable, match="PRIOR_CALL_UNRESOLVED"):
        restarted.search("run", "query")
    assert len(calls)==1
    assert restarted.usage("run")[0]["state"]=="pending"


def test_live_discovery_preserves_unknown_purchasing_facts(tmp_path):
    def handler(req):
        if req.url.path=="/web/search/live":
            return httpx.Response(200,json={"results":[{"url":"https://supplier.example/item", "title":"ESP32-S3"}]})
        if req.url.path=="/company/identify":
            return httpx.Response(200,json=[{"matched_on":"supplier.example","matches":[{"confidence_score":1,"company_data":{
                "crustdata_company_id":123,"basic_info":{"name":"Supplier","primary_domain":"supplier.example"}}}]}])
        return httpx.Response(200,json=[{"url":"https://supplier.example/item","success":True,"content":"<h1>ESP32-S3</h1>","title":"Module"}])
    provider=client(tmp_path,handler)
    request=SourcingRequest(text="500 ESP32-S3 modules",deadline=now()+timedelta(days=21),mode="live")
    suppliers,quotes,evidence=LiveDiscovery(provider,"run").discover(request,[Spec(description="ESP32-S3",quantity=500)])
    assert suppliers[0].identity=="resolved" and suppliers[0].canonical_id=="123"
    assert suppliers[0].kind=="unknown"
    assert quotes[0].stock is None and quotes[0].shipping_minor is None and not quotes[0].tiers
    assert quotes[0].matches_spec is None and quotes[0].status=="unverified"
    assert all(e.mode=="live" for e in evidence)
    assert not any("fixture" in e.reference for e in evidence)


def test_failed_response_body_not_persisted_or_exposed(tmp_path):
    provider=client(tmp_path,lambda _:httpx.Response(401,json={"message":"sensitive-provider-response"}))
    with pytest.raises(IntegrationUnavailable) as error:
        provider.search("run","query")
    assert "sensitive" not in str(error.value)
    with provider.engine.connect() as c:
        assert c.exec_driver_sql("SELECT response FROM calls").scalar_one() is None


def test_minimal_profile_only_spends_point_two_on_identify(tmp_path):
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        assert req.url.path=="/company/identify"
        return httpx.Response(200, headers={"x-credits-used":"0.2"}, json=[{"matched_on":"mouser.com","matches":[{
            "company_data":{"crustdata_company_id":42,"basic_info":{"name":"Test distributor","primary_domain":"mouser.com"}}}]}])
    provider=client(tmp_path,handler,limit="0.2")
    req=SourcingRequest(text="500 ESP32-S3 modules",deadline=now()+timedelta(days=21),mode="live",supplier_domains=["mouser.com"])
    suppliers,quotes,evidence=LiveDiscovery(provider,"run").discover(req,[Spec(description="ESP32-S3",quantity=500)])
    assert calls==["/company/identify"]
    assert suppliers[0].identity=="resolved"
    assert len(evidence)==1 and evidence[0].mode=="live"
    assert sum(Decimal(c["charged"]) for c in provider.usage("run"))==Decimal("0.2")
    with pytest.raises(IntegrationUnavailable,match="CREDIT_LIMIT"):
        provider.search("run","should be blocked")
    with pytest.raises(IntegrationUnavailable,match="CREDIT_LIMIT"):
        provider.fetch("run",["https://mouser.com"])
    assert calls==["/company/identify"]


def test_search_engine_redirects_are_not_supplier_domains():
    from app.providers.live_discovery import supplier_url
    assert supplier_url("https://www.google.com/goto?url=opaque-token") is None
    assert supplier_url("https://www.google.com/url?q=https%3A%2F%2Fmouser.com%2Fproduct") == "https://mouser.com/product"
    assert supplier_url("https://www.google.com/search?q=ESP32") is None
