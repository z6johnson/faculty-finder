#!/usr/bin/env sh
# Bootstraps the persistent /data volume, then starts the web server.
#
#   /data/state    writable copy of the faculty JSON (seeded from the image)
#   /data/private  the sensitive EAH extract (uploaded via the admin UI; gitignored)
#   /data/backups  dated JSON snapshots from export_db_to_json.py
#   /data/app.db   the runtime SQLite database (built from the seeded JSON)
#
# Everything mutable lives here so the image stays immutable and the whole
# state directory lift-and-shifts to the UCSD on-prem VM later.
set -e

STATE_DIR="${DATA_STATE_DIR:-/data/state}"
SEED_DIR="/app/data"

mkdir -p "$STATE_DIR" /data/private /data/backups

# Seed the writable faculty JSON from the image on first boot only. We never
# overwrite an existing volume copy (that would clobber live enrichment).
for f in faculty.json sio_faculty.json jacobs_faculty.json; do
    if [ ! -f "$STATE_DIR/$f" ] && [ -f "$SEED_DIR/$f" ]; then
        echo "[entrypoint] seeding $f into $STATE_DIR"
        cp "$SEED_DIR/$f" "$STATE_DIR/$f"
    fi
done

# Build the SQLite database from the seeded JSON if it does not exist yet.
if [ ! -f "${FACULTY_DB_PATH:-/data/app.db}" ]; then
    echo "[entrypoint] building SQLite database at ${FACULTY_DB_PATH:-/data/app.db}"
    python scripts/migrate_json_to_sqlite.py
fi

echo "[entrypoint] starting gunicorn (single writer worker)"
# A single worker owns all writes (admin actions + scheduler), so SQLite has
# one writer process; threads handle concurrent reads. WAL keeps reads flowing.
exec gunicorn app:app \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers 1 \
    --threads 8 \
    --timeout 120
