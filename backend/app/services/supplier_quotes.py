"""Record supplier-issued terms checked by a reviewer in the local workspace."""
from app.contracts import Evidence, Quote, ReviewedQuoteTerms, RunState, Supplier, WorkflowEvent, now
from app.domain.quote_scope import comparable_text, quote_scope


def add_supplier_quote(run, task_id, submitted):
    task = next((task for task in run.human_reviews if task.id == task_id and task.status == "pending"), None)
    if run.state != RunState.WAITING_FOR_HUMAN or task is None:
        raise ValueError("Supplier quotes can be added during a pending human review")
    if any(order.state in {"PAY_PENDING", "RECONCILING"} for order in run.orders):
        raise ValueError("Reconcile the pending checkout before adding a replacement quote")
    spec = next((spec for spec in run.specs if spec.id == submitted.spec_id), None)
    if spec is None or spec.unresolved_questions or (task.spec_ids and spec.id not in task.spec_ids):
        raise ValueError("Use a fully specified item from this review")
    if spec.id in run.human_completed_spec_ids or any(order.spec_id == spec.id and order.state == "PAID" for order in run.orders):
        raise ValueError("This item has already been handled")
    if ((spec.quantity is not None and submitted.quantity != spec.quantity)
            or (spec.quantity is None and run.request.request_type == "purchase")
            or comparable_text(submitted.unit) != comparable_text(spec.unit)):
        raise ValueError("Quoted quantity and unit must match the request")
    if comparable_text(submitted.destination) != comparable_text(run.request.destination):
        raise ValueError("Quoted destination must match the request")
    at = now()
    if submitted.delivery_at.utcoffset() is None or submitted.expires_at.utcoffset() is None:
        raise ValueError("Delivery and expiration must include a time zone")
    if submitted.expires_at <= at:
        raise ValueError("The supplier quote has expired")
    if not at < submitted.delivery_at <= run.request.deadline:
        raise ValueError("Quoted delivery must be in the future and meet the request deadline")
    if any(not value.strip() for value in (submitted.reviewer, submitted.identity_reference, submitted.quote_reference, submitted.quote_excerpt, submitted.supplier_name)):
        raise ValueError("Supplier quote, identity evidence and reviewer details are required")

    domain = submitted.supplier_domain.lower().removeprefix("www.")
    supplier = next((supplier for supplier in run.suppliers if supplier.domain.lower().removeprefix("www.") == domain), None)
    if supplier is None:
        supplier = Supplier(id=f"domain:{domain}", canonical_id=None, name=submitted.supplier_name.strip(), domain=domain,
                            locations=[], kind=submitted.supplier_kind, identity="unverified")
        run.suppliers.append(supplier)

    evidence = Evidence(reference=submitted.quote_reference.strip(), provider="reviewed_supplier_quote", mode=run.request.mode,
                        fact="Supplier-issued quote checked by " + submitted.reviewer.strip(), value=submitted.quote_excerpt.strip())
    identity = Evidence(reference=submitted.identity_reference.strip(), provider="reviewed_supplier_quote", mode=run.request.mode,
                        fact="Supplier identity checked by " + submitted.reviewer.strip(), value=f"{submitted.supplier_name.strip()} ({domain})")
    scope = quote_scope(run.request, spec)
    signals = {"authorized": submitted.authorized_distributor, "factory": submitted.manufacturer_verified}
    terms = ReviewedQuoteTerms(request_fingerprint=scope, total_minor=submitted.total_minor,
                              destination=submitted.destination, reviewer=submitted.reviewer.strip(),
                              supplier_kind=submitted.supplier_kind, identity_reference=identity.reference)
    # One active reviewed quote per supplier and item. The evidence trail remains available.
    run.quotes = [quote for quote in run.quotes if not (quote.supplier_id == supplier.id and quote.spec_id == spec.id and quote.reviewed_terms)]
    run.quotes.append(Quote(id=f"reviewed:{evidence.id}", supplier_id=supplier.id, spec_id=spec.id, tiers={},
                            quantity=submitted.quantity, stock=submitted.quantity, currency="INR", delivery_at=submitted.delivery_at,
                            matches_spec=True, serves_destination=True, expires_at=submitted.expires_at,
                            evidence_ids=[evidence.id, identity.id], signals=signals, reviewed_terms=terms))
    run.evidence.extend([evidence, identity])
    run.events.append(WorkflowEvent(state=run.state, message=f"{terms.reviewer} added a supplier quote for {spec.description} from {supplier.name}."))
