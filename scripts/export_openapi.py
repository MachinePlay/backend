"""Dump the OpenAPI schema to frontend/openapi.json (run via `just gen-api`)."""

import json
from pathlib import Path

from app.main import app


def main() -> None:
    out = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"
    out.write_text(json.dumps(app.openapi(), indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
