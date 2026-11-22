"""Records every step's input, output and API calls to runs/<run_id>/.

Each step call becomes one JSON file, e.g. runs/20261122-1305_electronics_bom/02_find__ldo.json:
    {step, part, started_at, duration_ms, input, output, calls, error}
"""

from __future__ import annotations

import json
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from desk import ROOT

RUNS_DIR = ROOT / "runs"

# The Crustdata client appends one dict per HTTP call here; Run.step drains it.
CALL_LOG: list[dict] = []


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return f"<{type(obj).__name__}>"


class Run:
    def __init__(self, name: str):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + f"_{name}"   # microseconds: runs never share a folder
        self.dir = RUNS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []

    def record(self, step: str, output: Any, part: str | None = None, input: Any = None,
               calls: list | None = None, duration_ms: int = 0, error: str | None = None) -> dict:
        rec = {
            "step": step,
            "part": part,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "duration_ms": duration_ms,
            "input": to_jsonable(input),
            "output": to_jsonable(output),
            "calls": calls or [],
            "error": error,
        }
        self.records.append(rec)
        fname = f"{step}__{part}.json" if part else f"{step}.json"
        (self.dir / fname).write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        return rec

    def step(self, step: str, fn: Callable, *args, part: str | None = None, **kwargs) -> Any:
        """Run fn(*args, **kwargs) and record its input, output, calls and timing."""
        CALL_LOG.clear()
        t0 = time.monotonic()
        error, output = None, None
        try:
            output = fn(*args, **kwargs)
        except Exception:
            error = traceback.format_exc()
            raise
        finally:
            self.record(step, output, part=part, input={"args": args, "kwargs": kwargs},
                        calls=list(CALL_LOG), duration_ms=int((time.monotonic() - t0) * 1000),
                        error=error)
        return output


def list_runs() -> list[Path]:
    return sorted((p for p in RUNS_DIR.glob("*") if p.is_dir()), reverse=True)


def load_run(run: str | Path | None = None) -> list[dict]:
    """Load a past run's step records (latest run if none given)."""
    path = Path(run) if run else list_runs()[0]
    if not path.is_absolute() and not path.exists():
        path = RUNS_DIR / path
    files = sorted(path.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)   # execution order
    records = []
    for file in files:
        try:
            content = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file.read_text()  # Older CLI runs used the system encoding.
        records.append(json.loads(content))
    return records
