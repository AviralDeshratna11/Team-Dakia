"""Step 1: turn the founder's words into specs. Gemini reads; code validates and fills the gaps.

The model may suggest part numbers, but nothing is trusted until step 2 finds it on a live
distributor page. A line the model can't pin down becomes one question for the founder.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from pydantic import BaseModel

from desk import llm
from desk.schemas import PartCandidate, Question, Request, Spec, SpecResult


# What Gemini fills in. No free-form dicts: Gemini's structured output can't fill them.
class _KV(BaseModel):
    name: str
    value: str


class _Candidate(BaseModel):
    mpn: str
    manufacturer: str


class _Line(BaseModel):
    line_id: str
    description: str
    part_type: str
    params: list[_KV]
    qty_per_unit: int
    candidates: list[_Candidate]
    clarifying_question: str | None


class _Parsed(BaseModel):
    category: str | None
    units: int | None
    deliver_to: str | None
    deadline_date: str | None
    deadline_days: int | None
    data_budget_inr: float | None
    lines: list[_Line]
    assumptions: list[str]
    question: str | None


PROMPT = """You turn a founder's purchase request into a structured spec for a sourcing agent.
Today is {today}. Known categories: {categories}.

Rules:
- Use only facts stated in the request and the founder's answers. Never invent quantities, places,
  dates or budgets: leave them null. If the number of units is missing, set `question`.
- deadline: an explicit date goes in deadline_date (YYYY-MM-DD); a duration ("3 weeks") in deadline_days.
- One line per distinct part. line_id: a short snake_case name for the part (e.g. "ldo", "usb_c").
- qty_per_unit: how many of this part go into one unit. Use 1 unless the text says otherwise, and if
  the text hints at more without saying how many, keep 1 and say so in `assumptions`.
- params: the specs that decide which part fits, as name/value pairs. {spec_hint}
- candidates: up to 3 exact, orderable manufacturer part numbers that meet every stated param, the most
  common and widely stocked first, each with the original manufacturer's full name. If the founder
  named a part number, it comes first. Give the full orderable part number with every ordering suffix
  (memory size, package, temperature grade), never a family or series name. These are checked against
  live distributor data, so only list part numbers you are confident exist.
- If a spec that decides which part to buy is missing, so you can't responsibly name a candidate
  (e.g. "a small regulator" with no voltage or current), leave candidates empty and write ONE short
  clarifying_question for that line. Don't ask about anything that wouldn't change which part to buy.
- assumptions: everything you assumed that the text doesn't say.
- If the founder has answered a question, use the answer as a fact and don't ask about it again.

Request:
{text}
{answers}{final}"""

FINAL_PASS = """
This is the final pass: the founder has answered once and won't be asked again. Do not ask any
question (clarifying_question and question must be null) and give candidates for every line. Where
something is still open, choose the most common option consistent with the request and the answers,
and record each such choice in assumptions."""


def understand_specs(req: Request, packs: dict[str, dict], final: bool = False) -> SpecResult:
    """final=True is round 2 of the clarification gate: the model may not ask again."""
    if req.lines:
        return _manual(req)

    answers = ("\nFounder's answers to earlier questions:\n" + "\n".join(req.answers)) if req.answers else ""
    hints = " ".join(p.get("spec_hint", "") for p in packs.values())
    parsed = llm.ask(PROMPT.format(today=date.today().isoformat(), categories=", ".join(packs),
                                   spec_hint=hints, text=req.text.strip(), answers=answers,
                                   final=FINAL_PASS if final else ""), _Parsed)

    category = req.category or parsed.category
    if category not in packs:
        raise ValueError(f"category {category!r} has no pack yet (have: {', '.join(packs)})")
    units = req.units or parsed.units
    deadline = req.deadline or _deadline(parsed)

    questions = [Question(line_id=None, question=parsed.question)] if parsed.question else []
    if units is None and not questions:
        questions.append(Question(line_id=None, question="How many units do you need?"))

    specs, seen = [], set()
    for line in parsed.lines:
        line_id = _unique(_slug(line.line_id or line.part_type), seen)
        if not line.candidates:
            questions.append(Question(line_id=line_id, question=line.clarifying_question
                                      or f"Which exact part do you want for '{line.description}'?"))
            continue
        if units is None:
            continue
        first, *rest = line.candidates
        specs.append(Spec(line_id=line_id, description=line.description, part_type=line.part_type,
                          mpn=first.mpn.strip(), maker=first.manufacturer.strip() or None,
                          alternates=[PartCandidate(mpn=c.mpn.strip(), maker=c.manufacturer.strip() or None)
                                      for c in rest],
                          params={kv.name: kv.value for kv in line.params},
                          qty=units * max(line.qty_per_unit, 1), source=llm.DEFAULT_MODEL))

    return SpecResult(parsed_by=llm.DEFAULT_MODEL, category=category, units=units,
                      deliver_to=req.deliver_to or parsed.deliver_to, deadline=deadline,
                      data_budget_inr=req.data_budget_inr if req.data_budget_inr is not None
                      else parsed.data_budget_inr,
                      specs=specs, questions=questions, assumptions=parsed.assumptions)


def _manual(req: Request) -> SpecResult:
    """Lines given by hand: no LLM. Every line must carry an exact part number."""
    specs, questions = [], []
    for line in req.lines:
        if not line.mpn:
            questions.append(Question(line_id=line.line_id, question=f"Which exact part for '{line.description}'?"))
            continue
        specs.append(Spec(line_id=line.line_id, description=line.description, mpn=line.mpn, maker=line.maker,
                          params=line.params, qty=(req.units or 0) * line.qty_per_unit))
    return SpecResult(parsed_by="manual", category=req.category or "electronics", units=req.units,
                      deliver_to=req.deliver_to, deadline=req.deadline, data_budget_inr=req.data_budget_inr,
                      specs=specs, questions=questions, assumptions=[])


def _deadline(p: _Parsed) -> date | None:
    if p.deadline_date:
        try:
            return date.fromisoformat(p.deadline_date)
        except ValueError:
            pass
    return date.today() + timedelta(days=p.deadline_days) if p.deadline_days else None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "part"


def _unique(s: str, seen: set) -> str:
    out, i = s, 2
    while out in seen:
        out, i = f"{s}_{i}", i + 1
    seen.add(out)
    return out
