import argparse
import logging
import time
from app.persistence.database import Database
from app.persistence.repositories import list_runs
from app.workflow.orchestrator import Orchestrator, ACTIVE
from app.providers.mock_checkout import SimulatedCrash


def cycle(db, worker):
    with db.transaction() as c:
        ids = [r.id for r in list_runs(c) if r.state in ACTIVE]
    for run_id in ids:
        worker.tick(run_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Advance every active run by one durable step")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    db = Database()
    worker = Orchestrator(db)
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            cycle(db, worker)
            if args.once: break
            time.sleep(max(.05, args.interval))
    except SimulatedCrash:
        logging.error("Intentional demo crash after provider commit. Restart the worker to reconcile safely.")
        raise SystemExit(42)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
