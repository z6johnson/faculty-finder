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

from data import divisions

# Scalar (text) columns that pass through unchanged.
_TEXT_FIELDS = [
    "first_name", "last_name", "title", "email",
    "research_interests", "research_interests_enriched",
    "profile_url", "orcid", "openalex_id", "identity_status", "raw_hash",
    "last_enriched",
    "employee_class", "job_code", "job_code_description",
    "vc_area", "division_school", "department_unit",
    "department_l2", "department_l3", "department_l4", "department_l5",
    "department_eah", "department_code", "eah_status", "subdepartment",
]
_INT_FIELDS = ["h_index", "citation_count", "works_count", "pi_eligible"]
# Arrays / nested objects stored as JSON text.
_JSON_FIELDS = [
    "degrees", "expertise_keywords", "methodologies", "disease_areas",
    "populations", "committee_service", "integrity_flags",
    "funded_grants", "recent_publications", "awards", "patents",
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


# Columns added after the original schema shipped. Existing production DBs
# pick them up via ALTER TABLE (schema.sql only creates missing *tables*).
_FACULTY_COLUMN_MIGRATIONS = [
    ("openalex_id", "TEXT"),
    ("identity_status", "TEXT NOT NULL DEFAULT 'unresolved'"),
    ("citation_count", "INTEGER"),
    ("works_count", "INTEGER"),
    ("raw_hash", "TEXT"),
    ("awards", "TEXT"),
    ("patents", "TEXT"),
]


def _apply_migrations(conn):
    """Idempotently add columns that postdate an existing faculty table."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(faculty)")}
    if not existing:
        return  # fresh DB; schema.sql creates the full table
    for col, decl in _FACULTY_COLUMN_MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE faculty ADD COLUMN {col} {decl}")


def init_schema(conn):
    """Apply schema.sql plus column migrations (idempotent)."""
    _apply_migrations(conn)
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
    for field in _INT_FIELDS:
        if row[field] is not None:
            rec[field] = bool(row[field]) if field == "pi_eligible" else row[field]
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
        val = record.get(field)
        if field == "identity_status" and not val:
            val = "unresolved"  # column is NOT NULL
        values.append(val)
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
    """None/'all'/'' -> None (no filter); else the division slug.

    Historically None meant 'hwsph'; callers now pass explicit slugs, and an
    absent department means "every division" so new-division rows are never
    silently scoped to public health.
    """
    if department in (None, "", "all"):
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


# Faculty flagged out of the EAH reconcile stay in the DB (purges are
# admin-confirmed) but are excluded from the public matching pool.
_INACTIVE_STATUSES = ("Inactive", "Duplicate")
_ACTIVE_SQL = (" AND (faculty.eah_status IS NULL OR faculty.eah_status NOT IN "
               + "(" + ",".join(f"'{s}'" for s in _INACTIVE_STATUSES) + "))")


def count_match_pool(conn, department=None):
    """Return (with_profile, without_profile) counts for a department."""
    dept_sql, dept_params = _dept_clause(department)
    row = conn.execute(
        "SELECT COALESCE(SUM(has_profile), 0), COUNT(*) "
        "FROM faculty WHERE 1=1" + dept_sql + _ACTIVE_SQL, dept_params,
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
            "SELECT id FROM faculty WHERE has_profile = 1" + dept_sql + _ACTIVE_SQL
            + " ORDER BY last_name, first_name", dept_params,
        ).fetchall()
        return _load_records(conn, [r["id"] for r in rows])

    ranked = conn.execute(
        "SELECT faculty.id FROM faculty_fts JOIN faculty ON faculty.id = faculty_fts.rowid"
        " WHERE faculty_fts MATCH ? AND faculty.has_profile = 1" + dept_sql + _ACTIVE_SQL
        + " ORDER BY bm25(faculty_fts) LIMIT ?",
        [match] + dept_params + [limit],
    ).fetchall()
    ids = [r["id"] for r in ranked]

    if len(ids) < limit:
        placeholders = ",".join("?" * len(ids)) if ids else "0"
        pad = conn.execute(
            "SELECT id FROM faculty WHERE has_profile = 1" + dept_sql + _ACTIVE_SQL
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
        rec["_stable_key"] = row["stable_key"]
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


def update_faculty_division(conn, faculty_id, slug, label):
    """Move a faculty row to another division (EAH says they transferred)."""
    conn.execute(
        "UPDATE faculty SET department = ?, department_label = ?, updated_at = ?"
        " WHERE id = ?",
        (slug, label, _now_iso(), faculty_id),
    )


def mark_eah_status(conn, faculty_id, status):
    """Soft-flag a row's employment status (e.g. 'Inactive', 'Duplicate')."""
    conn.execute(
        "UPDATE faculty SET eah_status = ?, updated_at = ? WHERE id = ?",
        (status, _now_iso(), faculty_id),
    )


def purge_flagged_faculty(conn):
    """Admin-confirmed removal of rows flagged Inactive/Duplicate by the EAH
    reconcile. Returns the number of rows deleted."""
    placeholders = ",".join("?" * len(_INACTIVE_STATUSES))
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM faculty WHERE eah_status IN ({placeholders})",
        _INACTIVE_STATUSES,
    ).fetchall()]
    for fid in ids:
        conn.execute("DELETE FROM faculty_fts WHERE rowid = ?", (fid,))
        conn.execute("DELETE FROM faculty WHERE id = ?", (fid,))
    return len(ids)


def upsert_faculty(conn, department, record):
    """Insert or update a faculty row by stable_key (used by migration/seed).

    Returns the faculty id.
    """
    if not department:
        raise ValueError("upsert_faculty requires an explicit division slug")
    stable_key = compute_stable_key(department, record)
    label = record.get("department_label") or divisions.label_for(department)
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


# ---------------------------------------------------------------------------
# Schema bootstrap (idempotent) — lets a running app pick up new tables (jobs)
# without a manual migration.
# ---------------------------------------------------------------------------

def ensure_schema():
    """Apply schema.sql against the live DB (CREATE ... IF NOT EXISTS)."""
    conn = connect(readonly=False)
    try:
        init_schema(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Background jobs
#
# Status writes each open a short-lived writable connection: the job runner
# thread and request threads can write concurrently without sharing a single
# connection object (WAL + busy_timeout serialize them safely). Reads use the
# caller's read-only connection.
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_job(kind, params=None, trigger="manual"):
    conn = connect(readonly=False)
    try:
        cur = conn.execute(
            "INSERT INTO jobs (kind, params, status, trigger, created_at) "
            "VALUES (?, ?, 'queued', ?, ?)",
            (kind, json.dumps(params or {}), trigger, _now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def start_job(job_id):
    _job_set(job_id, "UPDATE jobs SET status='running', started_at=? WHERE id=?",
             (_now_iso(), job_id))


def set_job_progress(job_id, progress):
    _job_set(job_id, "UPDATE jobs SET progress=? WHERE id=?", (progress, job_id))


def finish_job(job_id, status, result=None, error=None):
    _job_set(
        job_id,
        "UPDATE jobs SET status=?, result=?, error=?, finished_at=? WHERE id=?",
        (status, json.dumps(result) if result is not None else None,
         error, _now_iso(), job_id),
    )


def fail_stale_jobs():
    """Mark jobs left 'queued'/'running' by a crash/restart as failed."""
    conn = connect(readonly=False)
    try:
        conn.execute(
            "UPDATE jobs SET status='failed', error='interrupted by restart', "
            "finished_at=? WHERE status IN ('queued','running')",
            (_now_iso(),),
        )
        conn.commit()
    finally:
        conn.close()


def _job_set(job_id, sql, params):
    conn = connect(readonly=False)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def get_job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(conn, limit=25):
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def recent_enrichment_log(conn, limit=50):
    rows = conn.execute(
        "SELECT retrieved_at, source_name, field_updated, method, confidence,"
        " stable_key FROM enrichment_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Admin faculty curation
# ---------------------------------------------------------------------------

def admin_list_faculty(conn, department=None, query=None, limit=50, offset=0):
    """Lightweight list for the curate UI: includes id/stable_key for editing."""
    dept_sql, dept_params = _dept_clause(department)
    match = _fts_query([query], op="AND") if query else None
    cols = ("faculty.id, faculty.stable_key, faculty.department, faculty.first_name,"
            " faculty.last_name, faculty.title, faculty.email, faculty.eah_status,"
            " faculty.last_enriched")
    if match:
        base = (" FROM faculty_fts JOIN faculty ON faculty.id = faculty_fts.rowid"
                " WHERE faculty_fts MATCH ?" + dept_sql)
        params = [match] + dept_params
        total = conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {cols}" + base + " ORDER BY bm25(faculty_fts) LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    else:
        base = " FROM faculty WHERE 1=1" + dept_sql
        total = conn.execute("SELECT COUNT(*)" + base, dept_params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {cols}" + base + " ORDER BY last_name, first_name LIMIT ? OFFSET ?",
            dept_params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def admin_get_faculty(conn, faculty_id):
    """Full record for one faculty, including id/stable_key/department."""
    row = conn.execute("SELECT * FROM faculty WHERE id=?", (faculty_id,)).fetchone()
    if not row:
        return None
    rec = _row_to_faculty(row)
    rec["id"] = row["id"]
    rec["stable_key"] = row["stable_key"]
    rec["department"] = row["department"]
    return rec


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def fetch_identity_queue(conn, department=None, pi_only=False, statuses=("unresolved",),
                         limit=None):
    """Faculty awaiting identity resolution, tagged with ``_db_id``."""
    dept_sql, params = _dept_clause(department)
    status_sql = ",".join("?" * len(statuses))
    sql = (f"SELECT * FROM faculty WHERE identity_status IN ({status_sql})"
           + dept_sql)
    params = list(statuses) + params
    if pi_only:
        sql += " AND pi_eligible = 1"
    sql += " ORDER BY pi_eligible DESC, id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        rec = _row_to_faculty(row)
        rec["_db_id"] = row["id"]
        rec["_stable_key"] = row["stable_key"]
        out.append(rec)
    return out


def set_identity_status(conn, faculty_id, status):
    conn.execute("UPDATE faculty SET identity_status = ?, updated_at = ? WHERE id = ?",
                 (status, _now_iso(), faculty_id))


def insert_identity_candidates(conn, faculty_id, candidates):
    """Store candidate external identities for admin review."""
    now = _now_iso()
    conn.executemany(
        "INSERT INTO identity_candidates"
        " (faculty_id, source, external_id, display_name, affiliation, score,"
        "  evidence, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        [(faculty_id, c["source"], c["external_id"], c.get("display_name"),
          c.get("affiliation"), c["score"],
          json.dumps(c.get("evidence") or {}, ensure_ascii=False), now)
         for c in candidates],
    )


def clear_identity_candidates(conn, faculty_id):
    """Drop pending candidates before a re-run for the same faculty."""
    conn.execute(
        "DELETE FROM identity_candidates WHERE faculty_id = ? AND status = 'pending'",
        (faculty_id,),
    )


def list_identity_candidates(conn, status="pending", department=None, limit=200):
    """Pending candidates grouped for the review queue, newest first."""
    dept_sql, dept_params = _dept_clause(department, alias="f")
    rows = conn.execute(
        "SELECT c.*, f.first_name AS f_first, f.last_name AS f_last,"
        " f.title AS f_title, f.department AS f_department,"
        " f.division_school AS f_division_school, f.email AS f_email"
        " FROM identity_candidates c JOIN faculty f ON f.id = c.faculty_id"
        " WHERE c.status = ?" + dept_sql +
        " ORDER BY c.faculty_id, c.score DESC LIMIT ?",
        [status] + dept_params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def get_identity_candidate(conn, candidate_id):
    row = conn.execute(
        "SELECT * FROM identity_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    return dict(row) if row else None


def decide_identity_candidate(conn, candidate_id, accept):
    """Accept/reject one candidate. Accepting writes the external id onto the
    faculty row (orcid only if currently empty), marks the faculty 'confirmed',
    and rejects sibling candidates."""
    cand = get_identity_candidate(conn, candidate_id)
    if not cand or cand["status"] != "pending":
        return None
    now = _now_iso()
    new_status = "accepted" if accept else "rejected"
    conn.execute(
        "UPDATE identity_candidates SET status = ?, decided_at = ? WHERE id = ?",
        (new_status, now, candidate_id),
    )
    if not accept:
        return cand

    fid = cand["faculty_id"]
    if cand["source"] == "openalex":
        conn.execute(
            "UPDATE faculty SET openalex_id = COALESCE(openalex_id, ?) WHERE id = ?",
            (cand["external_id"], fid),
        )
        evidence = json.loads(cand["evidence"] or "{}")
        if evidence.get("orcid"):
            conn.execute(
                "UPDATE faculty SET orcid = COALESCE(orcid, ?) WHERE id = ?",
                (evidence["orcid"], fid),
            )
    elif cand["source"] == "orcid":
        conn.execute(
            "UPDATE faculty SET orcid = COALESCE(orcid, ?) WHERE id = ?",
            (cand["external_id"], fid),
        )
    set_identity_status(conn, fid, "confirmed")
    conn.execute(
        "UPDATE identity_candidates SET status = 'rejected', decided_at = ?"
        " WHERE faculty_id = ? AND status = 'pending' AND id != ?",
        (now, fid, candidate_id),
    )
    return cand


def reject_faculty_identity(conn, faculty_id):
    """Mark a faculty as not findable anywhere; excluded from enrichment."""
    now = _now_iso()
    conn.execute(
        "UPDATE identity_candidates SET status = 'rejected', decided_at = ?"
        " WHERE faculty_id = ? AND status = 'pending'",
        (now, faculty_id),
    )
    set_identity_status(conn, faculty_id, "rejected")


# ---------------------------------------------------------------------------
# Backfill selection & division coverage
# ---------------------------------------------------------------------------

def fetch_backfill_candidates(conn, pi_only=False, limit=None):
    """Identity-resolved, never-enriched faculty in priority order."""
    sql = ("SELECT * FROM faculty"
           " WHERE last_enriched IS NULL"
           " AND identity_status IN ('auto', 'confirmed')")
    params = []
    if pi_only:
        sql += " AND pi_eligible = 1"
    sql += " ORDER BY pi_eligible DESC, department, id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        rec = _row_to_faculty(row)
        rec["_db_id"] = row["id"]
        rec["_stable_key"] = row["stable_key"]
        out.append(rec)
    return out


def load_status_by_division(conn):
    """Per-division coverage for the admin dashboard."""
    rows = conn.execute(
        "SELECT department, department_label, COUNT(*) AS total,"
        " SUM(CASE WHEN identity_status IN ('auto','confirmed') THEN 1 ELSE 0 END) AS resolved,"
        " SUM(CASE WHEN identity_status = 'ambiguous' THEN 1 ELSE 0 END) AS ambiguous,"
        " SUM(CASE WHEN identity_status = 'not_found' THEN 1 ELSE 0 END) AS not_found,"
        " SUM(has_profile) AS with_profile,"
        " SUM(CASE WHEN last_enriched IS NOT NULL THEN 1 ELSE 0 END) AS enriched,"
        " SUM(CASE WHEN pi_eligible = 1 THEN 1 ELSE 0 END) AS pi_eligible"
        " FROM faculty GROUP BY department ORDER BY total DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        total = d["total"] or 0
        d["pct_resolved"] = round((d["resolved"] or 0) / total * 100, 1) if total else 0
        d["pct_enriched"] = round((d["enriched"] or 0) / total * 100, 1) if total else 0
        out.append(d)
    return out
