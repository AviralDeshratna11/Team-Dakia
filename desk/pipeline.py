"""Runs the six steps in order, recording each one. The notebook calls the same steps one cell at a time."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

import yaml

from desk import steps
from desk.crustdata import Crustdata
from desk import draft_pack, rfq
from desk.investigate import investigate_leads
from desk.schemas import Question, Request, SpecResult, Status
from desk.trace import Run

# Asks the founder one question and returns the answer (input() on the CLI and in the notebook).
Ask = Callable[[Question], str]


def load_request(path: str | Path) -> Request:
    return Request(**yaml.safe_load(Path(path).read_text()))


def request_key(req: Request) -> str:
    """Stable id for 'this request', so re-runs find the same orders (never pay twice)."""
    return hashlib.sha256(req.model_dump_json(exclude={"approval_limit_inr"}).encode()).hexdigest()[:16]


def clarify_specs(r: Run, req: Request, packs: dict, ask: Ask | None) -> SpecResult | None:
    """Step 1 as a gate: nothing vague reaches step 2.

    Round 1 reads the request. If anything is unclear, all questions go to the founder at once,
    and round 2 re-reads the whole request with the answers and may not ask again: anything still
    open gets the most common choice, listed in assumptions. A line that still comes back without a
    part is dropped (reported, never guessed). Returns None if questions are pending and
    there's no one to ask (e.g. a non-interactive run): add the answers to the request and re-run.
    """
    sr = r.step("01_spec", steps.understand_specs, req, packs, part="round1")
    if not sr.questions:
        return sr
    if ask is None:
        r.record("01_clarify", {"status": "WAITING_FOR_FOUNDER", "questions": sr.questions})
        return None

    answers = [f"Q: {q.question} A: {ask(q).strip()}" for q in sr.questions]
    r.record("01_clarify", {"status": "ANSWERED", "questions": sr.questions, "answers": answers})
    req = req.model_copy(update={"answers": req.answers + answers})
    sr = r.step("01_spec", steps.understand_specs, req, packs, final=True, part="round2")
    if sr.questions:
        sr.assumptions.append("still unclear after the founder's answers, so not searched: "
                              + "; ".join(f"[{q.line_id or 'request'}] {q.question}" for q in sr.questions))
    return sr


def ensure_pack(r: Run, category: str, req: Request, packs: dict, cd: Crustdata, ask: Ask | None) -> dict | None:
    """No pack for this field: an agent drafts one, and the founder approves it before it's used."""
    pack, _agent = r.step("01b_draft_pack", draft_pack.draft_pack, category, req.text, cd, packs["electronics"])
    q = Question(line_id=None, question=(
        f"I've never sourced '{category}'. I drafted a pack for it: {pack['summary'][:400]} "
        f"I'd find makers with Crustdata Company Search '{pack['company_search']['query']}' and order by "
        f"{pack['ordering'].upper()}. Results will be marked low confidence. Use it? (yes/no)"))
    if ask is None:
        r.record("01b_pack_gate", {"status": "WAITING_FOR_FOUNDER", "question": q.question,
                                   "approve_with": f"python -m desk pack approve {category}"})
        return None
    answer = ask(q).strip()
    approved = answer.lower().startswith("y")
    r.record("01b_pack_gate", {"status": "APPROVED" if approved else "REJECTED", "question": q.question,
                               "answer": answer})
    if approved:
        draft_pack.approve(category)
        return pack
    return None


def rfq_step(rkey: str, specs: list, finds: list, checks: list, pack: dict, deliver_to, deadline) -> list[dict]:
    """Open RFQs to sellers we found but couldn't price (only for parts with no usable quote), best seller
    check first and never to a REJECTED seller; advance any thread that's our move; add quoted threads to
    the part's quotes (step 5 then decides)."""
    score = {c.vendor_domain: (c.status != "REJECTED", c.score or 0) for c in checks}
    out = []
    for s, f in zip(specs, finds):
        has_usable = any(q.status == Status.FOUND for q in f.quotes)
        cfg = pack.get("rfq") or {}
        targets = sorted((q for q in f.quotes if q.found_by in ("agent", "search") and q.status == Status.GAP
                          and score.get(q.vendor_domain, (False, 0))[0]),
                         key=lambda q: score[q.vendor_domain][1], reverse=True)
        if not has_usable:
            for q in targets[:cfg.get("max_vendors", 3)]:
                rfq.open_thread(rkey, s, q.vendor, q.vendor_domain, deliver_to, deadline, questions=cfg.get("questions"))
        for t in rfq.threads(rkey):
            if t.line_id != s.line_id:
                continue
            res = rfq.run_thread(t)
            t = rfq.get(t.thread_id)
            for x in f.quotes:
                if x.vendor_domain == t.vendor_domain:
                    x.note = f"RFQ {t.status.lower()} (thread {t.thread_id})"
            q = rfq.quote_from(t, s, pack["fx_to_inr"])
            if q:
                f.quotes = [x for x in f.quotes if x.vendor_domain != q.vendor_domain] + [steps.assess_quote(q, s, pack)]
            out.append({"line_id": s.line_id, "thread": t.thread_id, "vendor": t.vendor, "status": t.status,
                        "agent_ran": res is not None, "escalation": t.escalation, "quote": t.quote})
    return out


def run(request_path: str | Path, use_cache: bool = True, ask: Ask | None = None) -> Run:
    req = load_request(request_path)
    packs = steps.load_packs()
    cd = Crustdata(use_cache=use_cache)
    r = Run(Path(request_path).stem)

    r.record("00_request", req)
    sr = clarify_specs(r, req, packs, ask)
    if sr is None or sr.units is None:
        return r
    pack = packs.get(sr.category) or ensure_pack(r, sr.category, req, packs, cd, ask)
    if pack is None:
        return r
    specs, rkey = sr.specs, request_key(req)
    finds = [r.step("02_find", steps.find_suppliers, s, pack, cd, part=s.line_id) for s in specs]
    for s, f in zip(specs, finds):
        inv = r.step("02b_investigate", investigate_leads, s, f, pack, cd, part=s.line_id)
        f.quotes += [q for q in inv.quotes if q.status != Status.NO_MATCH]
        f.alternatives += [q for q in inv.quotes if q.status == Status.NO_MATCH]
    checks = r.step("03_check", steps.check_suppliers, finds, pack, cd)
    r.step("03b_rfq", rfq_step, rkey, specs, finds, checks, pack, sr.deliver_to, sr.deadline)
    r.step("04_buy_data", steps.buy_data, finds, sr.data_budget_inr or 0)
    decisions = [r.step("05_decide", steps.decide, s, f, checks, pack, sr.deadline, part=s.line_id)
                 for s, f in zip(specs, finds)]
    for d in decisions:
        r.step("06_order", steps.place_order, d, rkey, req.approval_limit_inr, part=d.line_id)
    return r
