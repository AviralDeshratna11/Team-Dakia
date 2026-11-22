"""SQLite transactions serialize state changes across API and worker processes."""
from contextlib import contextmanager
from pathlib import Path
import os

from sqlalchemy import create_engine, event


class Database:
    def __init__(self, path: str | None = None):
        self.path = str(Path(path or os.getenv("SOURCING_DB", "sourcing.db")).resolve())
        self.engine = create_engine("sqlite:///" + self.path, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")

        with self.transaction() as c:
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount > 0), status TEXT NOT NULL)")
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS ledger (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, account TEXT NOT NULL, amount INTEGER NOT NULL, description TEXT NOT NULL, reference TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id, account, reference))")
            c.exec_driver_sql("CREATE INDEX IF NOT EXISTS reservations_run ON reservations(run_id)")
            c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ledger_run ON ledger(run_id)")

    @contextmanager
    def transaction(self):
        with self.engine.connect() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise
