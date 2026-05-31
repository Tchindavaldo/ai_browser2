---
name: update-swagger
description: Regenerate the committed OpenAPI/Swagger schema (docs/openapi.json) after changing FastAPI endpoints or Pydantic models in core/server.py. Use whenever routes, request/response models, tags, summaries, or validation errors change so the documented schema stays in sync with the code.
---

# Update Swagger / OpenAPI

The live docs are served by FastAPI at `/docs` (Swagger UI) and `/openapi.json`.
A committed copy lives at `docs/openapi.json` so the schema is reviewable in git
and consumable by clients without booting the server.

## When to run
After any change to `core/server.py` that affects the API surface:
- new/removed/renamed endpoint
- changed request/response model (`PayRequest`, `PayResponse`, …) or `Field` metadata
- changed tags, summaries, or documented error `responses`
- a new aggregator that changes `supported_networks` exposed by `/aggregators`

## How to run
Always use the project venv (its FastAPI/Pydantic versions define the schema; the
global `python` may differ and produce a spurious diff):
```bash
venv/bin/python scripts/dump_openapi.py
```
This imports the app (no browser/LLM started — `app.openapi()` only reads metadata)
and writes `docs/openapi.json`.

## Verify
- The command prints `OpenAPI schema written to … (N paths)`.
- `git diff docs/openapi.json` shows the intended changes (new path, field, etc.).
- Optionally boot and check: `python main.py` then open `http://localhost:7332/docs`.

## Commit
Include `docs/openapi.json` in the same commit as the `core/server.py` change so the
schema never drifts from the code.
