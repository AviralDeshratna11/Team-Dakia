"""Runs the six steps in order, recording each one. The notebook calls the same steps one cell at a time."""

from __future__ import annotations

from pathlib import Path

import yaml

from desk import steps
from desk.crustdata import Crustdata
from desk.schemas import Request
from desk.trace import Run


def load_request(path: str | Path) -> Request:
    return Request(**yaml.safe_load(Path(path).read_text()))


def run(request_path: str | Path, use_cache: bool = True) -> Run:
    req = load_request(request_path)
    pack = steps.load_pack(req.category)
    cd = Crustdata(use_cache=use_cache)
    r = Run(Path(request_path).stem)

    r.record("00_request", req)
    specs = r.step("01_spec", steps.understand_specs, req)
    finds = [r.step("02_find", steps.find_suppliers, s, pack, cd, part=s.line_id) for s in specs]
    checks = r.step("03_check", steps.check_suppliers, finds)
    r.step("04_buy_data", steps.buy_data, finds, req.data_budget_inr)
    decisions = [r.step("05_decide", steps.decide, s, f, checks, part=s.line_id) for s, f in zip(specs, finds)]
    for d in decisions:
        r.step("06_order", steps.place_order, d, r.id, part=d.line_id)
    return r
