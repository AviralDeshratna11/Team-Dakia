import sys
from datetime import date, timedelta

import yaml

from conftest import FakeCrustdata, find_with, quote
from desk import draft_pack, orders, payments, pipeline, rfq, steps
from desk.__main__ import main


def test_all_cli_commands_offline(spec, real_sellers, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.yaml"
    request.write_text(yaml.safe_dump({
        "text": "Buy the test voltage regulator", "category": "electronics",
        "units": spec.qty, "deliver_to": "Bengaluru", "deadline": date.today() + timedelta(days=30),
        "approval_limit_inr": 0, "lines": [{
            "line_id": spec.line_id, "description": spec.description, "mpn": spec.mpn, "maker": spec.maker,
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(pipeline, "Crustdata", lambda **_: FakeCrustdata(real_sellers))
    monkeypatch.setattr(steps, "find_suppliers", lambda line, pack, _: find_with(steps.assess_quote(quote(), line, pack)))

    def cli(*args):
        monkeypatch.setattr(sys, "argv", ["desk", *args])
        main()
        return capsys.readouterr().out

    assert "run:" in cli("run", str(request))
    [order] = orders.list_orders()
    assert order.state == "PLANNED"
    assert spec.mpn in cli("show") and "PLANNED" in cli("orders")
    assert "APPROVED" in cli("approve", order.idempotency_key)
    cli("run", str(request))
    assert "PAID" in cli("orders")
    assert "provider: mock" in cli("wallet")
    cli("wallet", "limit", "50")
    assert payments.AgentWallet().per_payment_limit == 50

    thread = rfq.open_thread("request", spec, "Example supplier", "supplier.example", "Bengaluru", None)
    assert thread.thread_id in cli("rfq")
    cli("rfq", "reply", thread.thread_id, "Sample quote")
    assert "Sample quote" in cli("rfq", thread.thread_id)

    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "sample.yaml").write_text("name: sample\n", encoding="utf-8")
    monkeypatch.setattr(draft_pack, "DRAFTS_DIR", drafts)
    monkeypatch.setattr(draft_pack, "PACKS_DIR", tmp_path)
    assert "pack sample approved" in cli("pack", "approve", "sample")
    assert (tmp_path / "sample.yaml").read_text(encoding="utf-8") == "name: sample\n"
