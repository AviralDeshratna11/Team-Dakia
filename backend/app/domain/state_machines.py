from app.contracts import RunState as S, WorkflowEvent

TRANSITIONS = {
    S.CREATED: {S.PARSING, S.FAILED}, S.PARSING: {S.WAITING_FOR_CLARIFICATION, S.DISCOVERING, S.FAILED},
    S.WAITING_FOR_CLARIFICATION: {S.PARSING}, S.DISCOVERING: {S.VERIFYING, S.FAILED},
    S.VERIFYING: {S.RESEARCHING, S.FAILED}, S.RESEARCHING: {S.DECIDING, S.FAILED},
    S.DECIDING: {S.WAITING_FOR_APPROVAL, S.EXECUTING, S.FAILED},
    S.WAITING_FOR_APPROVAL: {S.EXECUTING, S.DECIDING},
    S.EXECUTING: {S.COMPLETED, S.COMPLETED_WITH_GAPS, S.FAILED, S.DECIDING},
    S.FAILED: {S.PARSING}, S.COMPLETED: set(), S.COMPLETED_WITH_GAPS: set(), S.CANCELLED:set(),
    S.WAITING_FOR_HUMAN:{S.PARSING,S.DISCOVERING,S.VERIFYING,S.RESEARCHING,S.DECIDING,S.EXECUTING,S.WAITING_FOR_CLARIFICATION,S.COMPLETED,S.CANCELLED},
}
for source in [S.CREATED,S.PARSING,S.WAITING_FOR_CLARIFICATION,S.DISCOVERING,S.VERIFYING,S.RESEARCHING,S.DECIDING,S.WAITING_FOR_APPROVAL,S.EXECUTING,S.FAILED,S.COMPLETED_WITH_GAPS]:
    TRANSITIONS[source].add(S.WAITING_FOR_HUMAN)


def transition(run, target, message):
    if target not in TRANSITIONS[run.state]:
        raise ValueError(f"invalid run transition {run.state} -> {target}")
    run.state = target
    run.events.append(WorkflowEvent(state=target, message=message))


ORDER_TRANSITIONS = {"PLANNED": {"APPROVED"}, "APPROVED": {"PAY_PENDING"}, "PAY_PENDING": {"APPROVED", "PAID", "RECONCILING", "FAILED"}, "RECONCILING": {"APPROVED", "PAID", "FAILED"}, "PAID": set(), "FAILED": set()}


def order_transition(order, target):
    if target not in ORDER_TRANSITIONS[order.state]:
        raise ValueError(f"invalid order transition {order.state} -> {target}")
    order.state = target
