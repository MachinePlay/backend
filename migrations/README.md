# Migrations

One-off scripts that reshape existing MongoDB data when the models change.
Beanie has no built-in migration runner, so these are plain scripts, applied by
hand in numeric order.

## Convention

- One file per migration, named `NNN_short_description.py` (`001_`, `002_`, …).
  The numeric prefix is the apply order.
- Each targets whatever `MONGO_URL` / `MONGO_DB` resolve to (via env / `.env`),
  so point those at the database you mean to migrate.
- Write them **idempotent** — re-running must be a no-op, so a half-applied
  migration can be safely re-run.

## Running

From `backend/` (needs `PYTHONPATH=.` so `app` imports resolve):

```sh
PYTHONPATH=. uv run python migrations/001_uuid_to_link.py
```

## On production

The service runs from `/root/backend` with its env in
`/etc/machineplay/backend.env`. There are `Justfile` shortcuts (run from
`backend/`); back up first, pull the new code **without** restarting, migrate,
then restart onto the new code:

```sh
just backup                                                    # mongodump snapshot on the VPS
ssh root@machineplay.org 'cd /root/machineplay && git pull && cd /root/backend && git pull'
just migrate 001                                               # runs migrations/001_*.py on prod
ssh root@machineplay.org systemctl restart machineplay
```

Migrating before the restart avoids the window where new code would read
old-shaped documents. (`just deploy` pulls **and** restarts in one step, so
don't use it here — it leaves no gap to migrate in.)
