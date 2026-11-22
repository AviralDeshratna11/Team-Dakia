import json
from functools import lru_cache
from pathlib import Path
from app.contracts import CategoryPack


@lru_cache
def pack_for(category: str) -> CategoryPack:
    if category not in {"electronics", "leather_goods"}:
        raise ValueError("unsupported category")
    path = Path(__file__).parent / "packs" / f"{category}.json"
    return CategoryPack.model_validate(json.loads(path.read_text(encoding="utf-8")))
