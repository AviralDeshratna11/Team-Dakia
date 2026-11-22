"""Funded synthetic wallet whose receipts survive workflow rollback."""
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import create_engine
from app.contracts import uid
from app.providers.payments import InsufficientFunds, PaymentReceipt


class SimulatedCrash(BaseException):
    pass


class MockPayments:
    def __init__(self, path, opening_minor=100_000_000):
        if not 0 <= opening_minor <= 100_000_000:
            raise ValueError("Invalid synthetic opening balance")
        self.engine = create_engine("sqlite:///" + str(path), connect_args={"timeout": 30, "check_same_thread": False})
        with self.write() as c:
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS payments (key TEXT PRIMARY KEY, amount INTEGER NOT NULL, payment_id TEXT NOT NULL, account TEXT NOT NULL)")
            columns = {row[1] for row in c.exec_driver_sql("PRAGMA table_info(payments)")}
            if "payee_ref" not in columns:
                c.exec_driver_sql("ALTER TABLE payments ADD COLUMN payee_ref TEXT")
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS wallet_funding (key TEXT PRIMARY KEY, amount INTEGER NOT NULL, created_at TEXT NOT NULL)")
            c.exec_driver_sql("INSERT OR IGNORE INTO wallet_funding VALUES ('opening', ?, ?)", (opening_minor, self.timestamp()))

    @staticmethod
    def timestamp():
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def write(self):
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection
            connection.commit()

    @staticmethod
    def totals(connection):
        funded = connection.exec_driver_sql("SELECT COALESCE(SUM(amount), 0) FROM wallet_funding").scalar_one()
        spent = connection.exec_driver_sql("SELECT COALESCE(SUM(amount), 0) FROM payments").scalar_one()
        return {"currency": "INR", "mode": "synthetic", "available_minor": funded - spent,
                "funded_minor": funded, "spent_minor": spent}

    def wallet(self):
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            return self.totals(connection)

    def fund(self, key, amount):
        if key == "opening" or not 0 < amount <= 100_000_000:
            raise ValueError("Invalid synthetic funding request")
        with self.write() as connection:
            connection.exec_driver_sql("INSERT OR IGNORE INTO wallet_funding VALUES (?, ?, ?)", (key, amount, self.timestamp()))
            recorded = connection.exec_driver_sql("SELECT amount FROM wallet_funding WHERE key=?", (key,)).scalar_one()
            if recorded != amount:
                raise ValueError("Funding key already used with another amount")
            return self.totals(connection)

    def lookup(self, key) -> PaymentReceipt | None:
        with self.engine.connect() as c:
            row = c.exec_driver_sql("SELECT * FROM payments WHERE key=?", (key,)).mappings().first()
            return dict(row) if row else None

    def pay(self, key, amount, account, fault=None, *, payee_ref=None) -> PaymentReceipt:
        if amount < 0: raise ValueError("negative payment")
        with self.write() as c:
            row = c.exec_driver_sql("SELECT * FROM payments WHERE key=?", (key,)).mappings().first()
            if row:
                if row["amount"] != amount or row["account"] != account or row["payee_ref"] not in (None, payee_ref):
                    raise ValueError("idempotency key payload conflict")
                return dict(row)
            if amount > self.totals(c)["available_minor"]:
                raise InsufficientFunds("Synthetic wallet has insufficient funds")
            result: PaymentReceipt = {"key": key, "amount": amount, "account": account,
                                      "payment_id": "sim_" + uid(), "payee_ref": payee_ref}
            c.exec_driver_sql("INSERT INTO payments(key, amount, payment_id, account, payee_ref) VALUES (?, ?, ?, ?, ?)",
                              (key, amount, result["payment_id"], account, payee_ref))
        if fault == "checkout_timeout": raise TimeoutError("Mock timeout AFTER payment committed")
        if fault == "checkout_crash": raise SimulatedCrash("Mock worker crash AFTER payment committed")
        return result

    def count(self, account="orders"):
        with self.engine.connect() as c:
            return c.exec_driver_sql("SELECT COUNT(*) FROM payments WHERE account=?", (account,)).scalar_one()
