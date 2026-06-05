"""SQLite data-access layer for the faculty research-alignment backend.

All SQL lives here. The Flask app opens a read-only connection; the enrichment
pipeline opens a single writable (WAL) connection. Storing every faculty record
behind this module means a future move to Postgres is a single-file change.

Faculty records round-trip to/from the same dict shape the app used with the
old JSON files: scalar columns map 1:1, JSON columns are parsed back to lists,
and (`department`, `department_label`) are populated from columns instead of
being merged in by the caller.
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get(
    "FACULTY_DB_PATH",
    os.path.join(os.path.dirname(__file__), "app.db"),
)
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

DEPT_LABELS = {
    "hwsph": "Herbert Wertheim School of Public Health",
    "sio": "Scripps Institution of Oceanography",
    "jacobs": "Jacobs School of Engineering",
}

# Scalar (text) columns that pass through unchanged.
_TEXT_FIELDS = [
    "first_name", "last_name", "title", "email",
    "research_interests", "research_interests_enriched",
    "profile_url", "orcid", "last_enriched",
    "employee_class", "job_code", "job_code_description",
    "vc_area", "division_school", "department_unit",
    "department_l2", "department_l3", "department_l4", "department_l5",
    "department_eah", "department_code", "eah_status", "subdepartment",
]
_INT_FIELDS = ["h_index", "pi_eligible"]
# Arrays / nested objects stored as JSON text.
_JSON_FIELDS = [
    "degrees", "expertise_keywords", "methodologies", "disease_areas",
    "populations", "committee_service", "integrity_flags",
    "funded_grants", "recent_publications",
]
# Every column written from a faculty record (excludes id/stable_key/department/
# department_label and the derived has_profile/grants_count/pubs_count/updated_at).
_RECORD_COLUMNS = _TEXT_FIELDS + _INT_FIELDS + _JSON_FIELDS


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def connect(readonly=True):
    """Open a SQLite connection with WAL + busy_timeout configured."""
    if readonly:
        if not os.path.exists(DB_PATH):
            raise FileNotFoundError(
                f"Faculty DB not found at {DB_PATH}. Run "
                "scripts/migrate_json_to_sqlite.py to create it."
            )
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False
        )
    else:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Read connections are per-thread (gunicorn worker threads each get their own,
# reused across requests). The writer is a single process-wide connection.
_local = threading.local()
_write_conn = None


def get_read_conn():
    conn = getattr(_local, "read_conn", None)
    if conn is None:
        conn = connect(readonly=True)
        _local.read_conn = conn
    return conn


def get_write_conn():
    global _write_conn
    if _write_conn is None:
        _write_conn = connect(readonly=False)
    return _write_conn


def init_schema(conn):
    """Apply schema.sql (idempotent)."""
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()


# ---------------------------------------------------------------------------
# Identity & record (de)serialization
# ---------------------------------------------------------------------------

def _slug(value):
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def compute_stable_key(department, record):
    """Deterministic identity for UPSERT, prefixed by department.

    Priority: orcid -> email -> name. Assigned once at first insert; later
    enrichment updates fields, not the key.
    """
    dept = department or "hwsph"
    orcid = (record.get("orcid") or "").strip()
    if orcid:
        return f"{dept}:orcid:{orcid}"
    email = (record.get("email") or "").strip().lower()
    if email:
        return f"{dept}:email:{email}"
    name = f"{_slug(record.get('last_name'))}|{_slug(record.get('first_name'))}"
    return f"{dept}:name:{name}"


def _has_profile(record):
    return 1 if (
        record.get("research_interests")
        or record.get("research_interests_enriched")
        or record.get("expertise_keywords")
    ) else 0


def _row_to_faculty(row, for_export=False):
    """Reconstruct a faculty dict in the original JSON record shape.

    Scalars that are NULL are omitted; JSON arrays default to []. When
    ``for_export`` is False the dict is tagged with department/department_label
    (matching the old in-app merge); for export those tags are dropped.
    """
    rec = {}
    for field in _TEXT_FIELDS:
        val = row[field]
        if val is not None:
            rec[field] = val
    if row["h_index"] is not None:
        rec["h_index"] = row["h_index"]
    if row["pi_eligible"] is not None:
        rec["pi_eligible"] = bool(row["pi_eligible"])
    for field in _JSON_FIELDS:
        raw = row[field]
        rec[field] = json.loads(raw) if raw else []
    if not for_export:
        rec["department"] = row["department"]
        rec["department_label"] = row["department_label"]
    return rec


def _record_values(record):
    """Serialize a faculty record dict to the column tuple for write."""
    values = []
    for field in _TEXT_FIELDS:
        values.append(record.get(field))
    for field in _INT_FIELDS:
        val = record.get(field)
        if field == "pi_eligible" and isinstance(val, bool):
            val = int(val)
        values.append(val)
    for field in _JSON_FIELDS:
        val = record.get(field)
        values.append(json.dumps(val, ensure_ascii=False) if val else None)
    return values


# ---------------------------------------------------------------------------
# Department filtering helpers
# ---------------------------------------------------------------------------

def _norm_dept(department):
    """None/'hwsph' -> 'hwsph'; 'all' -> None (no filter); else the dept key."""
    if department in (None, "hwsph"):
        return "hwsph"
    if department == "all":
        return None
    return department


def _dept_clause(department, alias="faculty"):
    dept = _norm_dept(department)
    if dept is None:
        return "", []
    return f" AND {alias}.department = ?", [dept]


# ---------------------------------------------------------------------------
# Full-text search maintenance
# ---------------------------------------------------------------------------

def _fts_texts(record):
    name = f"{record.get('first_name') or ''} {record.get('last_name') or ''}".strip()
    title = record.get("title") or ""
    research = " ".join(filter(None, [
        record.get("research_interests"),
        record.get("research_interests_enriched"),
    ]))
    keywords = " ".join(
        str(x) for field in ("expertise_keywords", "disease_areas",
                              "methodologies", "populations", "committee_service")
        for x in (record.get(field) or [])
    )
    return name, title, research, keywords


def reindex_faculty(conn, faculty_id, record):
    """Rebuild the FTS row for one faculty (call after every write)."""
    name, title, research, keywords = _fts_texts(record)
    conn.execute("DELETE FROM faculty_fts WHERE rowid = ?", (faculty_id,))
    conn.execute(
        "INSERT INTO faculty_fts(rowid, name, title, research, keywords) "
        "VALUES (?, ?, ?, ?, ?)",
        (faculty_id, name, title, research, keywords),
    )


_FTS_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _fts_query(terms, op="AND"):
    """Build a safe FTS5 MATCH expression of prefix terms."""
    tokens = []
    for term in terms:
        for tok in _FTS_TOKEN_RE.findall(str(term).lower()):
            if len(tok) >= 2:
                tokens.append(f"{tok}*")
    if not tokens:
        return None
    # De-duplicate while preserving order.
    seen = set()
    uniq = [t for t in tokens if not (t in seen or seen.add(t))]
    joiner = " OR " if op == "OR" else " "
    return joiner.join(uniq)


# ---------------------------------------------------------------------------
# Read path — directory & matching
# ---------------------------------------------------------------------------

def search_faculty(conn, department=None, query=None, limit=20, offset=0,
                   fields=None):
    """Directory search. Returns (results, total).

    Without a query: rows ordered by name. With a query: FTS5 prefix match
    (all terms required), ranked by relevance. ``fields`` whitelists the
    output keys (preserving FACULTY_DIRECTORY_FIELDS behavior).
    """
    dept_sql, dept_params = _dept_clause(department)
    name_guard = (" AND faculty.first_name IS NOT NULL AND faculty.first_name != ''"
                  " AND faculty.last_name IS NOT NULL AND faculty.last_name != ''")

    match = _fts_query([query], op="AND") if query else None

    if match:
        base = (" FROM faculty_fts JOIN faculty ON faculty.id = faculty_fts.rowid"
                " WHERE faculty_fts MATCH ?" + dept_sql + name_guard)
        params = [match] + dept_params
        total = conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
        rows = conn.execute(
            "SELECT faculty.*" + base + " ORDER BY bm25(faculty_fts) LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    else:
        base = " FROM faculty WHERE 1=1" + dept_sql + name_guard
        total = conn.execute("SELECT COUNT(*)" + base, dept_params).fetchone()[0]
        rows = conn.execute(
            "SELECT *" + base + " ORDER BY last_name, first_name LIMIT ? OFFSET ?",
            dept_params + [limit, offset],
        ).fetchall()

    results = []
    for row in rows:
        rec = _row_to_faculty(row)
        if fields is not None:
            rec = {k: rec[k] for k in fields if k in rec}
        results.append(rec)
    return results, total


def count_match_pool(conn, department=None):
    """Return (with_profile, without_profile) counts for a department."""
    dept_sql, dept_params = _dept_clause(department)
    row = conn.execute(
        "SELECT COALESCE(SUM(has_profile), 0), COUNT(*) "
        "FROM faculty WHERE 1=1" + dept_sql, dept_params,
    ).fetchone()
    with_profile, total = row[0], row[1]
    return with_profile, total - with_profile


def _load_records(conn, ids):
    """Load full faculty records for an ordered list of ids, preserving order."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM faculty WHERE id IN ({placeholders})", ids
    ).fetchall()
    by_id = {row["id"]: _row_to_faculty(row) for row in rows}
    return [by_id[i] for i in ids if i in by_id]


def fetch_match_candidates(conn, department, keywords, pool_with_profile=None,
                           limit=60):
    """Select up to ``limit`` matchable faculty for the LLM matcher.

    Mirrors the old _pre_filter_faculty: small pools return everyone; larger
    pools return the top candidates by FTS relevance, padded to ``limit`` with
    other matchable faculty so the matcher always sees a full slate.
    """
    dept_sql, dept_params = _dept_clause(department)
    if pool_with_profile is None:
        pool_with_profile, _ = count_match_pool(conn, department)

    match = _fts_query(keywords, op="OR")

    # Small pool or no usable keywords: return all matchable faculty.
    if pool_with_profile <= limit or not match:
        rows = conn.execute(
            "SELECT id FROM faculty WHERE has_profile = 1" + dept_sql
            + " ORDER BY last_name, first_name", dept_params,
        ).fetchall()
        return _load_records(conn, [r["id"] for r in rows])

    ranked = conn.execute(
        "SELECT faculty.id FROM faculty_fts JOIN faculty ON faculty.id = faculty_fts.rowid"
        " WHERE faculty_fts MATCH ? AND faculty.has_profile = 1" + dept_sql
        + " ORDER BY bm25(faculty_fts) LIMIT ?",
        [match] + dept_params + [limit],
    ).fetchall()
    ids = [r["id"] for r in ranked]

    if len(ids) < limit:
        placeholders = ",".join("?" * len(ids)) if ids else "0"
        pad = conn.execute(
            "SELECT id FROM faculty WHERE has_profile = 1" + dept_sql
            + f" AND id NOT IN ({placeholders})"
            " ORDER BY h_index DESC, last_name, first_name LIMIT ?",
            dept_params + ids + [limit - len(ids)],
        ).fetchall()
        ids.extend(r["id"] for r in pad)

    return _load_records(conn, ids)


# ---------------------------------------------------------------------------
# Write path — enrichment & seeding
# ---------------------------------------------------------------------------

def fetch_for_enrichment(conn, department=None):
    """Load all faculty for a department as dicts, each tagged with ``_db_id``.

    Returns the {"faculty": [...]} shape the pipeline expects.
    """
    dept_sql, dept_params = _dept_clause(department)
    rows = conn.execute(
        "SELECT * FROM faculty WHERE 1=1" + dept_sql
        + " ORDER BY id", dept_params,
    ).fetchall()
    faculty = []
    for row in rows:
        rec = _row_to_faculty(row)
        rec["_db_id"] = row["id"]
        faculty.append(rec)
    return {"faculty": faculty}


def save_faculty_record(conn, faculty_id, record):
    """Write one mutated record back by primary key (UPDATE + FTS reindex)."""
    set_cols = ", ".join(f"{c} = ?" for c in _RECORD_COLUMNS)
    values = _record_values(record)
    values += [
        _has_profile(record),
        len(record.get("funded_grants") or []),
        len(record.get("recent_publications") or []),
        datetime.now(timezone.utc).isoformat(),
        faculty_id,
    ]
    conn.execute(
        f"UPDATE faculty SET {set_cols}, has_profile = ?, grants_count = ?, "
        "pubs_count = ?, updated_at = ? WHERE id = ?",
        values,
    )
    reindex_faculty(conn, faculty_id, record)


def upsert_faculty(conn, department, record):
    """Insert or update a faculty row by stable_key (used by migration/seed).

    Returns the faculty id.
    """
    department = _norm_dept(department) or department
    stable_key = compute_stable_key(department, record)
    label = DEPT_LABELS.get(department, department)
    now = datetime.now(timezone.utc).isoformat()

    insert_cols = (["stable_key", "department", "department_label"]
                   + _RECORD_COLUMNS
                   + ["has_profile", "grants_count", "pubs_count", "updated_at"])
    values = ([stable_key, department, label] + _record_values(record)
              + [_has_profile(record),
                 len(record.get("funded_grants") or []),
                 len(record.get("recent_publications") or []),
                 now])
    placeholders = ",".join("?" * len(insert_cols))
    # On conflict keep the existing id/stable_key; refresh everything else.
    update_cols = [c for c in insert_cols if c != "stable_key"]
    update_sql = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    conn.execute(
        f"INSERT INTO faculty ({','.join(insert_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(stable_key) DO UPDATE SET {update_sql}",
        values,
    )
    faculty_id = conn.execute(
        "SELECT id FROM faculty WHERE stable_key = ?", (stable_key,)
    ).fetchone()[0]
    reindex_faculty(conn, faculty_id, record)
    return faculty_id


# ---------------------------------------------------------------------------
# Audit log & status
# ---------------------------------------------------------------------------

_LOG_COLUMNS = ["faculty_id", "stable_key", "source_name", "source_url",
                "field_updated", "old_value", "new_value", "confidence",
                "method", "raw_response", "retrieved_at"]


def append_log(conn, entries):
    """Append enrichment-log entries (list of dicts keyed by column name)."""
    if not entries:
        return
    placeholders = ",".join("?" * len(_LOG_COLUMNS))
    conn.executemany(
        f"INSERT INTO enrichment_log ({','.join(_LOG_COLUMNS)}) "
        f"VALUES ({placeholders})",
        [[e.get(c) for c in _LOG_COLUMNS] for e in entries],
    )


def rotate_log(conn, max_age_days=30):
    """Delete log entries older than max_age_days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    conn.execute("DELETE FROM enrichment_log WHERE retrieved_at < ?", (cutoff,))


def load_status(conn, department=None):
    """Enrichment coverage summary (replaces get_enrichment_status)."""
    dept_sql, dept_params = _dept_clause(department)
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN research_interests IS NOT NULL AND research_interests != '' THEN 1 ELSE 0 END) AS orig,"
        " SUM(CASE WHEN research_interests_enriched IS NOT NULL AND research_interests_enriched != '' THEN 1 ELSE 0 END) AS enriched,"
        " SUM(CASE WHEN grants_count > 0 THEN 1 ELSE 0 END) AS grants,"
        " SUM(CASE WHEN pubs_count > 0 THEN 1 ELSE 0 END) AS pubs"
        " FROM faculty WHERE 1=1" + dept_sql,
        dept_params,
    ).fetchone()
    total = row["total"] or 0
    orig = row["orig"] or 0
    enriched = row["enriched"] or 0
    return {
        "total_faculty": total,
        "with_original_interests": orig,
        "with_enriched_interests": enriched,
        "with_funded_grants": row["grants"] or 0,
        "with_publications": row["pubs"] or 0,
        "coverage_original": round(orig / total * 100, 1) if total else 0,
        "coverage_enriched": round(enriched / total * 100, 1) if total else 0,
    }


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default
