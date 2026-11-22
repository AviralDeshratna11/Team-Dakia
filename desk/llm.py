"""Gemini on Vertex AI. Gemini models only: the credential's usage rules forbid anything else.

Answers are cached in .cache/llm/, so re-running a notebook cell isn't billed again.
Every call (hit or miss) is appended to trace.CALL_LOG, like Crustdata calls.
"""

from __future__ import annotations

import json
import os
import time
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from desk import ROOT
from desk.crustdata import cache_key
from desk.trace import CALL_LOG

CACHE_DIR = ROOT / ".cache" / "llm"
DEFAULT_MODEL = "gemini-2.5-flash"

T = TypeVar("T", bound=BaseModel)

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=os.environ["GOOGLE_CLOUD_PROJECT"],
                               location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
    return _client


def ask(prompt: str, schema: type[T], model: str = DEFAULT_MODEL, use_cache: bool = True) -> T:
    """One structured call: the answer is parsed into `schema`. Temperature 0 for repeatability."""
    if not model.startswith("gemini-"):
        raise ValueError(f"{model!r} not allowed: this credential may only call Gemini models")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = cache_key("llm", model, prompt, schema.model_json_schema())
    path = CACHE_DIR / f"{key}.json"
    if use_cache and path.exists():
        CALL_LOG.append({"endpoint": f"gemini:{model}", "what": f"llm -> {schema.__name__}",
                         "cache_key": key, "cached": True, "credits": 0, "ms": 0})
        return schema.model_validate_json(json.loads(path.read_text())["text"])

    t0 = time.monotonic()
    resp = client().models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                           response_schema=schema))
    ms = int((time.monotonic() - t0) * 1000)
    usage = resp.usage_metadata
    tokens = {"in": getattr(usage, "prompt_token_count", None), "out": getattr(usage, "candidates_token_count", None)}
    path.write_text(json.dumps({"model": model, "prompt": prompt, "schema": schema.__name__,
                                "text": resp.text, "tokens": tokens}, ensure_ascii=False))
    CALL_LOG.append({"endpoint": f"gemini:{model}", "what": f"llm -> {schema.__name__} {tokens}",
                     "cache_key": key, "cached": False, "credits": 0, "ms": ms})
    return schema.model_validate_json(resp.text)
