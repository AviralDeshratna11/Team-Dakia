from contextlib import asynccontextmanager
from pathlib import Path
import os
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from typing import Literal
from app.contracts import (Contract, SourcingRequest, WorkflowRun, WorkflowEvent, RunState as S,
                           Spec, Approval, LedgerEntry, BudgetReservation, Decision, Order, RFQDraft, Clarification,
                           HumanReviewTask, HumanReviewResolution, SupplierQuoteSubmission)
from app.domain.state_machines import transition
from app.persistence.database import Database
from app.persistence.repositories import get_run, save_run, list_runs, ledger
from app.workflow.orchestrator import approve, fingerprint, order_quote_current
from app.services.llm_extraction import LLMExtractionService
from app.services.human_review import open_review, resolve_review
from app.services.supplier_quotes import add_supplier_quote
from app.providers.mock_checkout import MockPayments
from app.providers.razorpay_preview import upi_payout_preview


class BudgetUpdate(Contract):
    research_budget_minor: int = Field(ge=0)


class ClarificationAnswer(Contract):
    answer: str = Field(min_length=1, max_length=10000)
    specs: list[Spec] | None = None


class HumanReviewRequest(Contract):
    note: str = Field(min_length=5,max_length=2000)


class WalletBalance(Contract):
    mode: Literal["synthetic"]
    currency: Literal["INR"]
    available_minor: int
    funded_minor: int
    spent_minor: int


class WalletFunding(Contract):
    amount_minor: int = Field(gt=0, le=100_000_000, strict=True)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,80}$")


def create_app(database: Database | None = None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.db = database or Database()
        app.state.wallet = MockPayments(app.state.db.path + ".payments.db")
        try:
            yield
        finally:
            app.state.wallet.engine.dispose()

    app = FastAPI(title="Sourcing Desk", version="0.1.0", lifespan=lifespan,
                  description="Local procurement skeleton. Fixture data, simulated payments, RFQ drafts only.")

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "Run not found"}})

    @app.exception_handler(ValueError)
    async def conflict(_, exc):
        return JSONResponse(status_code=409, content={"error": {"code": "INVALID_STATE", "message": str(exc)}})

    @app.exception_handler(RequestValidationError)
    async def validation(_, exc):
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": "Invalid request", "fields": [".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()]}})

    @app.get("/api/health")
    def health():
        return {"status": "ok", "payments": "mock", "live_integrations": "Crustdata requires worker key; Gemini parsing/extraction uses Vertex service-account credentials or a Gemini API key", "worker": "separate process required"}

    @app.get("/api/human-reviews",response_model=list[HumanReviewTask])
    def review_queue(request: Request):
        with request.app.state.db.transaction() as c:
            return [task for run in list_runs(c) for task in run.human_reviews if task.status == "pending"]

    @app.get("/api/payments/wallet", response_model=WalletBalance)
    def wallet_balance(request: Request):
        return request.app.state.wallet.wallet()

    @app.post("/api/payments/wallet/fund", response_model=WalletBalance)
    def fund_wallet(body: WalletFunding, request: Request):
        return request.app.state.wallet.fund(body.idempotency_key, body.amount_minor)

    @app.get("/api/runs/{run_id}/orders/{order_id}/payout-preview")
    def payout_preview(run_id: str, order_id: str, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            order = next((order for order in run.orders if order.id == order_id), None)
            if order is None:
                return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "Order not found"}})
            return upi_payout_preview(order)

    @app.post("/api/runs/{run_id}/human-review",response_model=WorkflowRun)
    def request_review(run_id: str,body: HumanReviewRequest,request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c,run_id)
            if run.state in {S.COMPLETED,S.CANCELLED}: raise ValueError("finished requests cannot be escalated")
            open_review(run,"USER_REQUESTED",body.note)
            save_run(c,run)
        return run

    @app.post("/api/runs/{run_id}/human-reviews/{task_id}/resolve",response_model=WorkflowRun)
    def resolve_human_review(run_id: str,task_id: str,body: HumanReviewResolution,request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c,run_id)
            resolve_review(run,task_id,body)
            save_run(c,run)
        return run

    @app.post("/api/runs/{run_id}/human-reviews/{task_id}/supplier-quotes", response_model=WorkflowRun, status_code=201)
    def submit_supplier_quote(run_id: str, task_id: str, body: SupplierQuoteSubmission, request: Request):
        with request.app.state.db.transaction() as connection:
            run = get_run(connection, run_id)
            add_supplier_quote(run, task_id, body)
            save_run(connection, run)
        return run

    @app.post("/api/runs", response_model=WorkflowRun, status_code=201)
    def create(body: SourcingRequest, request: Request):
        run = WorkflowRun(request=body)
        run.events.append(WorkflowEvent(state=S.CREATED, message=f"{body.mode} run created; awaiting worker."))
        with request.app.state.db.transaction() as c: save_run(c, run)
        return run

    @app.get("/api/runs", response_model=list[WorkflowRun])
    def index(request: Request):
        with request.app.state.db.transaction() as c: return list_runs(c)

    @app.get("/api/runs/{run_id}", response_model=WorkflowRun)
    def read(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id)

    @app.get("/api/runs/{run_id}/events", response_model=list[WorkflowEvent])
    def events(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id).events

    @app.get("/api/runs/{run_id}/plan", response_model=list[Decision])
    def plan(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id).decisions

    @app.get("/api/runs/{run_id}/orders", response_model=list[Order])
    def orders(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id).orders

    @app.get("/api/runs/{run_id}/rfqs", response_model=list[RFQDraft])
    def rfqs(run_id: str, request: Request):
        with request.app.state.db.transaction() as c: return get_run(c, run_id).rfqs

    @app.get("/api/runs/{run_id}/ledger", response_model=list[LedgerEntry])
    def spend(run_id: str, request: Request):
        with request.app.state.db.transaction() as c:
            get_run(c, run_id)
            return ledger(c, run_id)

    @app.get("/api/runs/{run_id}/reservations", response_model=list[BudgetReservation])
    def reservations(run_id: str, request: Request):
        with request.app.state.db.transaction() as c:
            get_run(c, run_id)
            return [BudgetReservation(id=r.id, run_id=r.run_id, amount_minor=r.amount, status=r.status) for r in c.exec_driver_sql("SELECT * FROM reservations WHERE run_id=?", (run_id,))]

    @app.patch("/api/runs/{run_id}/budget", response_model=WorkflowRun)
    def budget(run_id: str, body: BudgetUpdate, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            run.request.research_budget_minor = body.research_budget_minor
            run.events.append(WorkflowEvent(state=run.state, message=f"Research ceiling updated to {body.research_budget_minor} paise; committed spend remains visible."))
            save_run(c, run)
        return run

    @app.post("/api/runs/{run_id}/clarifications", response_model=WorkflowRun)
    def clarification(run_id: str, body: ClarificationAnswer, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            if run.state != S.WAITING_FOR_CLARIFICATION: raise ValueError("not awaiting clarification")
            previous_answer = run.request.text.rsplit("\nClarification: ",1)[-1] if "\nClarification: " in run.request.text else None
            normalize = lambda value: " ".join(value.split()).casefold()
            if not body.specs and previous_answer and normalize(body.answer) == normalize(previous_answer):
                raise ValueError("That answer is already saved. Please answer the current question: "+run.clarification.question)
            run.clarification.answer = body.answer
            run.request.text += "\nClarification: " + body.answer
            if body.specs is not None:
                updated = run.request.model_dump()
                updated["specs"] = [s.model_dump() for s in body.specs]
                run.request = SourcingRequest.model_validate(updated)
            transition(run, S.PARSING, "Clarification saved; worker will parse again.")
            save_run(c, run)
        return run

    @app.post("/api/runs/{run_id}/approvals", response_model=WorkflowRun)
    def approval(run_id: str, body: Approval, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            approve(run, body)
            save_run(c, run)
        return run

    @app.post("/api/runs/{run_id}/resume", response_model=WorkflowRun)
    def resume(run_id: str, request: Request):
        with request.app.state.db.transaction() as c:
            run = get_run(c, run_id)
            if run.state == S.WAITING_FOR_HUMAN:
                raise ValueError("resolve the pending human-review task before resuming")
            if run.state == S.FAILED:
                last_call = run.llm_usage[-1] if run.llm_usage else {}
                recoverable = run.error and any(code in run.error for code in ["GEMINI_SCHEMA_INVALID","GEMINI_OUTPUT_TRUNCATED","GEMINI_INVALID_OR_BLOCKED_OUTPUT"])
                if recoverable and last_call.get("stage") == "parse_request" and run.request_interpretation and not run.orders:
                    run.specs, question = LLMExtractionService.restore_intent(run)
                    run.clarification = Clarification(question=question or "Please confirm exact item specs using the structured editor; the last model response was not applied.")
                    run.error = None
                    transition(run,S.PARSING,"Recovered last validated request without another model call.")
                    transition(run,S.WAITING_FOR_CLARIFICATION,run.clarification.question)
                    save_run(c,run)
                    return run
                raise ValueError("Inspect run.error and configure the missing integration before creating a new run. Failed paid calls are not silently retried.")
            if run.state == S.WAITING_FOR_APPROVAL and (run.plan_version != fingerprint(run) or any(
                not order_quote_current(run, order) for order in run.orders if order.state != "PAID"
            )):
                transition(run, S.DECIDING, "Replanning changed/expired quote; approval invalidated.")
            elif run.state == S.WAITING_FOR_CLARIFICATION:
                raise ValueError("answer the clarification before resuming")
            save_run(c, run)
        return run

    return app


app = create_app()
