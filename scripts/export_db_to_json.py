"""Export the SQLite faculty data back to the original JSON files.

Usage:
    python scripts/export_db_to_json.py [--db PATH] [--dept hwsph|sio|jacobs]

The SQLite database on the volume is the runtime source of truth; this script
writes human-readable, git-diffable snapshots (data/faculty.json, etc.) so the
existing provenance/review workflow keeps working. Faculty are stable-sorted by
last name then first name. Run it at the end of an enrichment run if you want
the JSON snapshots committed to git.
"""

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db

DEPT_FILES = {
    "hwsph": "faculty.json",
    "sio": "sio_faculty.json",
    "jacobs": "jacobs_faculty.json",
}
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _write_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def export_department(conn, dept):
    rows = conn.execute(
        "SELECT * FROM faculty WHERE department = ? ORDER BY last_name, first_name",
        (dept,),
    ).fetchall()
    faculty = [db._row_to_faculty(row, for_export=True) for row in rows]

    header = db.get_meta(conn, f"filemeta:{dept}", default={}) or {}
    out = dict(header)
    out["faculty"] = faculty

    path = os.path.join(DATA_DIR, DEPT_FILES[dept])
    _write_atomic(path, out)
    print(f"  {dept:8s} -> {DEPT_FILES[dept]} ({len(faculty)} faculty)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Override FACULTY_DB_PATH for this run.")
    parser.add_argument("--dept", choices=list(DEPT_FILES),
                        help="Export a single department (default: all).")
    args = parser.parse_args()

    if args.db:
        db.DB_PATH = args.db

    conn = db.connect(readonly=True)
    depts = [args.dept] if args.dept else list(DEPT_FILES)
    for dept in depts:
        export_department(conn, dept)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
