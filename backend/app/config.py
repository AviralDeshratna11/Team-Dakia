import json
from functools import lru_cache
from pathlib import Path
from app.contracts import CategoryPack
import os


def load_local_environment(path=None):
    """Load local setup values without executing shell commands or overriding process env."""
    path = Path(path) if path else Path(__file__).resolve().parents[2] / ".env"
    if not path.exists(): return
    allowed = {"GEMINI_BACKEND","GEMINI_MODEL","GEMINI_API_KEY","GOOGLE_APPLICATION_CREDENTIALS",
               "GOOGLE_CLOUD_PROJECT","GOOGLE_CLOUD_LOCATION","CRUSTDATA_API_KEY","SOURCING_DB"}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        name, value = line.split("=",1)
        name, value = name.strip(), value.strip()
        if name in allowed and value:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'",'"'}: value = value[1:-1]
            os.environ.setdefault(name,value)


@lru_cache
def pack_for(category: str) -> CategoryPack:
    # Categories are descriptive input, never interpolated into arbitrary filesystem paths.
    profile = category if category in {"electronics", "leather_goods"} else "general"
    path = Path(__file__).parent / "packs" / f"{profile}.json"
    return CategoryPack.model_validate(json.loads(path.read_text(encoding="utf-8")))
