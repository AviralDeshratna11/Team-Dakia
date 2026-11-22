import hashlib
import json
from app.config import pack_for
from app.contracts import (RunState as S, WorkflowRun, WorkflowEvent, Clarification, SupplierCheck,
                           ResearchPurchase, Evidence, Order, RFQDraft, Approval, now)
from app.domain.state_machines import transition, order_transition
from app.domain.decision_engine import decide, assess, landed_cost
from app.domain.data_value import worth_buying
from app.persistence.repositories import get_run, save_run, reserve, can_commit, record_spend
from app.providers.fixtures.discovery import FixtureDiscovery
from app.providers.live import LiveBoundary
from app.providers.crustdata import CrustdataClient
from app.providers.live_discovery import LiveDiscovery
from app.providers.interfaces import IntegrationUnavailable
from app.providers.mock_checkout import MockPayments
from app.services.specification_parser import FixtureParser
from app.providers.gemini import GeminiClient, GeminiOutputError
from app.services.llm_extraction import LLMExtractionService
from app.services.human_review import open_review

ACTIVE = {S.CREATED, S.PARSING, S.DISCOVERING, S.VERIFYING, S.RESEARCHING, S.DECIDING, S.EXECUTING}


def fingerprint(run):
    payload = {"specs": [s.model_dump(mode="json") for s in run.specs],
               "quotes": [q.model_dump(mode="json") for q in run.quotes],
               "suppliers": [s.model_dump(mode="json") for s in run.suppliers],
               "pack": pack_for(run.request.category).model_dump(mode="json"),
               "destination": run.request.destination, "deadline": run.request.deadline.isoformat(),
               "threshold": run.request.approval_threshold_minor}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


class Orchestrator:
    def __init__(self, database, payments=None, live_client=None, llm_client=None):
        self.db = database
        self.payments = payments or MockPayments(database.path + ".payments.db")
        self.live_client = live_client
        self.llm_client = llm_client

    def llm(self):
        if self.llm_client is None:
            self.llm_client = GeminiClient(journal_path=self.db.path + ".llm.db")
        return LLMExtractionService(self.llm_client)

    def tick(self, run_id):
        with self.db.transaction() as c:
            run = get_run(c, run_id)
            if run.state not in ACTIVE:
                return run
            try:
                self.advance(c, run)
            except GeminiOutputError as exc:
                if run.state == S.PARSING and exc.code in {"GEMINI_SCHEMA_INVALID","GEMINI_OUTPUT_TRUNCATED"}:
                    run.specs, question = LLMExtractionService.restore_intent(run)
                    run.clarification = Clarification(question=question or "Please confirm the item specifications using the structured editor; the latest model response could not be validated.")
                    run.error = None
                    transition(run,S.WAITING_FOR_CLARIFICATION,"Gemini output was invalid. Preserved the last valid requirements; no automatic paid retry. "+str(exc))
                    if run.request.human_review_enabled:
                        open_review(run,"MODEL_RESPONSE_INVALID","The model response could not be validated. A reviewer can supply structured requirements.",[str(exc)],resume_state=S.PARSING)
                else:
                    run.error = str(exc)
                    if run.request.human_review_enabled:
                        open_review(run,"MODEL_RESPONSE_BLOCKED",run.error,[run.error])
                    else: transition(run,S.FAILED,run.error)
            except IntegrationUnavailable as exc:
                run.error = str(exc)
                if run.request.human_review_enabled:
                    open_review(run,"PROVIDER_UNAVAILABLE","Sourcing could not continue within the available provider access or budget.",[run.error])
                else: transition(run, S.FAILED, run.error)
            except Exception as exc:
                if not run.request.human_review_enabled: raise
                run.error = "SOURCING_OPERATION_FAILED: "+type(exc).__name__
                open_review(run,"EXECUTION_FAILED","An operation failed; a reviewer must inspect the saved state before continuing.",[run.error])
            if run.request.mode == "live" and self.live_client is not None:
                run.provider_usage = self.live_client.usage(run.id)
            if run.request.llm_enabled and self.llm_client is not None:
                run.llm_usage = self.llm_client.usage(run.id)
            save_run(c, run)
            return run

    def advance(self, c, run):
        pack = pack_for(run.request.category)
        if run.state == S.CREATED:
            transition(run, S.PARSING, "Gemini request parsing enabled." if run.request.llm_enabled else "Reading the request with the bounded reference/structured parser; Gemini disabled for this run.")
        elif run.state == S.PARSING:
            if run.request.llm_enabled and not (run.request.specs and run.request_interpretation):
                run.specs, question = self.llm().parse(run)
            else:
                run.specs, question = FixtureParser().parse(run.request)
            if question:
                run.clarification = Clarification(question=question)
                transition(run, S.WAITING_FOR_CLARIFICATION, question)
            else:
                transition(run, S.DISCOVERING, f"Specifications saved; finding {run.request.mode} suppliers.")
        elif run.state == S.DISCOVERING:
            if run.request.mode == "live":
                if self.live_client is None:
                    self.live_client = CrustdataClient(journal_path=self.db.path + ".credits.db")
                self.live_client.set_run_limit(run.id,run.request.provider_credit_limit)
                provider = LiveDiscovery(self.live_client, run.id)
            else:
                provider = FixtureDiscovery()
            # Simulated bounded provider retries occur on separate persisted ticks.
            attempts = run.stage_attempts.get("discovery", 0) + 1
            run.stage_attempts["discovery"] = attempts
            if run.request.mode == "fixture" and run.request.scenario == "provider_timeout" and attempts < 3:
                run.events.append(WorkflowEvent(state=run.state, message=f"Simulated provider timeout / 429 attempt {attempts}/3; retry on next worker poll."))
                return
            run.suppliers, run.quotes, run.evidence = provider.discover(run.request, run.specs)
            transition(run, S.VERIFYING, "Canonical company IDs merged; source timestamps saved." + (" Provider retries exhausted; quote facts remain unverified." if attempts == 3 else ""))
        elif run.state == S.VERIFYING:
            if run.request.llm_enabled:
                try:
                    self.llm().extract(run)
                    run.events.append(WorkflowEvent(state=run.state, message="Gemini provider evidence extraction saved with cited claims and explicit gaps. LLM claims do not authorize purchases."))
                except (GeminiOutputError,IntegrationUnavailable) as exc:
                    recoverable = (isinstance(exc,GeminiOutputError) and exc.code != "GEMINI_OUTPUT_BLOCKED") or "GEMINI_CALL_LIMIT" in str(exc)
                    if not recoverable: raise
                    run.extraction_warnings.append("Supplier insight generation unavailable: "+str(exc)+". Raw evidence retained; no model claims applied or automatic paid retry.")
                    run.events.append(WorkflowEvent(state=run.state,message=run.extraction_warnings[-1]))
            run.checks = []
            for supplier in run.suppliers:
                quote = next(q for q in run.quotes if q.supplier_id == supplier.id)
                run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="identity", result="pass" if supplier.identity == "resolved" else "unverified",
                    reason=f"{run.request.mode} canonical identity resolved." if supplier.identity == "resolved" else "No matching company evidence; this does not prove fraud.", evidence_ids=quote.evidence_ids))
                trust = next((signal for signal in ["authorized","factory"] if signal in pack.trust_signals),"supplier_type")
                value = quote.signals.get(trust) if trust != "supplier_type" else supplier.kind in pack.allowed_kinds
                run.checks.append(SupplierCheck(supplier_id=supplier.id, kind=trust, result="pass" if value is True else "fail" if value is False else "unverified",
                    reason="Synthetic combined trust evidence; hiring alone is not conclusive." if run.request.mode == "fixture" else "Category trust signal not yet verified by live evidence; identity alone is insufficient.", evidence_ids=quote.evidence_ids))
            transition(run, S.RESEARCHING, "Supplier checks saved. Evaluating decision-changing gaps.")
        elif run.state == S.RESEARCHING:
            if self.research_step(c, run, pack):
                return
            transition(run, S.DECIDING, "Research complete or budget unavailable; uncertainty retained.")
        elif run.state == S.DECIDING:
            self.build_plan(run, pack)
            blocked = [d for d in run.decisions if d.outcome not in {"selected","handled_by_human"} or (run.request.request_type == "shortlist" and d.outcome != "handled_by_human" and len(d.selected_supplier_ids) < run.request.shortlist_count)]
            if blocked and run.request.human_review_enabled:
                reasons = sorted({reason for d in blocked for candidate in d.candidates for reason in candidate.reason_codes})
                if not reasons: reasons = ["NO_ELIGIBLE_SUPPLIER_OR_INSUFFICIENT_SHORTLIST"]
                open_review(run,"NO_PURCHASABLE_PLAN","The requested items cannot be sourced with the available verified evidence. Human help is required.",reasons,[d.spec_id for d in blocked],S.DECIDING)
                return
            needs_approval = any(o.state == "PLANNED" for o in run.orders)
            transition(run, S.WAITING_FOR_APPROVAL if needs_approval else S.EXECUTING,
                       "Plan saved; exact amounts require approval." if needs_approval else "Plan saved; executing mock orders or preparing RFQ drafts.")
        elif run.state == S.EXECUTING:
            # Resolve effects from a prior crash before considering any replacement plan.
            for order in run.orders:
                if order.state in {"PAY_PENDING", "RECONCILING"}:
                    previous = self.payments.lookup(order.idempotency_key)
                    if previous:
                        order.payment_id = previous["payment_id"]
                        order_transition(order, "PAID")
                        record_spend(c, run.id, "orders", order.amount_minor, "Simulated supplier order (reconciled)", order.idempotency_key)
            if run.plan_version != fingerprint(run) or any(q.expires_at <= now() for q in run.quotes if any(o.quote_id == q.id and o.state != "PAID" for o in run.orders)):
                transition(run, S.DECIDING, "Plan changed or evidence expired; old approvals cannot authorize a new plan.")
                return
            pending = next((o for o in run.orders if o.state != "PAID"), None)
            if pending:
                self.order_step(c, run, pending)
                return
            if run.request.request_type == "shortlist" or pack.ordering_method == "rfq":
                for decision in run.decisions:
                    for supplier_id in decision.selected_supplier_ids:
                        draft_id = f"{run.id}:{decision.spec_id}:{supplier_id}"
                        if not any(d.id == draft_id for d in run.rfqs):
                            spec = next(s for s in run.specs if s.id == decision.spec_id)
                            run.rfqs.append(RFQDraft(id=draft_id, supplier_id=supplier_id, subject="RFQ: " + spec.description,
                                body=f"Please provide capabilities, MOQ, pricing, material/finish options, certifications and delivery lead time to {run.request.destination}. Requested scope: {run.request.text}. This is a draft; details must be confirmed before ordering."))
            gaps = any(d.outcome not in {"selected","handled_by_human"} for d in run.decisions) or (run.request.request_type == "shortlist" and any(d.outcome != "handled_by_human" and len(d.selected_supplier_ids) < run.request.shortlist_count for d in run.decisions))
            message = "Finished with explicit gaps." if gaps else ("Finished; reviewer-reported manual handling is recorded separately from automated orders." if run.human_completed_spec_ids else "Finished: mock payments reconciled; RFQs remain drafts.")
            transition(run, S.COMPLETED_WITH_GAPS if gaps else S.COMPLETED,message)

    def research_step(self, c, run, pack):
        suppliers = {s.id: s for s in run.suppliers}
        if run.request.request_type != "purchase" or run.request.mode == "live":
            return False
        for quote in run.quotes:
            if quote.stock is not None:
                continue
            purchase = next((p for p in run.research if p.quote_id == quote.id), None)
            if purchase and purchase.status in {"skipped", "released", "paid"}:
                continue
            if purchase is None:
                price = pack.research_cost_minor
                useful, rationale = worth_buying(quote, price, pack)
                hypothetical = quote.model_copy(update={"stock": max(quote.quantity, 10000)})
                could_qualify = assess(hypothetical, suppliers[quote.supplier_id], run.request, pack).eligible
                existing = [assess(q, suppliers[q.supplier_id], run.request, pack) for q in run.quotes if q.spec_id == quote.spec_id]
                best = min((v.cost.total_minor for v in existing if v.eligible and v.cost), default=None)
                cost = landed_cost(quote, pack)
                could_change = best is None or (cost is not None and cost.total_minor < best)
                purchase = ResearchPurchase(id=f"research:{run.id}:{quote.id}", quote_id=quote.id,
                    question="Can the supplier fulfil the required stock quantity?", quoted_minor=price, rationale=rationale,
                    status="quoted" if useful and could_qualify and could_change else "skipped")
                if purchase.status == "skipped":
                    purchase.rationale += " Other hard checks fail or existing eligible options are cheaper."
                run.research.append(purchase)
                return True
            if purchase.status == "quoted":
                purchase.status = "reserved" if reserve(c, run, purchase.id, purchase.quoted_minor) else "skipped"
                if purchase.status == "skipped": purchase.rationale += " Budget ceiling prevents purchase."
                run.events.append(WorkflowEvent(state=run.state, message=f"Research {purchase.status}: {purchase.quoted_minor / 100:.2f} INR (mock)."))
                return True
            if purchase.status == "reserved":
                paid = self.payments.lookup(purchase.id)
                if not paid and not can_commit(c, run, purchase.id):
                    c.exec_driver_sql("UPDATE reservations SET status='released' WHERE id=?", (purchase.id,))
                    purchase.status = "released"
                    purchase.rationale += " Budget lowered before payment; reservation released."
                    return True
                # Reconcile any previous provider commit before attempting another payment.
                self.payments.pay(purchase.id, purchase.quoted_minor, "research")
                c.exec_driver_sql("UPDATE reservations SET status='settled' WHERE id=?", (purchase.id,))
                record_spend(c, run.id, "research", purchase.quoted_minor, purchase.question, purchase.id)
                fact = Evidence(reference=f"mock-paid-data://{purchase.id}", provider="mock_paid_data", fact="stock", value="10000 units available in synthetic scenario")
                run.evidence.append(fact)
                quote.stock = 10000
                quote.evidence_ids.append(fact.id)
                purchase.evidence_ids = [fact.id]
                purchase.status = "paid"
                return True
        return False

    def build_plan(self, run, pack):
        suppliers = {s.id: s for s in run.suppliers}
        run.decisions = [decide(s, run.quotes, suppliers, run.request, pack) for s in run.specs]
        for index, decision in enumerate(run.decisions):
            if decision.spec_id in run.human_completed_spec_ids:
                evidence_ids = ["human:"+task.id for task in run.human_reviews if task.resolution == "manual_completion" and decision.spec_id in task.spec_ids]
                run.decisions[index] = decision.model_copy(update={"outcome":"handled_by_human","selected_quote_id":None,"selected_supplier_ids":[],
                    "evidence_ids":evidence_ids,"explanation":"A reviewer reported manual handling with a reference. No automated payment is claimed for this item."})
        run.plan_version = fingerprint(run)
        paid = [o for o in run.orders if o.state == "PAID"]
        run.orders = paid
        if run.request.request_type == "purchase":
            for decision in run.decisions:
                if not decision.selected_quote_id or any(o.spec_id == decision.spec_id for o in paid):
                    continue
                candidate = next(c for c in decision.candidates if c.quote_id == decision.selected_quote_id)
                oid = f"{run.id}:{decision.spec_id}:{candidate.supplier_id}:{run.plan_version}"
                order = Order(id=oid, spec_id=decision.spec_id, supplier_id=candidate.supplier_id, quote_id=candidate.quote_id,
                    plan_version=run.plan_version, amount_minor=candidate.cost.total_minor,
                    idempotency_key=hashlib.sha256(oid.encode()).hexdigest())
                if order.amount_minor <= run.request.approval_threshold_minor:
                    order_transition(order, "APPROVED")
                run.orders.append(order)
        for purchase in run.research:
            if purchase.status == "paid":
                purchase.changed_decision = any(d.selected_quote_id == purchase.quote_id for d in run.decisions)

    def order_step(self, c, run, order):
        if order.state == "APPROVED":
            order_transition(order, "PAY_PENDING")
            return  # Durable intent before any provider side effect.
        if order.state not in {"PAY_PENDING", "RECONCILING"}:
            raise ValueError("order is not authorized")
        existing = self.payments.lookup(order.idempotency_key)
        try:
            result = existing or self.payments.pay(order.idempotency_key, order.amount_minor, "orders", run.request.scenario)
        except TimeoutError:
            order_transition(order, "RECONCILING")
            run.events.append(WorkflowEvent(state=run.state, message="Mock checkout timed out after committing; will reconcile by idempotency key."))
            return
        order.payment_id = result["payment_id"]
        order_transition(order, "PAID")
        record_spend(c, run.id, "orders", order.amount_minor, "Simulated supplier order", order.idempotency_key)


def approve(run, approval: Approval):
    if run.state != S.WAITING_FOR_APPROVAL:
        raise ValueError("run is not waiting for approval")
    if approval.plan_version != run.plan_version or run.plan_version != fingerprint(run):
        raise ValueError("stale plan approval; replan required")
    order = next((o for o in run.orders if o.id == approval.order_id), None)
    if order is None or order.amount_minor != approval.amount_minor or order.plan_version != approval.plan_version:
        raise ValueError("approval does not match exact order version and amount")
    if any(q.expires_at <= now() for q in run.quotes if q.id == order.quote_id):
        raise ValueError("quote expired; replan required")
    if order.state == "PLANNED": order_transition(order, "APPROVED")
    run.events.append(WorkflowEvent(state=run.state, message=f"Recorded approval for exact order {order.id}: {order.amount_minor} paise."))
    if all(o.state != "PLANNED" for o in run.orders):
        transition(run, S.EXECUTING, "All required approvals recorded.")
