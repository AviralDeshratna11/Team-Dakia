from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from app.contracts import Candidate, Cost, Decision, now
from app.domain.quote_scope import quote_scope


def rounded(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def landed_cost(quote, pack) -> Cost | None:
    applicable = [q for q in quote.tiers if q <= quote.quantity]
    fx = pack.fx_to_inr.get(quote.currency)
    if not applicable or quote.shipping_minor is None or fx is None:
        return None
    rate = Decimal(fx)
    goods = rounded(Decimal(quote.tiers[max(applicable)] * quote.quantity) * rate)
    shipping = rounded(Decimal(quote.shipping_minor * (quote.quantity if quote.shipping_basis == "unit" else 1)) * rate)
    duty = rounded(Decimal(goods + shipping) * Decimal(pack.duty_bps) / 10000)
    tax = rounded(Decimal(goods + shipping + duty) * Decimal(pack.tax_bps) / 10000)
    total = goods + shipping + duty + tax
    return Cost(goods_minor=goods, shipping_minor=shipping, duty_minor=duty, tax_minor=tax,
                total_minor=total, unit_minor=str((Decimal(total) / quote.quantity).quantize(Decimal(".01"))))


def assess(quote, supplier, request, pack, at: datetime | None = None, *, spec=None) -> Candidate:
    at = at or now()
    reviewed = quote.reviewed_terms
    signals = dict(quote.signals, identity=bool(reviewed) or supplier.identity == "resolved", stock=None if quote.stock is None else quote.stock >= quote.quantity)
    breakdown = {k: w if signals.get(k) is True else 0 for k, w in pack.weights.items()}
    coverage = sum(w for k, w in pack.weights.items() if signals.get(k) is not None)
    reasons = []
    if not signals["identity"]: reasons.append("IDENTITY_UNVERIFIED")
    supplier_kind = reviewed.supplier_kind if reviewed else supplier.kind
    if supplier_kind not in pack.allowed_kinds: reasons.append("SUPPLIER_TYPE_NOT_ELIGIBLE")
    if quote.matches_spec is not True: reasons.append("SPEC_MISMATCH_OR_UNVERIFIED")
    if quote.serves_destination is False or (request.request_type == "purchase" and quote.serves_destination is not True):
        reasons.append("DESTINATION_UNVERIFIED_OR_UNSERVED")
    if quote.expires_at <= at: reasons.append("EVIDENCE_STALE")
    if quote.status != "verified": reasons.append("QUOTE_UNVERIFIED")
    if reviewed and (spec is None or reviewed.request_fingerprint != quote_scope(request, spec)):
        reasons.append("REVIEWED_QUOTE_SCOPE_CHANGED")
    if quote.quantity < quote.moq: reasons.append("MOQ_NOT_MET")
    for signal in pack.trust_signals:
        if signal in {"authorized", "factory"} and signals.get(signal) is not True:
            reasons.append(f"{signal.upper()}_UNVERIFIED_OR_FAILED")
    cost = None
    if request.request_type == "purchase":
        if not (reviewed or pack.cost_policy_confirmed): reasons.append("COST_POLICY_UNCONFIRMED")
        if quote.stock is None: reasons.append("STOCK_UNVERIFIED")
        elif quote.stock < quote.quantity: reasons.append("INSUFFICIENT_STOCK")
        if quote.delivery_at is None: reasons.append("DELIVERY_UNVERIFIED")
        elif quote.delivery_at > request.deadline or (reviewed and quote.delivery_at <= at): reasons.append("DEADLINE_MISSED")
        if not reviewed and quote.lead_days is None: reasons.append("LEAD_TIME_UNVERIFIED")
        if reviewed:
            cost = Cost(goods_minor=None, shipping_minor=None, duty_minor=None, tax_minor=None,
                        total_minor=reviewed.total_minor, unit_minor=str((Decimal(reviewed.total_minor) / quote.quantity).quantize(Decimal(".01"))),
                        assumptions="Supplier’s all-in INR quote. Individual cost components were not recorded.")
        elif pack.cost_policy_confirmed:
            cost = landed_cost(quote, pack)
        if cost is None: reasons.append("COST_INCOMPLETE")
    # Reviewed terms have an explicit human identity and fulfillment check. Shortlists
    # require verified product fit, but do not infer stock from a catalogue entry.
    if not reviewed and request.request_type == "purchase" and sum(breakdown.values()) < pack.threshold:
        reasons.append("BELOW_RELIABILITY_THRESHOLD")
    return Candidate(quote_id=quote.id, supplier_id=supplier.id, eligible=not reasons,
                     reason_codes=reasons, reliability=sum(breakdown.values()), coverage=coverage,
                     reliability_breakdown=breakdown, cost=cost, evidence_ids=quote.evidence_ids)


def decide(spec, quotes, suppliers, request, pack) -> Decision:
    item_quotes = [quote for quote in quotes if quote.spec_id == spec.id]
    candidates = [assess(q, suppliers[q.supplier_id], request, pack, spec=spec) for q in item_quotes]
    eligible = sorted((c for c in candidates if c.eligible), key=lambda c: (c.cost.total_minor if c.cost else 0, -c.reliability, c.supplier_id))
    by_supplier = {}
    for candidate in eligible:
        by_supplier.setdefault(candidate.supplier_id, candidate)
    selected = list(by_supplier.values())[:request.shortlist_count] if request.request_type == "shortlist" else eligible[:1]
    selected_reviewed = bool(selected and any(q.id == selected[0].quote_id and q.reviewed_terms for q in item_quotes))
    return Decision(spec_id=spec.id, selected_quote_id=selected[0].quote_id if selected and request.request_type == "purchase" else None,
                    selected_supplier_ids=[c.supplier_id for c in selected], outcome="selected" if selected else "no_eligible_supplier",
                    candidates=candidates, evidence_ids=sorted({e for c in selected for e in c.evidence_ids}), config_version=pack.version,
                    explanation=("Lowest eligible total using reviewer-confirmed supplier terms." if selected_reviewed else "Lowest eligible landed cost; reliability threshold satisfied.") if selected and request.request_type == "purchase" else
                                ("Eligible suppliers ranked by reliability." if selected else "No supplier meets the evidence and eligibility policy."),
                    alternatives=[] if selected else ["Confirm missing evidence, approve a substitute, or extend the deadline."])
