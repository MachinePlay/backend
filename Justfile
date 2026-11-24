run:
    uv run uvicorn app.main:app --reload --timeout-graceful-shutdown 0

# Local open registry on :5000 for the CLI and runner (machineplay/.env points
# at it). Reuses the container if it exists.
registry:
    docker start machineplay-registry 2>/dev/null || \
        docker run -d --name machineplay-registry --restart unless-stopped \
            -p 5000:5000 -v machineplay-registry:/var/lib/registry registry:2

# Remove the local registry container; its image volume persists.
registry-stop:
    -docker rm -f machineplay-registry

test:
    uv run pytest

coverage:
    uv run pytest --cov --cov-report=term-missing --cov-report=html
    xdg-open htmlcov/index.html

# Export the OpenAPI schema to ../frontend/openapi.json. Prefer `just gen-api`
# in the umbrella dir, which also regenerates the TS types.
gen-api:
    PYTHONPATH=. uv run python scripts/export_openapi.py

# Pull & restart the backend on the VPS.
deploy:
    ssh root@machineplay.org deploy-machineplay-backend

# Follow backend logs from the VPS.
logs:
    ssh -t root@machineplay.org 'journalctl -u machineplay -n 200 -f'

# mongodump prod into /root/mp-backup-<timestamp> on the VPS.
backup:
    ssh root@machineplay.org 'set -a; . /etc/machineplay/backend.env; set +a; mongodump --uri="$MONGO_URL/$MONGO_DB" --out "/root/mp-backup-$(date +%F-%H%M%S)"'

# Run a prod migration by number, e.g. `just migrate 001`. See migrations/README.md.
migrate num:
    ssh root@machineplay.org 'cd /root/backend; set -a; . /etc/machineplay/backend.env; set +a; PYTHONPATH=. uv run python migrations/{{num}}_*.py'
