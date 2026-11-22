"""Run from any directory; exports the backend contract without opening a database."""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from app.main import app

target = root / "docs" / "api" / "openapi.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
print(f"Exported {target}")
