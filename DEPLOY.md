# Deployment

The runtime data backend is **SQLite** (`data/app.db`, FTS5-indexed). The web
process opens it **read-only**; data changes happen only in the batch enrichment
job. All SQL lives in `data/db.py`.

## One-off: build the database

```bash
python scripts/migrate_json_to_sqlite.py        # JSON -> data/app.db (idempotent)
```

Re-running is safe (records UPSERT on a stable identity key). `--rebuild` drops
and recreates the tables. Override the location with `FACULTY_DB_PATH`.

## Recommended: Railway (persistent host + volume + cron)

Serverless (Vercel) can't host a writable SQLite, so deploy to a persistent
host. One Railway **service** owns a **volume**; the web process reads the DB and
a **cron trigger on the same service** runs the weekly enrichment (the sole
writer). WAL mode lets reads continue while the cron writes.

1. **Create the service** from this repo. Start command (also in `Procfile`):
   ```
   gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
   ```
2. **Add a volume** mounted at `/data`.
3. **Environment variables:**
   - `FACULTY_DB_PATH=/data/app.db`
   - `LITELLM_API_KEY`, `LITELLM_API_BASE`, `LITELLM_MODEL`
   - `NCBI_API_KEY`, `S2_API_KEY` (optional)
4. **Bootstrap the DB once** against the volume (Railway one-off command / shell):
   ```
   FACULTY_DB_PATH=/data/app.db python scripts/migrate_json_to_sqlite.py
   ```
5. **Schedule enrichment** with a Railway Cron on the same service, mirroring the
   old GitHub Actions cadence (one school per slot, Sun 00:00 / 02:00 / 04:00 UTC):
   ```
   ENRICH_DEPARTMENT=hwsph python enrichment/run.py     # (sio / jacobs for the others)
   ```
   The cron writes `/data/app.db`; WAL keeps the web workers serving reads.
   Optionally append `python scripts/export_db_to_json.py` to keep git-diffable
   JSON snapshots.

### Concurrency & backups
- WAL mode is set automatically; the web process opens the DB read-only, which
  enforces the "no request-time writes" invariant. A single cron writer + many
  readers never block (a 5s `busy_timeout` covers checkpoints).
- Nightly online backup (WAL-safe): `sqlite3 /data/app.db ".backup /data/backups/app-$(date +%F).db"`.
- The committed JSON snapshots double as a portable cold backup.

## Render (blueprint: `render.yaml`)

Render disks **cannot be shared between services**, so the Railway "web reads +
cron writes one shared volume" model doesn't translate directly. Because the
runtime is read-only, the simplest correct pattern is to **build `data/app.db`
from the committed JSON at deploy time** (no disk, never committed) and keep
enrichment in GitHub Actions:

1. Merge the SQLite PR into the `render-sqlite` branch, then connect a Render
   Blueprint to this repo — it reads `render.yaml`.
2. The web service's build runs `pip install -r requirements.txt && python
   scripts/migrate_json_to_sqlite.py`, producing a read-only `app.db` in the
   deploy. `startCommand` is gunicorn.
3. Set the secrets marked `sync: false` in `render.yaml` (`LITELLM_*`,
   `NCBI_API_KEY`, `S2_API_KEY`) in the Render dashboard.
4. Data refreshes whenever updated JSON lands on `render-sqlite` (via the
   GitHub Actions enrichment loop, or a merge from `main`); Render auto-redeploys
   and rebuilds the DB.

To make Render own enrichment on-host instead, give the web service a persistent
disk (mount `/data`, set `FACULTY_DB_PATH=/data/app.db`) and run the enrichment
on a schedule **inside** the web service (a background scheduler), since a
separate Render Cron Job can't write the web service's disk.

## Other alternatives
- **Fly.io + LiteFS** — only if you need multi-region read replicas / HA.
- **Neon / Supabase (managed Postgres)** — the next step *after* SQLite if you
  outgrow a single node (concurrent writers, managed backups, or Supabase auth).
  Because all SQL is behind `data/db.py`, that swap is localized to one module.

## GitHub Actions (build-time path, still supported)
`.github/workflows/enrich.yml` runs a self-contained loop: rebuild the DB from
the committed JSON, enrich, export back to JSON, and commit. Use this if you
prefer git-as-provenance with a serverless frontend; use the Railway cron above
if the volume DB is your source of truth.
