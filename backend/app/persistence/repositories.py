from app.contracts import WorkflowRun, LedgerEntry, now, uid


def get_run(c, run_id: str) -> WorkflowRun:
    row = c.exec_driver_sql("SELECT document FROM runs WHERE id=?", (run_id,)).first()
    if not row:
        raise KeyError(run_id)
    return WorkflowRun.model_validate_json(row[0])


def save_run(c, run: WorkflowRun):
    c.exec_driver_sql("INSERT INTO runs(id,document) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document", (run.id, run.model_dump_json()))


def list_runs(c) -> list[WorkflowRun]:
    return [WorkflowRun.model_validate_json(row[0]) for row in c.exec_driver_sql("SELECT document FROM runs ORDER BY rowid DESC")]


def ledger(c, run_id: str) -> list[LedgerEntry]:
    return [LedgerEntry(id=r.id, account=r.account, amount_minor=r.amount, description=r.description, reference=r.reference, created_at=r.created_at)
            for r in c.exec_driver_sql("SELECT * FROM ledger WHERE run_id=? ORDER BY rowid", (run_id,))]


def record_spend(c, run_id, account, amount, description, reference):
    c.exec_driver_sql("INSERT OR IGNORE INTO ledger VALUES(?,?,?,?,?,?,?)", (uid(), run_id, account, amount, description, reference, now().isoformat()))


def reserve(c, run: WorkflowRun, reservation_id: str, amount: int) -> bool:
    if amount <= 0:
        raise ValueError("reservation amount must be positive")
    existing = c.exec_driver_sql("SELECT run_id,amount,status FROM reservations WHERE id=?", (reservation_id,)).first()
    if existing:
        if existing[0] != run.id or existing[1] != amount:
            raise ValueError("reservation identity conflict")
        return existing[2] in {"reserved", "settled"}
    committed = c.exec_driver_sql("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE run_id=? AND status IN ('reserved','settled')", (run.id,)).scalar_one()
    if committed + amount > run.request.research_budget_minor:
        return False
    c.exec_driver_sql("INSERT INTO reservations VALUES(?,?,?,'reserved')", (reservation_id, run.id, amount))
    return True


def can_commit(c, run, reservation_id):
    # Re-read the latest ceiling while holding the same write lock as the payment.
    settled = c.exec_driver_sql("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE run_id=? AND status='settled'", (run.id,)).scalar_one()
    amount = c.exec_driver_sql("SELECT amount FROM reservations WHERE id=? AND status='reserved'", (reservation_id,)).scalar_one_or_none()
    return amount is not None and settled + amount <= run.request.research_budget_minor
