"""Write the OpenAPI definition to api/openapi.json so the API contract is reviewable in the repo.

    python -m scripts.export_openapi
"""
import json
from pathlib import Path

from app.main import app

OUT = Path(__file__).resolve().parent.parent / "api" / "openapi.json"


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {OUT} ({len(app.openapi()['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
