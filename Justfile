run:
    uv run uvicorn app.main:app --reload --timeout-graceful-shutdown 0

# Run a local docker registry (open, no auth) so the CLI/runner push & pull
# engine images without touching prod. Point them at it with
# MACHINEPLAY_REGISTRY=127.0.0.1:5000 (already set in machineplay/.env).
# Idempotent: restarts the existing container if it was created before.
registry:
    docker start machineplay-registry 2>/dev/null || \
        docker run -d --name machineplay-registry --restart unless-stopped \
            -p 5000:5000 -v machineplay-registry:/var/lib/registry registry:2

# Stop & remove the local registry container (its image volume persists).
registry-stop:
    -docker rm -f machineplay-registry

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
