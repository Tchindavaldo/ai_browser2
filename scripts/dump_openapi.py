"""Dump the FastAPI OpenAPI schema to docs/openapi.json.

Run after changing endpoints/models so the committed schema stays in sync:
    python scripts/dump_openapi.py

Imports the app WITHOUT starting the browser/LLM lifespan (app.openapi() only
reads the route/model metadata), so it's fast and side-effect free.
"""

import json
import os
import sys

# Ensure repo root is importable when run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import app  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "openapi.json")


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    schema = app.openapi()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"OpenAPI schema written to {OUT} ({len(schema.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
