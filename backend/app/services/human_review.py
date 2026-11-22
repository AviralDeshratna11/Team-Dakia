"""Persistent in-app procurement handoffs. No email, external contact or real payment."""
from app.contracts import HumanReviewTask, HumanReviewResolution, Evidence, SourcingRequest, RunState as S, WorkflowEvent, now
from app.domain.state_machines import transition
import hashlib


def open_review(run, code, summary, blockers=None, spec_ids=None, resume_state=None):
    pending = next((task for task in run.human_reviews if task.status == "pending"),None)
    if pending: return pending
    stage = resume_state or run.state
    if stage in {S.CREATED,S.PARSING,S.WAITING_FOR_CLARIFICATION,S.FAILED}: stage = S.PARSING
    if stage in {S.WAITING_FOR_APPROVAL,S.COMPLETED_WITH_GAPS}: stage = S.DECIDING
    task = HumanReviewTask(run_id=run.id,reason_code=code,summary=summary,blockers=blockers or [],
        spec_ids=spec_ids if spec_ids is not None else [s.id for s in run.specs if s.id not in run.human_completed_spec_ids],resume_state=stage)
    run.human_reviews.append(task)
    transition(run,S.WAITING_FOR_HUMAN,"Human review requested: "+summary+" No external message was sent.")
    return task


def resolve_review(run, task_id, resolution: HumanReviewResolution):
    task = next((t for t in run.human_reviews if t.id == task_id),None)
    if task is None: raise ValueError("human review task not found")
    fingerprint = hashlib.sha256(resolution.model_dump_json().encode()).hexdigest()
    if task.status != "pending":
        if task.resolution_fingerprint == fingerprint:
            return  # Idempotent repeat; never re-execute a resolved task.
        raise ValueError("review task is already resolved")
    if run.state != S.WAITING_FOR_HUMAN: raise ValueError("run is not waiting for this review")
    pending_payment = any(o.state in {"PAY_PENDING","RECONCILING"} for o in run.orders)
    revisions = {field:getattr(resolution,field) for field in ["specs","supplier_domains","provider_credit_limit","destination","deadline"] if getattr(resolution,field) is not None}
    if resolution.request_text is not None: revisions["text"] = resolution.request_text
    if resolution.action != "retry" and revisions: raise ValueError("request revisions are only accepted with retry")
    if pending_payment and (resolution.action != "retry" or revisions):
        raise ValueError("reconcile pending checkout with an unchanged retry before cancelling, revising or recording manual completion")
    if resolution.action == "manual_completion" and not resolution.reference:
        raise ValueError("manual handling requires a receipt, order, RFQ or other completion reference")
    if resolution.action == "evaluate_quotes" and not any(quote.reviewed_terms for quote in run.quotes):
        raise ValueError("Add a reviewed supplier quote before evaluating quotes")
    if (any(o.state == "PAID" for o in run.orders) or run.human_completed_spec_ids) and any(key in revisions for key in ["specs","text","destination","deadline"]):
        raise ValueError("paid or manually handled items cannot be rewritten; create a separate request for materially changed requirements")
    updated_request = None
    if revisions:
        data = run.request.model_dump()
        data.update(revisions)
        updated_request = SourcingRequest.model_validate(data)
    task.status = "cancelled" if resolution.action == "cancel" else "resolved"
    task.reviewer,task.resolution,task.note,task.reference = resolution.reviewer,resolution.action,resolution.note,resolution.reference
    task.resolved_at = now()
    task.resolution_fingerprint = fingerprint
    run.error = None
    if resolution.action == "cancel":
        transition(run,S.CANCELLED,"Reviewer cancelled remaining work. Existing payment records are preserved.")
    elif resolution.action == "manual_completion":
        run.evidence.append(Evidence(id="human:"+task.id,reference=resolution.reference,provider="human_review",mode=run.request.mode,
            status="unverified",fact="Human-reported manual handling; not an automated purchase",value=resolution.note))
        run.human_completed_spec_ids = sorted(set(run.human_completed_spec_ids+task.spec_ids))
        if not task.spec_ids:
            transition(run,S.COMPLETED,"Request handled manually by reviewer; no automated payment was created.")
        else:
            transition(run,S.DECIDING,"Manual handling recorded for blocked items; recomputing remaining automated work.")
    elif resolution.action == "evaluate_quotes":
        run.plan_version = None
        transition(run, S.DECIDING, "Re-evaluating saved supplier quotes against the current request.")
    else:
        if updated_request: run.request = updated_request
        target = S.EXECUTING if pending_payment else task.resume_state
        if revisions:
            target = S.PARSING if any(key in revisions for key in ["specs","text","destination","deadline"]) else S.DISCOVERING
        run.plan_version = None if target != S.EXECUTING else run.plan_version
        transition(run,target,"Reviewer requested a retry. Existing provider spending, caches and payment identities are retained.")
    run.events.append(WorkflowEvent(state=run.state,message=f"Human review {task.id}: {resolution.action} recorded by {resolution.reviewer}."))
