# Migrations

Plain scripts, applied by hand in numeric order (`NNN_description.py`). Each
targets whatever `MONGO_URL` / `MONGO_DB` resolve to, and must be
**idempotent** so a half-applied run can be repeated.

Locally, from `backend/`:

```sh
PYTHONPATH=. uv run python migrations/001_uuid_to_link.py
```

## On production

Back up, pull the new code **without** restarting, migrate, then restart:

```sh
just backup                                                    # mongodump snapshot on the VPS
ssh root@machineplay.org 'cd /root/machineplay && git pull && cd /root/backend && git pull'
just migrate 001                                               # runs migrations/001_*.py on prod
ssh root@machineplay.org systemctl restart machineplay
```

Don't use `just deploy` here: it pulls and restarts in one step, so new code
would read old-shaped documents.
