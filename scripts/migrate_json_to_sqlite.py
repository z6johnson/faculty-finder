"""One-off, idempotent migration of the faculty JSON files into SQLite.

Usage:
    python scripts/migrate_json_to_sqlite.py [--db PATH] [--rebuild]

Reads data/faculty.json, data/sio_faculty.json, data/jacobs_faculty.json and
populates the SQLite database (schema in data/schema.sql). Re-running is safe:
records UPSERT on their stable_key. ``--rebuild`` drops and recreates tables.

The per-department source-file headers (university, source_url, date_retrieved,
etc.) are preserved in the ``meta`` table so the data can be exported back to
the original JSON shape (scripts/export_db_to_json.py).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db

DEPT_FILES = {
    "hwsph": "faculty.json",
    "sio": "sio_faculty.json",
    "jacobs": "jacobs_faculty.json",
}
DATA_DIR = (os.environ.get("DATA_STATE_DIR", "").strip()
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))


def _drop_all(conn):
    for stmt in (
        "DROP TABLE IF EXISTS faculty_fts",
        "DROP TABLE IF EXISTS enrichment_log",
        "DROP TABLE IF EXISTS faculty",
        "DROP TABLE IF EXISTS meta",
    ):
        conn.execute(stmt)
    conn.commit()


def run_migration(rebuild=False, db_path=None):
    """Build/refresh the SQLite DB from the faculty JSON snapshots.

    Idempotent (records UPSERT on stable_key). Callable in-process by the
    entrypoint, the admin EAH upload handler, and the enrichment scheduler so
    the runtime DB stays in sync with the JSON state after writes.

    Returns a stats dict.
    """
    if db_path:
        db.DB_PATH = db_path

    conn = db.connect(readonly=False)
    if rebuild:
        _drop_all(conn)
    db.init_schema(conn)

    grand_total = 0
    per_dept = {}
    for dept, filename in DEPT_FILES.items():
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            print(f"  SKIP {dept}: {filename} not found")
            continue
        with open(path) as f:
            data = json.load(f)

        faculty = data.get("faculty", [])
        # Preserve the file-level header (everything except the faculty array).
        header = {k: v for k, v in data.items() if k != "faculty"}
        db.set_meta(conn, f"filemeta:{dept}", header)

        for record in faculty:
            db.upsert_faculty(conn, dept, record)
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM faculty WHERE department = ?", (dept,)
        ).fetchone()[0]
        grand_total += len(faculty)
        per_dept[dept] = count
        print(f"  {dept:8s} imported {len(faculty):5d} records  (rows in db: {count})")

    db.set_meta(conn, "schema_version", 1)
    conn.execute("ANALYZE")
    conn.commit()

    total_rows = conn.execute("SELECT COUNT(*) FROM faculty").fetchone()[0]
    fts_rows = conn.execute("SELECT COUNT(*) FROM faculty_fts").fetchone()[0]
    print(f"\nDone. {grand_total} records processed, {total_rows} faculty rows, "
          f"{fts_rows} FTS rows. DB at {db.DB_PATH}")
    fts_consistent = total_rows == fts_rows
    if not fts_consistent:
        print("WARNING: faculty row count != FTS row count!")
    conn.close()
    return {
        "processed": grand_total,
        "faculty_rows": total_rows,
        "fts_rows": fts_rows,
        "per_dept": per_dept,
        "fts_consistent": fts_consistent,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Override FACULTY_DB_PATH for this run.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Drop and recreate all tables before importing.")
    args = parser.parse_args()

    stats = run_migration(rebuild=args.rebuild, db_path=args.db)
    return 0 if stats["fts_consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())
