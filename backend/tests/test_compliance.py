"""Compliance screen on finalists: owners found, namesakes cleared, fronts held, adverse leadership blocked."""
import json
import httpx

from app.contracts import Supplier, RunState as S
from app.domain.compliance import classify_articles, front_company_flags, verdict
from test_live_checks import FakeCrustdata, system, start, leather_request, run_until_done

OWNERS = {1: [{"basic_profile": {"name": "Rakesh Gupta", "current_title": "Founder"}}],
          3: [{"basic_profile": {"name": "Imran Khan", "current_title": "Managing Director"}}],
          6: [{"basic_profile": {"name": "Neha Singh", "current_title": "Proprietor"}}]}
NEWS = {"Imran Khan": [
            {"title": "Imran Khan speaks at cricket awards", "snippet": "The former Pakistan captain...", "url": "https://news.example/1"},
            {"title": "Agra Leather Works MD Imran Khan arrested in GST fraud case", "snippet": "Agra police said...", "url": "https://news.example/2"}],
        "Rakesh Gupta": [
            {"title": "Rakesh Gupta named in Delhi land scam", "snippet": "A Delhi builder...", "url": "https://news.example/3"}]}


class FakeWithPeople(FakeCrustdata):
    def __call__(self, request):
        path = request.url.path
        if path == "/account/endpoints":
            response = super().__call__(request)
            body = response.json()
            body["endpoints"].append({"path": "/person/search", "status": "enabled", "base_credits": 0.03})
            return httpx.Response(200, json=body)
        if path == "/person/search":
            body = json.loads(request.content)
            self.calls.append((path, body))
            cid = body["filters"]["conditions"][0]["value"]
            return httpx.Response(200, headers={"x-credits-used": "0.03"}, json={"profiles": OWNERS.get(cid, [])})
        if path == "/web/search/live":
            body = json.loads(request.content)
            self.calls.append((path, body))
            name = body["query"].strip('"')
            return httpx.Response(200, headers={"x-credits-used": "1"}, json={"success": True, "results": NEWS.get(name, [])})
        return super().__call__(request)


def supplier(name, **profile):
    return Supplier(id=name, canonical_id="1", name=name, domain=f"{name.split()[0].lower()}.example", locations=["Agra, Uttar Pradesh, India"],
                    kind="manufacturer", identity="resolved", profile={"headquarters": "Agra, Uttar Pradesh, India", **profile})


def test_namesakes_are_cleared_and_tied_adverse_news_counts():
    split = classify_articles("Imran Khan", supplier("Agra Leather Works"), NEWS["Imran Khan"])
    assert [a["title"] for a in split["namesakes_cleared"]] == ["Imran Khan speaks at cricket awards"]
    assert len(split["adverse"]) == 1 and "GST fraud" in split["adverse"][0]["title"]
    unrelated = classify_articles("Rakesh Gupta", supplier("Kanpur Hides Pvt Ltd"), NEWS["Rakesh Gupta"])
    assert not unrelated["adverse"] and len(unrelated["namesakes_cleared"]) == 1


def test_front_company_flags_hold_but_never_block_alone():
    shell = supplier("New Hides", founded=2026, headcount=2, headquarters_raw="A, B, C, D, E, Kanpur, Uttar Pradesh, India")
    flags = front_company_flags(shell, [shell])
    assert len(flags) == 3
    assert verdict([{"name": "X"}], flags, [])[0] == "hold"
    assert verdict([], [], [])[0] == "hold"  # owner unknown
    assert verdict([{"name": "X"}], [], [])[0] == "approve"
    assert verdict([{"name": "X"}], [], [{"title": "fraud"}])[0] == "block"


def test_blocked_finalist_is_replaced_and_rest_screened(tmp_path):
    fake = FakeWithPeople(order=[1, 2, 3, 5, 4, 6])
    db, worker, client = system(tmp_path, fake)
    rid = start(db, leather_request())
    run = run_until_done(db, worker, rid)

    assert any(e.state == S.SCREENING for e in run.events)
    compliance = {c.supplier_id: c for c in run.checks if c.kind == "compliance"}
    assert compliance["crustdata:3"].result == "fail" and "GST fraud" in compliance["crustdata:3"].reason
    selected = set(run.decisions[0].selected_supplier_ids)
    assert "crustdata:3" not in selected
    assert selected == {"crustdata:1", "crustdata:4", "crustdata:6"}
    assert compliance["crustdata:1"].result == "pass"  # the Delhi namesake was cleared
    assert compliance["crustdata:4"].result == "unverified" and "owns or runs" in compliance["crustdata:4"].reason
    blocked = next(c for c in run.decisions[0].candidates if c.supplier_id == "crustdata:3")
    assert "COMPLIANCE_FAILED" in blocked.reason_codes
    agra = next(s for s in run.suppliers if s.id == "crustdata:3")
    assert agra.profile["compliance"]["directors"][0]["namesakes_cleared"]
    assert run.state == S.COMPLETED and len(run.rfqs) == 3
    assert not any(r.supplier_id == "crustdata:3" for r in run.rfqs)


def test_screening_notes_do_not_force_a_replan(tmp_path):
    fake = FakeWithPeople(order=[1, 3, 6])
    db, worker, _ = system(tmp_path, fake)
    run = run_until_done(db, worker, start(db, leather_request()))
    assert not any("Plan changed" in e.message for e in run.events)
