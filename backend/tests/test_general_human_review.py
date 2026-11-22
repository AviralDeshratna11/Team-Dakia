from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from app.config import pack_for
from app.contracts import SourcingRequest, WorkflowRun, RunState as S, Spec, now
from app.contracts.llm import ParsedIntent
from app.domain.decision_engine import assess
from app.main import create_app
from app.persistence.database import Database
from app.persistence.repositories import save_run,get_run
from app.providers.interfaces import IntegrationUnavailable
from app.providers.fixtures.discovery import FixtureDiscovery
from app.workflow.orchestrator import Orchestrator,ACTIVE


@pytest.fixture
def system(tmp_path):
    db=Database(str(tmp_path/'workflow.db'))
    worker=Orchestrator(db)
    with TestClient(create_app(db)) as api:
        yield db,worker,api


def payload(**changes):
    return SourcingRequest.model_validate({'text':'Source 100 reusable cotton tote bags, 38 x 42 cm',
        'deadline':now()+timedelta(days=21),'specs':[{'id':'bags','description':'Cotton tote bags','quantity':100,'parameters':{'material':'cotton','dimensions':'38 x 42 cm'}}],**changes}).model_dump(mode='json')


def drive(api,worker,rid,until=None):
    for _ in range(60):
        run=api.get('/api/runs/'+rid).json()
        if until and until(run): return run
        if run['state'] not in ACTIVE: return run
        worker.tick(rid)
    pytest.fail('workflow did not settle')


def start_blocked(system):
    db,worker,api=system
    rid=api.post('/api/runs',json=payload(category='packaging')).json()['id']
    run=drive(api,worker,rid)
    assert run['state']=='WAITING_FOR_HUMAN'
    return rid,run,run['human_reviews'][0]


@pytest.mark.parametrize('category',['office furniture','coffee','packaging','cleaning services','industrial machinery','../../not-a-path'])
def test_any_context_uses_conservative_general_policy(category):
    request=SourcingRequest.model_validate(payload(category=category))
    assert request.human_review_enabled
    assert pack_for(category).name=='general'
    assert not pack_for(category).cost_policy_confirmed
    assert ParsedIntent(summary='Request',category=category,items=[]).category==category


def test_purchase_and_shortlist_are_independent_of_category():
    assert SourcingRequest.model_validate(payload(category='leather_goods',request_type='purchase')).request_type=='purchase'
    assert SourcingRequest.model_validate(payload(category='furniture',request_type='shortlist')).request_type=='shortlist'


def test_unsourced_items_create_one_durable_review_and_pause(system):
    db,worker,api=system
    rid,run,task=start_blocked(system)
    assert task['reason_code']=='NO_PURCHASABLE_PLAN' and task['spec_ids']==['bags']
    assert not run['suppliers'] and not run['orders']
    before=len(run['events'])
    for _ in range(3): worker.tick(rid)
    saved=api.get('/api/runs/'+rid).json()
    assert len(saved['events'])==before and len(saved['human_reviews'])==1
    assert api.get('/api/human-reviews').json()[0]['id']==task['id']
    assert api.post('/api/runs/'+rid+'/resume').status_code==409


def test_manual_handling_requires_reference_and_does_not_create_payment(system):
    db,worker,api=system
    rid,run,task=start_blocked(system)
    url=f'/api/runs/{rid}/human-reviews/{task["id"]}/resolve'
    body={'reviewer':'Procurement lead','action':'manual_completion','note':'Verified requirements and handled with supplier outside the application.'}
    assert api.post(url,json=body).status_code==409
    body['reference']='PO-MANUAL-42'
    assert api.post(url,json=body).status_code==200
    finished=drive(api,worker,rid)
    assert finished['state']=='COMPLETED'
    assert finished['decisions'][0]['outcome']=='handled_by_human'
    assert not finished['orders'] and worker.payments.count()==0
    assert finished['human_completed_spec_ids']==['bags']
    facts=[e for e in finished['evidence'] if e['provider']=='human_review']
    assert len(facts)==1 and facts[0]['status']=='unverified'
    assert api.post(url,json=body).status_code==200
    assert api.post(url,json=dict(body,reference='different-reference')).status_code==409
    assert len([e for e in api.get('/api/runs/'+rid).json()['evidence'] if e['provider']=='human_review'])==1
    assert api.get('/api/human-reviews').json()==[]


def test_reviewer_can_revise_and_retry_without_resetting_the_run(system):
    db,worker,api=system
    rid,run,task=start_blocked(system)
    body={'reviewer':'Founder','action':'retry','note':'Approved a larger provider budget and specified the supplier.',
        'supplier_domains':['supplier.example'],'provider_credit_limit':'1.2'}
    response=api.post(f'/api/runs/{rid}/human-reviews/{task["id"]}/resolve',json=body)
    assert response.status_code==200
    retried=response.json()
    assert retried['id']==rid and retried['state']=='DISCOVERING'
    assert retried['request']['provider_credit_limit']=='1.2'
    assert retried['human_reviews'][0]['status']=='resolved'
    # The demo has no catalogue for this product: a fresh task replaces, rather than
    # duplicates, the resolved review when the retry still cannot qualify a supplier.
    paused=drive(api,worker,rid)
    assert paused['state']=='WAITING_FOR_HUMAN'
    assert len(paused['human_reviews'])==2
    assert len([t for t in paused['human_reviews'] if t['status']=='pending'])==1


def test_reviewer_can_cancel_remaining_work(system):
    db,worker,api=system
    rid,run,task=start_blocked(system)
    response=api.post(f'/api/runs/{rid}/human-reviews/{task["id"]}/resolve',json={'reviewer':'Founder','action':'cancel','note':'No acceptable supplier; cancel this request.'})
    assert response.status_code==200 and response.json()['state']=='CANCELLED'
    worker.tick(rid)
    assert api.get('/api/runs/'+rid).json()['state']=='CANCELLED'
    assert worker.payments.count()==0


def test_provider_failure_routes_to_human_instead_of_silently_finishing(system):
    db,_,api=system
    class UnavailableProvider:
        def set_run_limit(self,*_): pass
        def search(self,*_): raise IntegrationUnavailable('CRUSTDATA_CREDIT_LIMIT: paid call blocked')
        def usage(self,*_): return []
    worker=Orchestrator(db,live_client=UnavailableProvider())
    rid=api.post('/api/runs',json=payload(mode='live')).json()['id']
    paused=drive(api,worker,rid)
    assert paused['state']=='WAITING_FOR_HUMAN'
    task=paused['human_reviews'][0]
    assert task['reason_code']=='PROVIDER_UNAVAILABLE' and task['resume_state']=='DISCOVERING'
    assert 'CREDIT_LIMIT' in task['blockers'][0]


def test_user_can_request_a_human_during_clarification(system):
    db,worker,api=system
    rid=api.post('/api/runs',json=payload(text='Find suitable office furniture',specs=[])).json()['id']
    assert drive(api,worker,rid)['state']=='WAITING_FOR_CLARIFICATION'
    response=api.post('/api/runs/'+rid+'/human-review',json={'note':'Please have a procurement reviewer scope this request.'})
    assert response.status_code==200 and response.json()['state']=='WAITING_FOR_HUMAN'
    assert api.post('/api/runs/'+rid+'/human-review',json={'note':'Need assistance again.'}).json()['human_reviews']==response.json()['human_reviews']


def test_unconfirmed_general_tax_policy_cannot_authorize_purchase():
    req=SourcingRequest.model_validate(payload(category='electronics',specs=[{'id':'part','description':'ESP32-S3','quantity':500,'parameters':{'fixture_item':'esp32','part_number':'ESP32-S3'}}]))
    suppliers,quotes,_=FixtureDiscovery().discover(req,req.specs)
    candidate=assess(quotes[0],suppliers[0],req,pack_for('arbitrary product'))
    assert not candidate.eligible and candidate.cost is None
    assert 'COST_POLICY_UNCONFIRMED' in candidate.reason_codes


def test_pending_payment_must_reconcile_before_manual_handling(system):
    db,worker,api=system
    req=payload(category='electronics',approval_threshold_minor=100000000,specs=[{'id':'part','description':'ESP32-S3','quantity':500,'parameters':{'fixture_item':'esp32','part_number':'ESP32-S3'}}])
    rid=api.post('/api/runs',json=req).json()['id']
    pending=drive(api,worker,rid,until=lambda r:any(o['state']=='PAY_PENDING' for o in r['orders']))
    order=pending['orders'][0]
    worker.payments.pay(order['idempotency_key'],order['amount_minor'],'orders')
    paused=api.post('/api/runs/'+rid+'/human-review',json={'note':'Checkout confirmation needs reconciliation.'}).json()
    task=paused['human_reviews'][0]
    url=f'/api/runs/{rid}/human-reviews/{task["id"]}/resolve'
    assert api.post(url,json={'reviewer':'Founder','action':'manual_completion','note':'Attempt manual handling before reconciliation.','reference':'PO-unsafe'}).status_code==409
    assert api.post(url,json={'reviewer':'Founder','action':'retry','note':'Reconcile the saved checkout without changing the order.'}).status_code==200
    complete=drive(api,worker,rid)
    assert complete['state']=='COMPLETED' and worker.payments.count()==1
    assert complete['orders'][0]['idempotency_key']==order['idempotency_key']


def test_gemini_context_label_does_not_restrict_generic_requests(tmp_path):
    from app.services.llm_extraction import LLMExtractionService
    from test_llm import fake_client,response,intent
    data=intent()
    data['category']='reusable textiles'
    data['summary']='Source cotton tote bags'
    data['items'][0]['description']='Cotton tote bags'
    data['items'][0]['part_number']=None
    data['items'][0]['parameters']=[{'name':'material','value':'cotton'}]
    run=WorkflowRun(request=SourcingRequest(text='Cotton tote bags, 500 of each',deadline=now()+timedelta(days=21),llm_enabled=True))
    specs,question=LLMExtractionService(fake_client(tmp_path,lambda _:response(data))).parse(run)
    assert question is None and specs[0].description=='Cotton tote bags'
    assert specs[0].quantity==500 and specs[0].parameters['material']=='cotton'
