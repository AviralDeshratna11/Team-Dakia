"""A small, bounded tool-calling agent loop on Gemini (Vertex AI).

The model picks the next tool; code runs it and feeds back the result, until the model calls `finish`
with a typed report. Bounds: max_turns (on the last turn only `finish` is allowed) and a Crustdata
credit budget (paid tools refuse once it's spent). Agents gather and draft; code still decides.

Every model turn is cached by the full conversation so far, so re-running replays for free and gives
the same transcript. The transcript is returned for the trace.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from google.genai import types
from pydantic import BaseModel, ValidationError

from desk import ROOT, llm
from desk.crustdata import cache_key
from desk.trace import CALL_LOG

CACHE_DIR = ROOT / ".cache" / "agent"
MAX_RESULT_CHARS = 6000


@dataclass
class Tool:
    name: str
    description: str
    params: dict                     # JSON schema of the arguments
    fn: Callable[..., Any]
    paid: bool = False               # spends Crustdata credits: refused once the budget is spent


class AgentResult(BaseModel):
    agent: str
    task: str
    finished: bool
    stop_reason: str                 # finished | forced_finish | max_turns | error
    report: dict | None
    turns: int
    credits: float
    llm_calls: int
    llm_cached: int
    transcript: list[dict]


def run_agent(name: str, system: str, task: str, tools: list[Tool], report: type[BaseModel],
              max_turns: int = 8, credit_budget: float = 10.0, model: str = llm.DEFAULT_MODEL) -> AgentResult:
    if not model.startswith("gemini-"):
        raise ValueError(f"{model!r} not allowed: this credential may only call Gemini models")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    by_name = {t.name: t for t in tools}
    finish = types.FunctionDeclaration(
        name="finish", description="Call exactly once, when done (or when told to stop), with your report.",
        parameters_json_schema=_schema(report))
    decls = [types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.params)
             for t in tools] + [finish]
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=task)])]
    transcript: list[dict] = [{"turn": 0, "type": "task", "text": task}]
    credits, llm_calls, llm_cached = 0.0, 0, 0

    for turn in range(1, max_turns + 1):
        last = turn == max_turns
        config = types.GenerateContentConfig(
            system_instruction=system, temperature=0, tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode="ANY", allowed_function_names=["finish"])) if last else None)
        try:
            content, cached, usage = _generate(model, system, contents, decls, last, config)
        except Exception as e:  # noqa: BLE001 - any model/API failure ends the run, recorded
            transcript.append({"turn": turn, "type": "error", "text": f"{type(e).__name__}: {e}"})
            return AgentResult(agent=name, task=task, finished=False, stop_reason="error", report=None, turns=turn,
                               credits=credits, llm_calls=llm_calls, llm_cached=llm_cached, transcript=transcript)
        llm_calls += 1
        llm_cached += cached
        contents.append(content)
        calls = [p.function_call for p in content.parts or [] if p.function_call]
        texts = [p.text for p in content.parts or [] if p.text and not getattr(p, "thought", False)]
        if texts:
            transcript.append({"turn": turn, "type": "text", "text": " ".join(texts)[:2000]})
        if not calls:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(
                text="Use a tool, or call finish with your report.")]))
            continue

        responses = []
        for fc in calls:
            args = dict(fc.args or {})
            transcript.append({"turn": turn, "type": "call", "name": fc.name, "args": args, "tokens": usage})
            if fc.name == "finish":
                try:
                    rep = report.model_validate(args)
                except ValidationError as e:
                    result = {"error": f"invalid report, fix and call finish again: {e.errors()[:3]}"}
                else:
                    transcript.append({"turn": turn, "type": "finish", "report": rep.model_dump(mode="json")})
                    return AgentResult(agent=name, task=task, finished=True,
                                       stop_reason="forced_finish" if last else "finished",
                                       report=rep.model_dump(mode="json"), turns=turn, credits=credits,
                                       llm_calls=llm_calls, llm_cached=llm_cached, transcript=transcript)
            else:
                result, spent = _run_tool(by_name.get(fc.name), fc.name, args, credits, credit_budget)
                credits += spent
            text = _clip(result)
            transcript.append({"turn": turn, "type": "result", "name": fc.name, "result": text, "credits": credits})
            responses.append(types.Part.from_function_response(name=fc.name, response={"result": text}))
        if turn == max_turns - 1:
            responses.append(types.Part.from_text(text="Last turn next: call finish with what you have."))
        contents.append(types.Content(role="user", parts=responses))

    return AgentResult(agent=name, task=task, finished=False, stop_reason="max_turns", report=None, turns=max_turns,
                       credits=credits, llm_calls=llm_calls, llm_cached=llm_cached, transcript=transcript)


def _run_tool(tool: Tool | None, name: str, args: dict, credits: float, budget: float) -> tuple[Any, float]:
    if tool is None:
        return {"error": f"no tool named {name}"}, 0.0
    if tool.paid and credits >= budget:
        return {"error": f"credit budget spent ({credits:.1f}/{budget}); call finish with what you have"}, 0.0
    before = len(CALL_LOG)
    try:
        result = tool.fn(**args)
    except Exception as e:  # noqa: BLE001 - tool failures go back to the model as data
        result = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    spent = sum(c.get("credits", 0) for c in CALL_LOG[before:])
    return result, spent


def _generate(model: str, system: str, contents: list, decls: list, last: bool, config) -> tuple:
    """One model turn, cached by everything that determines it."""
    key = cache_key("agent", model, system, last,
                    [d.model_dump(mode="json", exclude_none=True) for d in decls],
                    [c.model_dump(mode="json", exclude_none=True) for c in contents])
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        hit = json.loads(path.read_text())
        CALL_LOG.append({"endpoint": f"gemini:{model}", "what": "agent turn", "cache_key": key, "cached": True,
                         "credits": 0, "ms": 0})
        return types.Content.model_validate(hit["content"]), True, hit.get("usage")
    t0 = time.monotonic()
    resp = llm.client().models.generate_content(model=model, contents=contents, config=config)
    ms = int((time.monotonic() - t0) * 1000)
    content = resp.candidates[0].content if resp.candidates and resp.candidates[0].content else \
        types.Content(role="model", parts=[types.Part.from_text(text="(empty response)")])
    u = resp.usage_metadata
    usage = {"in": getattr(u, "prompt_token_count", None), "out": getattr(u, "candidates_token_count", None),
             "thought": getattr(u, "thoughts_token_count", None)}
    path.write_text(json.dumps({"content": content.model_dump(mode="json", exclude_none=True), "usage": usage}))
    CALL_LOG.append({"endpoint": f"gemini:{model}", "what": f"agent turn {usage}", "cache_key": key,
                     "cached": False, "credits": 0, "ms": ms})
    return content, False, usage


def _schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema, flattened for Gemini (no $defs / titles)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


def _clip(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " …(truncated)"
