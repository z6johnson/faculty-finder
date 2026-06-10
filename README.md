# Research Alignment

AI-powered faculty expertise discovery tool for UC San Diego. Covers three schools — Herbert Wertheim School of Public Health (HWSPH), Scripps Institution of Oceanography (SIO), and Jacobs School of Engineering. Identify faculty whose research expertise aligns with funding opportunity requirements through three interaction modes.

## How It Works

Three ways to discover aligned faculty:

1. **Upload a funding opportunity** (PDF or TXT) — AI extracts requirements and ranks faculty by alignment
2. **Enter expertise requirements** manually — paste or describe the expertise you need and get ranked matches
3. **Browse the expert directory** — search and filter faculty by expertise, methods, disease areas, and populations

Two sequential LLM calls power the matching analysis via [LiteLLM](https://github.com/BerriAI/litellm):
- **Call 1 (extraction):** Parse the funding opportunity text into structured requirements — grant title, agency, summary, investigator roles with expertise areas/qualifications/constraints, and overall research themes.
- **Call 2 (matching):** Evaluate each faculty member's enriched profile against the extracted requirements across three scored dimensions (expertise alignment, methodological fit, track record), then return up to 15 ranked matches with reasoning.

For large faculty sets (60+), a keyword pre-filter scores each faculty member's text overlap against extracted requirement keywords and passes the top 60 candidates to the LLM. Sets of 60 or fewer skip this step entirely.

## Ranking Methodology

### Stage 1: Requirement Extraction

The uploaded document (or manually entered text) is sent to the LLM with `temperature=0` and a system prompt that instructs it to extract:

| Field | Description |
|-------|-------------|
| `grant_title` | Title of the funding opportunity |
| `funding_agency` | Sponsoring agency |
| `grant_summary` | 2–3 sentence summary of purpose, scope, and what the funder seeks |
| `investigator_requirements[]` | Per-role breakdown: role name, expertise areas, qualifications, constraints |
| `overall_research_themes[]` | Broad research themes spanning the opportunity |

The LLM uses the document's own terminology for roles (e.g., "Lead Investigator", "Project Director") and never invents requirements not stated or clearly implied. If no roles are explicitly defined, all requirements are grouped under a single "Investigator" entry. The response is parsed as JSON with fallback handling for markdown fences, truncated output, and wrapper objects.

### Stage 2: Faculty Matching & Scoring

A second LLM call (`temperature=0.05`) receives the extracted requirements alongside a compact summary of each eligible faculty member. For each candidate, it evaluates three dimensions on a 0–100 integer scale:

| Dimension | Weight | What it measures |
|-----------|--------|------------------|
| **Expertise alignment** | Highest | How closely the faculty member's research interests, expertise keywords, and funded project history match the required expertise areas and research themes |
| **Methodological fit** | Medium | Whether the researcher's methods (inferred from publications, MeSH terms, funded work descriptions) align with the opportunity's methodological needs |
| **Track record** | Lower | Strength of publication count, h-index, and funding history relative to the opportunity's scope |

The **overall match score (0–100)** is a weighted synthesis produced by the LLM itself — expertise alignment is weighted most heavily, followed by methodological fit, then track record. No post-processing, normalization, or re-weighting is applied after the model returns scores. The LLM also generates a 2–3 sentence `match_reasoning` for each match explaining the specific alignment.

**Filtering rules applied by the LLM prompt:**
- Only faculty with `match_score >= 40` are included
- At most 15 matches are returned
- Results are ordered by `match_score` descending

### What the LLM Sees Per Faculty Member

Each faculty member is serialized as a single line containing:

```
ID:{index} | {name}, {degrees} | {title} | Interests: {enriched_interests, truncated to 300 chars}
  | Keywords: {top 8 expertise keywords} | Funded projects: {count} | h-index: {value}
```

The LLM does not see full abstracts, complete publication lists, or grant dollar amounts during matching — those details inform the enriched profile narrative that the LLM reads.

### Keyword Pre-Filter (Large Faculty Sets)

When the eligible faculty set exceeds 60 members, a keyword pre-filter runs before the LLM stage to reduce token costs:

1. Extracts keywords from `overall_research_themes` and `investigator_requirements[].expertise_areas` in the extracted requirements
2. Expands multi-word terms into individual words (keeping only words > 3 characters)
3. Scores each faculty member by counting how many requirement keywords appear in their searchable text (enriched interests, original interests, expertise keywords, disease areas, methodologies, populations)
4. Passes the top 60 faculty by keyword score to the LLM

Faculty sets of 60 or fewer bypass this filter entirely — every eligible member goes directly to the LLM.

### Frontend Score Display

Each match card in the results view shows:

| Element | Details |
|---------|---------|
| **Rank** | Position (1–15) based on overall score |
| **Overall score** | 0–100 with color-coded bar: green ≥ 80, yellow 60–79, red < 60 |
| **Sub-scores** | Expertise, Methods, Track Record displayed individually |
| **Match reasoning** | LLM-generated 2–3 sentence explanation |
| **Research interests** | Original directory text for the faculty member |
| **Contact** | Email link |

The "How rankings are calculated" panel below the results provides the full methodology disclosure to end users.

### Model Configuration

Both LLM calls use the same model via LiteLLM (default: `openai/api-gpt-oss-120b`). The extraction call uses `max_tokens=2000`; the matching call uses `max_tokens=4000`. JSON mode is requested when the model supports it; the system retries without it if unsupported. Each call retries once on parse failure (1-second backoff).

## Enrichment Pipeline

Faculty records are seeded from the Employee Activity Hub (EAH) HR extract for
**every UCSD division** (~5,500 active academics) and enriched from free/open
academic data sources. Three stages take a row from bare HR data to a
matchable profile:

1. **Identity resolution** (`enrichment/identity.py`, job kind
   `identity_resolve`) — resolves each person to an OpenAlex author id (and
   ORCID when known) using name search constrained to the UCSD institution
   (ROR `0168r3w48`), scored on name similarity, affiliation currency, and
   topic↔division consistency. Confident matches are auto-accepted; ambiguous
   ones land in the admin **Identity review** queue; misses are marked
   `not_found` and retried weekly.
2. **Enrichment** (`enrichment/pipeline.py`) — fetches from the division's
   source bundle, merges fields, and LLM-normalizes the profile.
3. **Backfill** (job kind `backfill`) — nightly batches enrich
   identity-resolved, never-enriched faculty, PI-eligible rows first.

Faculty without an enriched profile stay out of the public matching pool
(`has_profile` gating), as do rows soft-flagged `Inactive` by the EAH
reconcile.

### Data Sources

Every division gets the **core** sources; discipline-specific extras are
routed per division in `enrichment/routing.py` (division slugs come from the
EAH `Division / School` value via `data/divisions.py`).

#### Core (all divisions)

| Source | Confidence | API | Auth | Fields Provided |
|--------|-----------|-----|------|-----------------|
| **OpenAlex** | 0.85 | REST (`api.openalex.org`) | Optional `OPENALEX_MAILTO` (polite pool) | `openalex_id`, `orcid`, `h_index`, `citation_count`, `works_count`, `recent_publications`, `expertise_keywords` |
| **ORCID** | 0.9 | REST (`pub.orcid.org/v3.0`) | No | `orcid`, `recent_publications`, `funded_grants`, `awards` |
| **UCSD Profiles** | 1.0 | Web scrape (`profiles.ucsd.edu`) | No | `research_interests_enriched`, `profile_url` |
| **Wikidata** | 0.8 | SPARQL | No (only runs when ORCID known) | `awards` (honors, society memberships) |
| **Email pattern** | 0.5 | Inference | No | `email` fallback |

#### Discipline extras by division

| Division(s) | Extra sources |
|-------------|---------------|
| hwsph | PubMed, NIH RePORTER, Semantic Scholar, ClinicalTrials.gov |
| sio | Scripps Profiles, NSF Awards, NIH RePORTER, PubMed, Semantic Scholar |
| jacobs | NSF, NIH, PubMed, Semantic Scholar, DBLP, arXiv, PatentsView |
| som, skaggs | PubMed, NIH RePORTER, ClinicalTrials.gov, PatentsView |
| bio-sci | PubMed, NIH, NSF, PatentsView |
| phys-sci | NSF, arXiv, NASA ADS, PatentsView, Crossref |
| soc-sci | NSF, NIH, RePEc, Crossref |
| arts-hum | Crossref, eScholarship |
| rady, gps | RePEc, NSF, Crossref |
| (anything else) | Crossref, NSF |

| Source | Confidence | Auth | Notes |
|--------|-----------|------|-------|
| **NIH RePORTER** | 0.8 | No | Federal grant records |
| **NSF Awards** | 0.8 | No | Federal grant records |
| **PatentsView (USPTO)** | 0.8 | Free key `PATENTSVIEW_API_KEY` | Patents; assignee must be the UC Regents |
| **ClinicalTrials.gov v2** | 0.8 | No | Trials where the person is an overall official with a UCSD affiliation |
| **NASA ADS** | 0.8 | Free key `ADS_API_KEY` | Physics/astronomy literature |
| **DBLP** | 0.75 | No | CS bibliography; requires an unambiguous author |
| **eScholarship** | 0.75 | No | UC open-access repository; bulk OAI-PMH harvest (`escholarship_harvest` job) into a local lookup table |
| **Semantic Scholar** | 0.75 | Optional `S2_API_KEY` | Legacy bundles only |
| **PubMed** | 0.7 | Optional `NCBI_API_KEY` | Biomedical literature |
| **Crossref** | 0.7 | Optional mailto | Funder acknowledgements looked up by DOI (from OpenAlex works) |
| **RePEc/IDEAS** | 0.7 | Free code `REPEC_API_CODE` | Economics/management |
| **arXiv** | 0.65 | No | Preprints; exact-name match required |

### Source Details

**UCSD Profiles / Scripps Profiles** — Scrapes `profiles.ucsd.edu` by searching for the faculty member's name, then parses the profile page for research descriptions, overview sections, and biography text. UCSD Profiles falls back to the HWSPH faculty directory page if the profile search fails. Scripps Profiles additionally tries `scripps.ucsd.edu/profiles/{slug}` with multiple slug patterns.

**NIH RePORTER** — Queries the NIH RePORTER v2 projects search endpoint by PI name + "UNIVERSITY OF CALIFORNIA SAN DIEGO" organization filter. Returns up to 25 projects sorted by start date (descending). Extracts: grant title, abstract (truncated to 500 chars), funding agency/institute, award amount, start/end dates, project number, and co-PI names.

**NSF Awards** — Queries the NSF Award Search API by PI name + "University of California San Diego" awardee filter. Returns up to 25 awards. Extracts: title, program name, abstract (truncated to 500 chars), obligated funds, start/end dates, award ID, and co-PIs.

**PubMed** — Two-step process using NCBI E-utilities. First, `esearch` finds PMIDs matching `{LastName} {FirstInitial}[Author] AND "University of California San Diego"[Affiliation]`, returning the 20 most recent. Then `efetch` retrieves article details in XML. Extracts: title, year, journal, MeSH terms, and abstract (truncated to 500 chars).

**ORCID** — Searches the ORCID public API by `given-names:{first} AND family-name:{last} AND affiliation-org-name:"University of California San Diego"`. Falls back to a name-only search if no affiliation match is found. Fetches the full record and extracts: ORCID ID, up to 20 publications (title, year, journal), up to 15 funding records (title, agency, dates), and total works count.

**Semantic Scholar** — Searches the Academic Graph API for authors matching the faculty name, filtering by UCSD/Scripps/SIO affiliation keywords. Falls back to the top result by name similarity if no affiliation match, provided they have >5 papers. Fetches author metrics (h-index, paper count, citation count) and the 20 most recent papers (title, year, journal).

### Enrichment Strategy

The pipeline runs in three phases per faculty member:

**Phase 1 — Fetch:** Queries the division's routed sources concurrently, with
per-source rate limiting (source instances are shared across the whole run so
limits actually hold).

**Phase 2 — Field writes:** Sources are applied in descending confidence order:
- **One-time writes** (`profile_url`, `orcid`, `openalex_id`, `google_scholar_id`): Written only if the field is currently empty. Never overwritten once set.
- **Refreshed metrics** (`h_index`, `citation_count`, `works_count`): Updated every run.
- **Merged lists** (`funded_grants`, `recent_publications`, `expertise_keywords`, `awards`, `patents`): Merged across sources with per-field dedupe (DOI/normalized title/patent number); higher-confidence entries win on collisions; capped per field.

**Phase 3 — LLM normalization:** Skipped when the raw context fingerprint
(`faculty.raw_hash`) is unchanged from the previous run — refreshes are nearly
LLM-free when nothing changed. Otherwise the normalizer synthesizes five
structured fields:
- `research_interests_enriched` — 2–4 sentence narrative research summary
- `expertise_keywords` — Domain-specific keyword list
- `methodologies` — Research methods (e.g., RCT, cohort study, remote sensing, numerical modeling)
- `disease_areas` — Health conditions or research domains studied
- `populations` — Target populations or study systems/regions

The normalizer prompt includes the faculty member's original directory description (always preserved, never overwritten), UCSD profile text, NIH/NSF grant titles and abstract excerpts, PubMed publication titles with MeSH terms, Semantic Scholar metrics and publication titles, and ORCID work titles. It is instructed to merge and deduplicate, prefer institutional sources when data conflicts, and never invent expertise unsupported by the data.

### Confidence Levels

| Source | Confidence | Rationale |
|--------|-----------|-----------|
| UCSD / Scripps Profiles | 1.0 | Institutional source of record |
| ORCID | 0.9 | Self-reported by the researcher |
| LLM Normalizer | 0.85 | Synthesized from multiple verified sources |
| NIH RePORTER | 0.8 | Verified federal grant records |
| NSF Awards | 0.8 | Verified federal grant records |
| Semantic Scholar | 0.75 | Good coverage but author name disambiguation can be imperfect |
| PubMed | 0.7 | Comprehensive biomedical literature, but name+affiliation disambiguation can be imperfect |

### Audit Log

Every field change from enrichment and every auto-accepted identity is recorded in the `enrichment_log` table: faculty id, stable key, source name, source URL, field updated, old value, new value, confidence score, method (`api`, `scrape`, `llm_extraction`, or `identity_auto`), raw response excerpt (up to 5000 chars), and ISO timestamp. This provides full provenance for every data point in every faculty profile. Entries older than 30 days are pruned at the start of each run.

### Schedule (in-process, UTC)

| Job | When |
|-----|------|
| Enrich hwsph / sio / jacobs | Sun 00:00 / 02:00 / 04:00 |
| EAH reconcile (no-op without an uploaded extract) | Sun 06:00 (`EAH_RECONCILE_HOUR`) |
| Identity sweep (new + `not_found` retries) | Sun 08:00 |
| JSON provenance snapshot to `/data/backups` | Sun 10:00 |
| Backfill (never-enriched, identity-resolved; PI-eligible first) | Mon–Sat 02:00 (`BACKFILL_HOUR`), budget `BACKFILL_TIME_BUDGET` (default 4h) |

All of these can also be triggered from the admin **Enrichment** page; eScholarship harvests (`escholarship_harvest`) are manual-trigger only.

### New-division rollout runbook

1. Deploy (startup applies the schema migrations automatically).
2. Upload the latest EAH extract via **Admin → EAH Sync** — the reconcile now
   seeds/refreshes every division directly in SQLite and soft-flags departed
   faculty `Inactive` (purge later with `data.db.purge_flagged_faculty`).
3. If divisions were seeded before this release, run
   `python scripts/normalize_divisions.py` once to slug them.
4. **Admin → Enrichment → Resolve identities (PI-eligible)**, then work the
   **Identity review** queue.
5. **Backfill enrichment (PI-eligible)** — or wait for the nightly job.
6. Repeat 4–5 without the PI filter for the remaining academics.

## Data Backend

**SQLite is the source of truth** (`/data/app.db` on the persistent volume;
FTS5 full-text indexed). Seeding (EAH reconcile), enrichment, identity
resolution, and admin edits all write straight to the DB through the single
data-access module `data/db.py`; the web app reads it through read-only
connections and never writes at request time.

The git-tracked JSON files (`data/*.json`) are the historical seeds for the
original three schools: they bootstrap a brand-new volume once
(`scripts/migrate_json_to_sqlite.py`, guarded by the entrypoint) and are no
longer written by the pipeline. Weekly per-division snapshots land in
`/data/backups` for provenance.

```bash
python scripts/migrate_json_to_sqlite.py        # bootstrap only: JSON -> app.db
python scripts/export_db_to_json.py             # app.db -> legacy JSON files
python scripts/export_db_to_json.py --snapshots # app.db -> per-division dated snapshots
python scripts/normalize_divisions.py --dry-run # preview division slug normalization
```

See [`DEPLOY.md`](DEPLOY.md) for deployment (Railway volume + cron, or the
GitHub Actions build-time path).

## Architecture

| Component | Platform | What it does |
|-----------|----------|--------------|
| **Frontend** | Railway / Vercel | Serves `index.html`, CSS, and JS as static files |
| **API** | Railway (gunicorn) | Runs the Flask app — `/api/match`, `/api/match-text`, `/api/faculty` |
| **Data** | SQLite (`data/app.db`) | FTS5-indexed faculty store, read-only at request time |
| **Enrichment** | Railway Cron / GitHub Actions | Weekly data enrichment from NIH, NSF, PubMed, ORCID, Semantic Scholar, UCSD/Scripps Profiles |

## Project Structure

```
research-alignment/
├── app.py                    # Flask API
├── requirements.txt          # Python dependencies
├── Procfile                  # gunicorn web process (Railway/Render)
├── DEPLOY.md                 # Deployment guide
├── vercel.json               # Legacy Vercel deployment config
├── index.html                # Single-page frontend (three-tab interface)
├── .env.example              # Environment variable template
├── data/
│   ├── db.py                 # SQLite data-access layer (all SQL)
│   ├── schema.sql            # SQLite schema (tables + FTS5)
│   ├── app.db                # SQLite database (generated; git-ignored)
│   ├── faculty.json          # HWSPH faculty directory (seed/snapshot)
│   ├── sio_faculty.json      # SIO faculty directory (seed/snapshot)
│   └── jacobs_faculty.json   # Jacobs faculty directory (seed/snapshot)
├── scripts/
│   ├── migrate_json_to_sqlite.py  # JSON -> SQLite (one-off / CI bootstrap)
│   └── export_db_to_json.py       # SQLite -> JSON (diffable provenance)
├── static/
│   ├── css/style.css         # UCSD-branded styles (Seed Style Guide)
│   └── js/app.js             # Frontend logic
├── utils/
│   ├── document_parser.py    # PDF/TXT text extraction
│   └── grant_matcher.py      # LLM matching engine + keyword pre-filter
├── enrichment/
│   ├── pipeline.py           # Enrichment orchestrator (HWSPH, SIO, Jacobs)
│   ├── normalizer.py         # LLM-based data normalization
│   ├── run.py                # GitHub Actions runner
│   ├── seed_sio.py           # SIO faculty seeding script
│   ├── seed_jacobs.py        # Jacobs faculty seeding script
│   └── sources/              # Data source adapters (NIH, NSF, PubMed, ORCID, UCSD, Scripps)
└── docs/
    ├── responsible-ai-seed-principles.md
    └── seed-style-guide.md
```

## Faculty Schema

Each faculty record includes:

| Field | Type | Description |
|-------|------|-------------|
| `first_name`, `last_name` | string | Name |
| `degrees` | string[] | Academic degrees |
| `title` | string | Position title |
| `email` | string | Contact email |
| `research_interests` | string | Original directory text (never overwritten) |
| `research_interests_enriched` | string | LLM-synthesized summary from all sources |
| `expertise_keywords` | string[] | Extracted domain keywords |
| `methodologies` | string[] | Research methods used |
| `disease_areas` | string[] | Health conditions studied |
| `populations` | string[] | Target populations |
| `committee_service` | string[] | Academic Senate committee participation |
| `integrity_flags` | string[] | Research integrity flags (future feature) |
| `h_index` | int | Hirsch index |
| `funded_grants` | object[] | Funded project history |
| `recent_publications` | object[] | Recent publication history |

## API

### `POST /api/match`

Upload a funding opportunity document for faculty matching.

**Request:** `multipart/form-data` with a `file` field (PDF or TXT, max 10 MB)

### `POST /api/match-text`

Match manually entered expertise text against faculty.

**Request:** `application/json` with `{"text": "expertise requirements..."}`

### `GET /api/faculty`

Return the faculty directory for browsing and filtering.

**Response (200):** Array of faculty objects with profile fields.

### Response Format (match endpoints)

```json
{
  "grant_summary": {
    "grant_title": "...",
    "funding_agency": "...",
    "grant_summary": "...",
    "investigator_requirements": [...],
    "overall_research_themes": [...]
  },
  "matches": [
    {
      "rank": 1,
      "first_name": "...",
      "last_name": "...",
      "match_score": 85,
      "expertise_alignment": 90,
      "methodological_fit": 80,
      "track_record": 75,
      "match_reasoning": "..."
    }
  ],
  "total_faculty_considered": 109,
  "faculty_without_interests_count": 21
}
```

## Deployment

**Recommended: deploy on Railway as a single container** — see the step-by-step
guide in **[RAILWAY.md](RAILWAY.md)**. Enrichment, scheduling, and the sensitive
EAH employment sync all run inside the service (no GitHub Actions, no CLI), with
a persistent `/data` volume. For the data-layer / alternative-host details, see
[DEPLOY.md](DEPLOY.md).

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LITELLM_API_KEY` | Yes | LLM API key |
| `LITELLM_API_BASE` | Yes | LLM API endpoint URL |
| `LITELLM_MODEL` | No | Model identifier (default: `openai/api-gpt-oss-120b`) |
| `NCBI_API_KEY` | No | PubMed API key (increases rate limit from 3 to 10 req/s) |
| `S2_API_KEY` | No | Semantic Scholar API key (increases quota) |
