from fastapi.testclient import TestClient
from app.contracts import RunState as S
from app.contracts.llm import ParsedIntent
from app.main import create_app
from app.persistence.database import Database
from app.persistence.repositories import save_run
from app.providers.gemini import GeminiOutputError
from app.services.llm_extraction import LLMExtractionService
from app.workflow.orchestrator import Orchestrator
from app.providers.fixtures.discovery import FixtureDiscovery
import httpx
from test_llm import fake_client,response,intent,run_request


def test_null_optional_lists_and_numeric_parameter_values_are_normalized(tmp_path):
    data=intent()
    item=data['items'][0]
    item['parameters']=[{'name':'voltage','value':3.3}]
    item['substitutes']=None
    item['missing_fields']=None
    client=fake_client(tmp_path,lambda _:response(data))
    result=client.generate('run','parse_request','policy',{},ParsedIntent)
    assert result.items[0].parameters[0].value=='3.3'
    assert result.items[0].substitutes==[] and result.items[0].missing_fields==[]


def test_schema_error_has_safe_field_diagnostics_not_model_values(tmp_path):
    data=intent()
    data['items'][0]['quantity']=-99
    client=fake_client(tmp_path,lambda _:response(data))
    try:
        client.generate('run','parse_request','policy',{},ParsedIntent)
        assert False,'invalid quantity must be rejected'
    except GeminiOutputError as error:
        assert error.code=='GEMINI_SCHEMA_INVALID'
        assert 'items.0.quantity' in str(error) and '-99' not in str(error)
    usage=client.usage('run')[0]
    assert usage['finish_reason']=='STOP' and 'quantity' in usage['diagnostic']


def test_specific_clarification_keeps_existing_quantities(tmp_path):
    data=intent()
    data['items'][0]['missing_fields']=['specifications']
    data['clarification_question']='specifications'
    run=run_request()
    specs,question=LLMExtractionService(fake_client(tmp_path,lambda _:response(data))).parse(run)
    assert specs[0].quantity==500
    assert 'ESP32-S3 module' in question and 'exact part numbers' in question
    assert question!='specifications'


def test_failed_run_restores_last_valid_intent_without_a_model_call(tmp_path):
    db=Database(str(tmp_path/'workflow.db'))
    run=run_request('ESP32-S3 modules\nClarification: 500 OF EACH')
    run.state=S.FAILED
    run.error='GEMINI_INVALID_OR_BLOCKED_OUTPUT: no extraction applied'
    data=intent(excerpt='500 OF EACH')
    data['items'][0]['missing_fields']=['specifications']
    run.request_interpretation=data
    run.llm_usage=[{'stage':'parse_request','state':'failed'}]
    with db.transaction() as c: save_run(c,run)
    with TestClient(create_app(db)) as api:
        restored=api.post(f'/api/runs/{run.id}/resume')
        assert restored.status_code==200
        value=restored.json()
        assert value['state']=='WAITING_FOR_CLARIFICATION' and value['error'] is None
        assert value['specs'][0]['quantity']==500
        duplicate=api.post(f'/api/runs/{run.id}/clarifications',json={'answer':'500 of each'})
        assert duplicate.status_code==409 and 'already saved' in duplicate.json()['error']['message']
        assert api.get(f'/api/runs/{run.id}').json()['state']=='WAITING_FOR_CLARIFICATION'
        assert len(api.get(f'/api/runs/{run.id}').json()['llm_usage'])==1


def test_malformed_parse_returns_to_clarification_instead_of_failing(tmp_path):
    db=Database(str(tmp_path/'workflow.db'))
    data=intent()
    data['items'][0]['quantity']=-2
    run=run_request()
    run.state=S.PARSING
    run.request_interpretation=intent()
    client=fake_client(tmp_path,lambda _:response(data))
    with db.transaction() as c: save_run(c,run)
    saved=Orchestrator(db,llm_client=client).tick(run.id)
    assert saved.state==S.WAITING_FOR_CLARIFICATION and saved.error is None
    assert saved.specs[0].quantity==500
    assert len(saved.llm_usage)==1 and saved.llm_usage[0]['state']=='failed'


def test_structured_clarification_reuses_valid_parse_without_new_generation(tmp_path):
    db=Database(str(tmp_path/'workflow.db'))
    run=run_request(specs=[{'id':'part','description':'ESP32-S3','quantity':500}])
    run.state=S.PARSING
    run.request_interpretation=intent()
    with db.transaction() as c: save_run(c,run)
    class NoCalls:
        def generate(self,*args): raise AssertionError('must not spend on an already structured clarification')
        def usage(self,*args): return []
    saved=Orchestrator(db,llm_client=NoCalls()).tick(run.id)
    assert saved.state==S.DISCOVERING and saved.specs[0].quantity==500


def test_safety_block_still_fails_and_is_not_bypassed_by_cached_intent(tmp_path):
    db=Database(str(tmp_path/'workflow.db'))
    run=run_request()
    run.state=S.PARSING
    run.request_interpretation=intent()
    client=fake_client(tmp_path,lambda _:httpx.Response(200,json={'promptFeedback':{'blockReason':'SAFETY'}}))
    with db.transaction() as c: save_run(c,run)
    saved=Orchestrator(db,llm_client=client).tick(run.id)
    assert saved.state==S.FAILED and 'GEMINI_OUTPUT_BLOCKED' in saved.error


def test_insight_call_limit_preserves_evidence_and_does_not_regenerate(tmp_path):
    db=Database(str(tmp_path/'workflow.db'))
    calls=[]
    client=fake_client(tmp_path,lambda req:(calls.append(req) or response(intent())),max_calls=1)
    run=run_request()
    run.specs,_=LLMExtractionService(client).parse(run)
    run.suppliers,run.quotes,run.evidence=FixtureDiscovery().discover(run.request,run.specs)
    run.state=S.VERIFYING
    original=[e.id for e in run.evidence]
    with db.transaction() as c: save_run(c,run)
    saved=Orchestrator(db,llm_client=client).tick(run.id)
    assert saved.state==S.RESEARCHING and saved.error is None
    assert [e.id for e in saved.evidence]==original
    assert 'GEMINI_CALL_LIMIT' in saved.extraction_warnings[0]
    assert len(calls)==1
