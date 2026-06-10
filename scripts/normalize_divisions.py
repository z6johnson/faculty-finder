#!/usr/bin/env python3
"""One-time normalization of department slugs for EAH-seeded faculty rows.

Usage:
    python scripts/normalize_divisions.py [--db PATH] [--dry-run]

Sets faculty.department / department_label from the row's EAH
``division_school`` value via the registry in data/divisions.py. Rows whose
slug already matches are untouched; stable_key is never rewritten (key
prefixes are historical identity, not semantics).
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db
from data.divisions import division_for


def normalize(dry_run=False):
    conn = db.connect(readonly=False)
    db.init_schema(conn)

    rows = conn.execute(
        "SELECT id, department, division_school FROM faculty"
    ).fetchall()
    changes = Counter()
    for row in rows:
        slug, label, _ = division_for(row["division_school"])
        if not row["division_school"]:
            # No EAH division on the row (e.g. legacy seed rows) — keep the
            # existing slug rather than dumping everyone into "other".
            continue
        if slug == row["department"]:
            continue
        changes[f"{row['department']} -> {slug}"] += 1
        if not dry_run:
            db.update_faculty_division(conn, row["id"], slug, label)

    if not dry_run:
        conn.commit()
    conn.close()

    total = sum(changes.values())
    print(f"{'Would update' if dry_run else 'Updated'} {total} of {len(rows)} rows:")
    for move, count in changes.most_common():
        print(f"  {count:5d}  {move}")
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Override FACULTY_DB_PATH for this run.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.db:
        db.DB_PATH = args.db
    normalize(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
