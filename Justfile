run:
    uv run uvicorn app.main:app --reload --timeout-graceful-shutdown 0

test:
    uv run pytest

coverage:
    uv run pytest --cov --cov-report=term-missing --cov-report=html
    xdg-open htmlcov/index.html

# Export the FastAPI OpenAPI schema to ../frontend/openapi.json.
# Run `just gen-api` in the frontend repo afterwards to regenerate TS types.
gen-api:
    PYTHONPATH=. uv run python scripts/export_openapi.py

# Pull & restart the backend on the VPS (see deploy-machineplay-backend in malganis).
deploy:
    ssh root@machineplay.org deploy-machineplay-backend

# Follow backend logs from the VPS.
logs:
    ssh -t root@machineplay.org 'journalctl -u machineplay -n 200 -f'
