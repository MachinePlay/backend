"""Dump the FastAPI OpenAPI schema to frontend/openapi.json.

Run from the `backend/` directory: `uv run python scripts/export_openapi.py`.
The frontend's `gen-api` script consumes the file to produce TS types.
"""

import json
from pathlib import Path

from app.main import app


def main() -> None:
    out = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"
    out.write_text(json.dumps(app.openapi(), indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
