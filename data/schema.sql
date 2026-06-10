-- SQLite schema for the faculty research-alignment backend.
--
-- Design: scalar / queryable fields are real columns; long-tail arrays and
-- heterogeneous nested objects (grants, publications) are stored as JSON text
-- columns (json1). An FTS5 virtual table backs both the directory search and
-- the matcher's keyword pre-filter. All SQL lives behind data/db.py.

CREATE TABLE IF NOT EXISTS faculty (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,   -- surrogate key / FTS rowid
    stable_key       TEXT NOT NULL UNIQUE,                -- identity (orcid|email|name)
    department       TEXT NOT NULL,                       -- division slug (see data/divisions.py)
    department_label TEXT NOT NULL,

    -- scalar / queryable columns
    first_name                  TEXT,
    last_name                   TEXT,
    title                       TEXT,
    email                       TEXT,
    research_interests          TEXT,
    research_interests_enriched TEXT,
    profile_url                 TEXT,
    orcid                       TEXT,
    openalex_id                 TEXT,
    identity_status             TEXT NOT NULL DEFAULT 'unresolved',
        -- 'unresolved' | 'auto' | 'confirmed' | 'ambiguous' | 'not_found' | 'rejected'
    h_index                     INTEGER,
    citation_count              INTEGER,
    works_count                 INTEGER,
    raw_hash                    TEXT,                     -- LLM normalization skip fingerprint
    last_enriched               TEXT,                     -- ISO8601

    -- HR / org scalars
    employee_class       TEXT,
    job_code             TEXT,
    job_code_description TEXT,
    pi_eligible          INTEGER,                          -- 0 / 1
    vc_area              TEXT,
    division_school      TEXT,
    department_unit      TEXT,
    department_l2        TEXT,
    department_l3        TEXT,
    department_l4        TEXT,
    department_l5        TEXT,
    department_eah       TEXT,
    department_code      TEXT,
    eah_status           TEXT,
    subdepartment        TEXT,

    -- long-tail arrays and nested objects as JSON text
    degrees             TEXT,    -- JSON array
    expertise_keywords  TEXT,    -- JSON array
    methodologies       TEXT,    -- JSON array
    disease_areas       TEXT,    -- JSON array
    populations         TEXT,    -- JSON array
    committee_service   TEXT,    -- JSON array
    integrity_flags     TEXT,    -- JSON array
    funded_grants       TEXT,    -- JSON array of objects (heterogeneous keys)
    recent_publications TEXT,    -- JSON array of objects
    awards              TEXT,    -- JSON array of objects {name, year, granting_org, source}
    patents             TEXT,    -- JSON array of objects {title, patent_number, year, assignee}

    -- derived / denormalized helpers
    has_profile  INTEGER NOT NULL DEFAULT 0,   -- 1 if matchable (has interests/keywords)
    grants_count INTEGER NOT NULL DEFAULT 0,
    pubs_count   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_faculty_dept     ON faculty(department);
CREATE INDEX IF NOT EXISTS idx_faculty_lastname ON faculty(last_name, first_name);
CREATE INDEX IF NOT EXISTS idx_faculty_profile  ON faculty(department, has_profile);
CREATE INDEX IF NOT EXISTS idx_faculty_identity ON faculty(identity_status, pi_eligible);

-- Candidate external identities awaiting human review (identity resolution).
CREATE TABLE IF NOT EXISTS identity_candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id   INTEGER NOT NULL REFERENCES faculty(id) ON DELETE CASCADE,
    source       TEXT NOT NULL,            -- 'openalex' | 'orcid'
    external_id  TEXT NOT NULL,            -- author id or ORCID iD
    display_name TEXT,
    affiliation  TEXT,
    score        REAL NOT NULL,
    evidence     TEXT,                     -- JSON: name similarity, topics, counts
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_idcand_status ON identity_candidates(status, faculty_id);

-- Local lookup table for eScholarship (no author-search API; bulk-harvested
-- via OAI-PMH by enrichment/escholarship_harvest.py).
CREATE TABLE IF NOT EXISTS escholarship_pubs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    author_norm  TEXT NOT NULL,          -- normalized "last|first" key
    title        TEXT NOT NULL,
    year         INTEGER,
    journal      TEXT,
    doi          TEXT,
    source_url   TEXT,
    harvested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eschol_author ON escholarship_pubs(author_norm);

-- Full-text search index (rowid == faculty.id, maintained by data/db.py).
CREATE VIRTUAL TABLE IF NOT EXISTS faculty_fts USING fts5(
    name,        -- first + last
    title,
    research,    -- research_interests + research_interests_enriched
    keywords,    -- expertise_keywords + disease_areas + methodologies + populations + committee_service
    tokenize = 'porter unicode61'
);

-- Provenance / audit log (replaces enrichment_log.jsonl).
CREATE TABLE IF NOT EXISTS enrichment_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id    INTEGER REFERENCES faculty(id) ON DELETE SET NULL,
    stable_key    TEXT,
    source_name   TEXT,
    source_url    TEXT,
    field_updated TEXT,
    old_value     TEXT,
    new_value     TEXT,
    confidence    REAL,
    method        TEXT,
    raw_response  TEXT,
    retrieved_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_log_retrieved ON enrichment_log(retrieved_at);
CREATE INDEX IF NOT EXISTS idx_log_faculty   ON enrichment_log(faculty_id);

-- Key/value metadata: schema version, per-department source-file headers, etc.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Background jobs (enrichment runs, EAH reconciles) launched from the admin UI
-- or the in-process scheduler. Replaces the GitHub Actions run history.
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,          -- 'enrich' | 'eah_reconcile'
    params      TEXT,                   -- JSON
    status      TEXT NOT NULL,          -- 'queued' | 'running' | 'succeeded' | 'failed'
    trigger     TEXT,                   -- 'manual' | 'schedule'
    progress    TEXT,                   -- free-text, e.g. "42/99"
    result      TEXT,                   -- JSON summary
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
