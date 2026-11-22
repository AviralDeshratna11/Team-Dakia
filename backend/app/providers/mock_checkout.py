"""Independent persistent mock provider: its commit survives workflow rollback."""
from sqlalchemy import create_engine
from app.contracts import uid


class SimulatedCrash(BaseException):
    pass


class MockPayments:
    def __init__(self, path):
        self.engine = create_engine("sqlite:///" + str(path), connect_args={"timeout": 30, "check_same_thread": False})
        with self.engine.begin() as c:
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS payments (key TEXT PRIMARY KEY, amount INTEGER NOT NULL, payment_id TEXT NOT NULL, account TEXT NOT NULL)")

    def lookup(self, key):
        with self.engine.connect() as c:
            row = c.exec_driver_sql("SELECT * FROM payments WHERE key=?", (key,)).mappings().first()
            return dict(row) if row else None

    def pay(self, key, amount, account, fault=None):
        if amount < 0: raise ValueError("negative payment")
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT OR IGNORE INTO payments VALUES(?,?,?,?)", (key, amount, uid(), account))
            row = c.exec_driver_sql("SELECT * FROM payments WHERE key=?", (key,)).mappings().one()
            if row["amount"] != amount or row["account"] != account:
                raise ValueError("idempotency key payload conflict")
            result = dict(row)
        if fault == "checkout_timeout": raise TimeoutError("Mock timeout AFTER payment committed")
        if fault == "checkout_crash": raise SimulatedCrash("Mock worker crash AFTER payment committed")
        return result

    def count(self, account="orders"):
        with self.engine.connect() as c:
            return c.exec_driver_sql("SELECT COUNT(*) FROM payments WHERE account=?", (account,)).scalar_one()
