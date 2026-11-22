from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.contracts import RunState
from app.main import create_app
from app.persistence.database import Database
from app.providers.mock_checkout import MockPayments
from app.providers.payments import InsufficientFunds
from app.workflow.orchestrator import Orchestrator
from test_sourcing import create, drive, request


def test_concurrent_debits_cannot_overdraw_wallet(tmp_path):
    wallet = MockPayments(tmp_path / 'payments.db', opening_minor=100)

    def attempt(key):
        try:
            return wallet.pay(key, 70, 'orders', payee_ref='supplier')
        except InsufficientFunds:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['first', 'second']))
    settled, = [result for result in results if result]
    assert attempt(settled['key']) == settled
    with pytest.raises(ValueError, match='payload conflict'):
        wallet.pay(settled['key'], 70, 'orders', payee_ref='different-supplier')
    assert wallet.wallet()['available_minor'] == 30


def test_wallet_top_up_and_order_retry_through_review(tmp_path):
    db = Database(str(tmp_path / 'state.db'))
    wallet = MockPayments(db.path + '.payments.db', opening_minor=0)
    worker = Orchestrator(db, payments=wallet)
    run_id = create(db, request(human_review_enabled=True))
    paused = drive(db, worker, run_id)
    assert paused.state == RunState.WAITING_FOR_HUMAN
    task, = paused.human_reviews
    order, = paused.orders
    assert task.reason_code == 'PAYMENT_INSUFFICIENT_FUNDS'
    assert order.state == 'APPROVED' and wallet.count() == 0

    with TestClient(create_app(db)) as client:
        preview = client.get(f'/api/runs/{run_id}/orders/{order.id}/payout-preview').json()
        assert preview['body']['amount'] == order.amount_minor
        assert preview['body']['mode'] == 'UPI' and not preview['body']['queue_if_low_balance']
        assert len(preview['headers']['X-Payout-Idempotency']) == 36
        assert wallet.count() == 0
        funding = {'idempotency_key': 'test-wallet-top-up-1', 'amount_minor': order.amount_minor}
        for _ in range(2):
            response = client.post('/api/payments/wallet/fund', json=funding)
            assert response.status_code == 200
            assert response.json()['available_minor'] == order.amount_minor
        response = client.post(f'/api/runs/{run_id}/human-reviews/{task.id}/resolve', json={
            'reviewer': 'Test founder', 'action': 'retry', 'note': 'Synthetic wallet funded.',
        })
        assert response.status_code == 200
        completed = drive(db, worker, run_id)
        assert completed.state == RunState.COMPLETED
        assert completed.orders[0].idempotency_key == order.idempotency_key
        receipt = wallet.lookup(order.idempotency_key)
        assert receipt['payee_ref'] == order.supplier_id
        assert completed.orders[0].payment_id == receipt['payment_id']
        assert client.get('/api/payments/wallet').json()['available_minor'] == 0


def test_wallet_initialization_preserves_legacy_receipts(tmp_path):
    from sqlalchemy import create_engine

    path = tmp_path / 'legacy.db'
    engine = create_engine('sqlite:///' + str(path))
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE payments (key TEXT PRIMARY KEY, amount INTEGER NOT NULL, payment_id TEXT NOT NULL, account TEXT NOT NULL)')
        connection.exec_driver_sql("INSERT INTO payments VALUES ('old-order', 80, 'old-receipt', 'orders')")
    engine.dispose()
    wallet = MockPayments(path, opening_minor=100)
    assert wallet.wallet()['available_minor'] == 20
    assert wallet.pay('old-order', 80, 'orders', payee_ref='legacy')['payment_id'] == 'old-receipt'
    assert MockPayments(path, opening_minor=200).wallet()['available_minor'] == 20
