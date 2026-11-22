import json
from datetime import timedelta
import httpx
import pytest
from fastapi.testclient import TestClient
from app.contracts import SourcingRequest, WorkflowRun, Spec, Evidence, now, RunState as S
from app.contracts.llm import ParsedIntent, SupplierExtraction
from app.providers.gemini import GeminiClient
from app.providers.interfaces import IntegrationUnavailable
from app.services.llm_extraction import LLMExtractionService
from app.providers.fixtures.discovery import FixtureDiscovery
from app.persistence.database import Database
from app.main import create_app
from app.workflow.orchestrator import Orchestrator, ACTIVE


def intent(quantity=500, excerpt="500 of each", question=None):
    return {"summary":"Source ESP32-S3 modules", "category":"electronics", "request_type":"purchase", "destination":"Bengaluru",
        "delivery_requirement":None,"research_budget_requirement":None,"clarification_question":question,
        "items":[{"description":"ESP32-S3 module","part_number":"ESP32-S3","parameters":[{"name":"part_number","value":"ESP32-S3"}],
            "quantity":quantity,"quantity_excerpt":excerpt,"unit":"piece","substitutes":[],"missing_fields":[]}]}


def fake_client(tmp_path, handler, max_calls=4):
    return GeminiClient(key="test-placeholder",model="gemini-3.1-flash-lite",journal_path=tmp_path/"llm.db",max_calls=max_calls,
        client=httpx.Client(base_url="https://generativelanguage.googleapis.com",transport=httpx.MockTransport(handler)))


def response(value, finish="STOP"):
    return httpx.Response(200,json={"candidates":[{"finishReason":finish,"content":{"parts":[{"text":json.dumps(value)}]}}],
        "usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":80,"totalTokenCount":180}})


def run_request(text="ESP32-S3 modules, 500 of each", **overrides):
    return WorkflowRun(request=SourcingRequest.model_validate({"text":text,"deadline":now()+timedelta(days=21),"llm_enabled":True,**overrides}))


def test_gemini_structured_request_cache_and_call_limit(tmp_path):
    calls=[]
    def handler(req):
        payload=json.loads(req.content)
        assert payload["generationConfig"]["responseMimeType"]=="application/json"
        assert "responseJsonSchema" in payload["generationConfig"]
        assert "tools" not in payload
        assert "test-placeholder" not in req.url.query.decode()
        calls.append(req)
        return response(intent())
    client=fake_client(tmp_path,handler,max_calls=1)
    one=client.generate("run","parse_request","extract only",{"request":"500 of each"},ParsedIntent)
    restarted=fake_client(tmp_path,handler,max_calls=1)
    assert restarted.generate("run","parse_request","extract only",{"request":"500 of each"},ParsedIntent)==one
    with pytest.raises(IntegrationUnavailable,match="CALL_LIMIT"):
        restarted.generate("run","parse_request","extract only",{"request":"new"},ParsedIntent)
    assert len(calls)==1
    assert restarted.usage("run")[0]["total_tokens"]==180


def test_quantity_requires_verbatim_request_support(tmp_path):
    service=LLMExtractionService(fake_client(tmp_path,lambda _:response(intent(excerpt="500 of each"))))
    run=run_request("ESP32-S3 modules for 500 builds")
    specs,question=service.parse(run)
    assert specs[0].quantity is None and question
    assert run.request.research_budget_minor==200000


@pytest.mark.parametrize("text,quantity,excerpt",[
    ("500 builds need ESP32-S3",500,"500 builds"),
    ("ESP32-S3 modules, 500 of each",999,"500 of each")
])
def test_build_counts_and_changed_numeric_quantities_require_clarification(tmp_path,text,quantity,excerpt):
    service=LLMExtractionService(fake_client(tmp_path,lambda _:response(intent(quantity,excerpt))))
    specs,question=service.parse(run_request(text))
    assert specs[0].quantity is None and question


def test_explicit_specs_and_budget_not_overwritten(tmp_path):
    payload=intent(quantity=999,excerpt="500 of each")
    service=LLMExtractionService(fake_client(tmp_path,lambda _:response(payload)))
    run=run_request(specs=[Spec(id="given",description="Precise user part",quantity=2)], research_budget_minor=100)
    specs,_=service.parse(run)
    assert specs[0].quantity==2 and specs[0].description=="Precise user part"
    assert run.request.research_budget_minor==100


def populated_run():
    run=run_request(mode="live")
    run.specs=[Spec(id="part",description="ESP32-S3",quantity=500)]
    run.suppliers,run.quotes,run.evidence=FixtureDiscovery().discover(run.request,run.specs)
    for quote in run.quotes:
        quote.status="unverified"
        quote.stock=None
    page=Evidence(id="page",reference="https://supplier.example/item",provider="crustdata_web_fetch",mode="live",
        fact="page",value=json.dumps({"content":"ESP32-S3: stock 1500 units. Unit price INR 12.50. We are a distributor."}))
    run.evidence.append(page)
    next(q for q in run.quotes if q.supplier_id=="bright").evidence_ids.append(page.id)
    return run


def insight(facts, evidence_ids=None):
    return {"insights":[{"supplier_id":"bright","summary":"Distributor listing with stated stock; delivery unknown.",
        "evidence_ids":evidence_ids or ["page"],"facts":facts,"missing_information":["Delivery date", "Authorization"]}]}


def fact(field="stock",value="1500",source="page",excerpt="stock 1500 units",spec="part"):
    return {"spec_id":spec,"field":field,"value":value,"evidence_id":source,"excerpt":excerpt}


def test_supported_facts_populate_live_quote_but_do_not_verify_or_change_identity(tmp_path):
    run=populated_run()
    identity=run.suppliers[0].canonical_id
    service=LLMExtractionService(fake_client(tmp_path,lambda _:response(insight([fact()]))))
    service.extract(run)
    quote=next(q for q in run.quotes if q.supplier_id=="bright")
    assert quote.stock==1500 and quote.status=="unverified"
    assert run.suppliers[0].canonical_id==identity
    derived=next(e for e in run.evidence if e.provider=="gemini_extraction")
    assert derived.status=="unverified" and '"source_evidence_id": "page"' in derived.value
    assert run.supplier_insights[0]["missing_information"]


@pytest.mark.parametrize("bad_fact",[
    fact(source="invented"), fact(excerpt="Unlimited stock available"), fact(spec="wrong-item"),
    fact(value="not a number"),fact(spec=None)
    ,fact(value="9999")
])
def test_unsupported_facts_do_not_change_quote(tmp_path,bad_fact):
    run=populated_run()
    service=LLMExtractionService(fake_client(tmp_path,lambda _:response(insight([bad_fact]))))
    service.extract(run)
    assert next(q for q in run.quotes if q.supplier_id=="bright").stock is None
    assert run.extraction_warnings


def test_purchasing_facts_cannot_come_from_identify_or_search(tmp_path):
    run=populated_run()
    source=next(e for e in run.evidence if e.id=="page")
    source.provider="crustdata_company_identify"
    service=LLMExtractionService(fake_client(tmp_path,lambda _:response(insight([fact()]))))
    service.extract(run)
    assert next(q for q in run.quotes if q.supplier_id=="bright").stock is None
    assert run.extraction_warnings


def test_conflicting_facts_are_discarded(tmp_path):
    run=populated_run()
    service=LLMExtractionService(fake_client(tmp_path,lambda _:response(insight([fact(),fact(value="100")]))))
    service.extract(run)
    assert next(q for q in run.quotes if q.supplier_id=="bright").stock is None


def test_failed_or_truncated_model_output_is_not_applied_or_retried(tmp_path):
    calls=[]
    def handler(req):
        calls.append(req)
        return response(intent(),finish="MAX_TOKENS")
    client=fake_client(tmp_path,handler)
    with pytest.raises(IntegrationUnavailable,match="INVALID_OR_BLOCKED"):
        client.generate("run","parse","policy",{},ParsedIntent)
    with pytest.raises(IntegrationUnavailable,match="PRIOR_CALL_UNRESOLVED"):
        client.generate("run","parse","policy",{},ParsedIntent)
    assert len(calls)==1


def test_network_failure_is_sanitized_and_not_retried(tmp_path):
    def handler(req): raise httpx.ReadTimeout("SECRET_TOKEN raw provider text")
    client=fake_client(tmp_path,handler)
    with pytest.raises(IntegrationUnavailable) as exc:
        client.generate("run","parse","policy",{},ParsedIntent)
    assert "SECRET" not in str(exc.value)
    assert client.usage("run")[0]["state"]=="pending"


def test_two_llm_stages_through_api_and_saved_worker_flow(tmp_path):
    stages=[]
    def handler(req):
        payload=json.loads(req.content)
        data=json.loads(payload["contents"][0]["parts"][0]["text"])
        if "request_text" in data:
            stages.append("parse")
            value=intent()
            value["items"][0]["parameters"].append({"name":"fixture_item","value":"esp32"})
            return response(value)
        stages.append("extract")
        supplier=data["suppliers"][0]
        evidence=data["quotes"][0]["evidence_ids"][0]
        return response({"insights":[{"supplier_id":supplier["id"],"summary":"Synthetic example supplier", "evidence_ids":[evidence],"facts":[],"missing_information":[]}]})
    database=Database(str(tmp_path/"workflow.db"))
    client=fake_client(tmp_path,handler)
    worker=Orchestrator(database,llm_client=client)
    with TestClient(create_app(database)) as api:
        payload=run_request(approval_threshold_minor=100000000).request.model_dump(mode="json")
        rid=api.post("/api/runs",json=payload).json()["id"]
        for _ in range(30):
            run=api.get(f"/api/runs/{rid}").json()
            if run["state"] not in ACTIVE: break
            worker.tick(rid)
        run=api.get(f"/api/runs/{rid}").json()
        assert run["state"]=="COMPLETED"
        assert run["request_interpretation"]["summary"]
        assert run["supplier_insights"] and len(run["llm_usage"])==2
        assert stages==["parse","extract"]
        worker.tick(rid)
        assert len(stages)==2


def test_missing_gemini_key_is_explicit_and_does_not_break_fixture_mode(tmp_path,monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY",raising=False)
    db=Database(str(tmp_path/"workflow.db"))
    worker=Orchestrator(db)
    with TestClient(create_app(db)) as api:
        rid=api.post("/api/runs",json=run_request().request.model_dump(mode="json")).json()["id"]
        worker.tick(rid)
        worker.tick(rid)
        run=api.get(f"/api/runs/{rid}").json()
        assert run["state"]=="FAILED" and "GEMINI_NOT_CONFIGURED" in run["error"]
