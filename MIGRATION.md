# Faculty Finder — Deployment & Operations Guide

Complete guide for deploying Faculty Finder on Vercel with GitHub Actions enrichment.

## Architecture

```
GitHub Repository
├── Vercel (Web Service — Flask serves frontend + API)
├── GitHub Actions (Scheduled enrichment — weekly cron)
└── data/faculty.json (Version-controlled data store)
```

- **Vercel** serves the frontend and matching API as a single serverless function
- **GitHub Actions** runs enrichment offline (no timeout constraints) and commits updated data back to the repo
- **No database** — `data/faculty.json` is the single source of truth, version-controlled with full git history

---

## 1. Vercel Setup

### Connect the repository

1. Sign up at [vercel.com](https://vercel.com) (GitHub login recommended)
2. Click **Add New Project** → import the repository
3. Vercel auto-detects the Python app via `vercel.json`
4. Deploy — the first build installs dependencies and starts serving

### Set environment variables

In the Vercel dashboard, go to your project → **Settings** → **Environment Variables**:

| Variable | Required | Value |
|----------|----------|-------|
| `LITELLM_API_KEY` | Yes | Your LLM provider API key |
| `LITELLM_API_BASE` | Yes | Your LLM API base URL |
| `LITELLM_MODEL` | No | Default: `openai/api-gpt-oss-120b` |

### Verify

Visit your Vercel URL — you should see the Faculty Finder UI. Upload a funding opportunity PDF to test matching.

---

## 2. Local Development

### Quick start

```bash
git clone https://github.com/your-org/grant-matcher.git
cd grant-matcher
pip install -r requirements.txt

# Create .env with your LLM credentials
cp .env.example .env  # or create manually

# Run locally
python -m flask run
```

### Local `.env` file

```env
LITELLM_API_KEY=your-llm-api-key
LITELLM_API_BASE=https://your-llm-endpoint.com/v1
```

---

## 3. Faculty Enrichment

Enrichment populates faculty records with data from public academic sources. It runs via **GitHub Actions** — either on a weekly schedule or triggered manually.

### How it works

1. GitHub Actions checks out the repo
2. Runs `python enrichment/run.py` which queries public APIs (UCSD Profiles, NIH RePORTER, PubMed, ORCID)
3. An LLM normalizes the raw data into structured fields
4. Updated `data/faculty.json` is committed and pushed back to the repo
5. Vercel auto-deploys with the updated data

### Configure GitHub Secrets

In your repo → **Settings** → **Secrets and variables** → **Actions**, add:

| Secret | Required | Purpose |
|--------|----------|---------|
| `LITELLM_API_KEY` | Yes | LLM API key for normalization |
| `LITELLM_API_BASE` | Yes | LLM API base URL |
| `LITELLM_MODEL` | No | LLM model override |
| `OPENALEX_MAILTO` | Strongly recommended | Contact email for the OpenAlex polite pool (avoids 429 rate limits) |
| `NCBI_API_KEY` | No | PubMed higher rate limits (3 → 10 req/s). Free at [ncbi.nlm.nih.gov/account](https://www.ncbi.nlm.nih.gov/account/) |
| `S2_API_KEY` | No | Semantic Scholar higher rate limits (3 → 1 req/s with key, but higher quota). See [semanticscholar.org/product/api](https://www.semanticscholar.org/product/api) |

### Run enrichment manually

1. Go to **Actions** tab in your GitHub repo
2. Select **Faculty Enrichment** workflow
3. Click **Run workflow**
4. Optionally specify sources or faculty indices
5. Watch the logs for progress

### Schedule

By default, enrichment runs **every Sunday at midnight UTC**. Edit `.github/workflows/enrich.yml` to change the cron schedule.

### Enrichment sources

#### HWSPH (Public Health)

| Source | Confidence | What it provides | Rate limit | Auth |
|--------|-----------|-----------------|------------|------|
| UCSD Profiles | 1.0 | Research description, profile URL | 1 req/2s | None |
| ORCID | 0.9 | ORCID ID, publications, fundings | 1 req/s | None |
| NIH RePORTER | 0.8 | Funded projects, abstracts, co-PIs | 1 req/s | None |
| Semantic Scholar | 0.75 | h-index, recent publications, citation/paper counts | 3 req/s (1 with key) | Optional (`S2_API_KEY`) |
| PubMed | 0.7 | Recent publications, MeSH terms, abstracts | 3 req/s (10 with key) | Optional (`NCBI_API_KEY`) |

#### SIO (Scripps Oceanography)

| Source | Confidence | What it provides | Rate limit | Auth |
|--------|-----------|-----------------|------------|------|
| Scripps Profiles | 1.0 | Research description, profile URL | 1 req/2s | None |
| ORCID | 0.9 | ORCID ID, publications, fundings | 1 req/s | None |
| NIH RePORTER | 0.8 | Funded projects, abstracts, co-PIs | 1 req/s | None |
| NSF Awards | 0.8 | Funded grants, program names, abstracts | 1 req/s | None |
| Semantic Scholar | 0.75 | h-index, recent publications, citation/paper counts | 3 req/s (1 with key) | Optional (`S2_API_KEY`) |
| PubMed | 0.7 | Recent publications, MeSH terms, abstracts | 3 req/s (10 with key) | Optional (`NCBI_API_KEY`) |

### Expected timings

| Source | Per faculty | All 130 faculty |
|--------|-----------|-----------------|
| UCSD / Scripps Profiles | ~4s | ~9 min |
| NIH RePORTER | ~1s | ~2.5 min |
| NSF Awards (SIO only) | ~1s | ~2.5 min |
| PubMed | ~1s | ~1.5 min |
| ORCID | ~2s | ~4.5 min |
| Semantic Scholar | ~6s | ~13 min |
| **All sources (HWSPH)** | ~14s | **~30 min** |
| **All sources (SIO)** | ~15s | **~33 min** |

---

## 4. Data Model

### `data/faculty.json` structure

```json
{
  "school": "Herbert Wertheim School of Public Health and Human Longevity Science",
  "university": "UC San Diego",
  "source_url": "https://hwsph.ucsd.edu/people/faculty/faculty-directory.html",
  "date_retrieved": "2026-03-11",
  "faculty": [
    {
      "first_name": "Wael",
      "last_name": "Al-Delaimy",
      "degrees": ["MD", "PhD"],
      "title": "Professor",
      "email": "waldelaimy@health.ucsd.edu",
      "research_interests": "Original from directory — never overwritten",

      "research_interests_enriched": "LLM-normalized summary from enrichment",
      "expertise_keywords": ["epidemiology", "HIV", "..."],
      "methodologies": ["cohort study", "RCT", "..."],
      "disease_areas": ["cardiovascular disease", "..."],
      "populations": ["adolescents", "refugees", "..."],
      "committee_service": ["Committee on Research Policy"],
      "integrity_flags": [],
      "profile_url": "https://profiles.ucsd.edu/...",
      "orcid": "0000-0001-2345-6789",
      "h_index": 42,
      "recent_publications": [{"title": "...", "year": 2025, "journal": "..."}],
      "funded_grants": [{"title": "...", "agency": "NIH", "start_date": "2023"}],
      "last_enriched": "2026-03-16T00:00:00+00:00"
    }
  ]
}
```

### `data/enrichment_log.json`

Append-only log tracking every field change made by enrichment. Each entry records the source, field, old/new values, confidence score, and timestamp. This provides full provenance — and since the file is in git, you get diffs over time.

### Source confidence levels

| Source | Confidence | Rationale |
|--------|-----------|-----------|
| UCSD / Scripps Profiles | 1.0 | Institutional source of record |
| ORCID | 0.9 | Self-reported by researcher |
| LLM Normalizer | 0.85 | Synthesized from multiple verified sources |
| NIH RePORTER | 0.8 | Verified federal grant records |
| NSF Awards | 0.8 | Verified federal grant records |
| Semantic Scholar | 0.75 | Good coverage; name disambiguation can be imperfect |
| PubMed | 0.7 | Comprehensive biomedical literature; name disambiguation can be imperfect |

---

## 5. Adding New Enrichment Sources

### Step 1: Create the source class

```python
# enrichment/sources/my_source.py
from .base import BaseSource

class MySource(BaseSource):
    source_name = "my_source"
    min_request_interval = 1.0
    confidence = 0.8

    def fields_provided(self):
        return ["field_name_1", "field_name_2"]

    def fetch(self, faculty_dict):
        # Your API/scraping logic here
        return {
            "field_name_1": "value",
            "_source_url": "https://api.example.com/...",
        }
```

### Step 2: Register in `enrichment/pipeline.py`

```python
from .sources.my_source import MySource

SOURCE_CLASSES = {
    ...,
    "my_source": MySource,
}
```

### Step 3: Handle in `enrichment/normalizer.py` (if applicable)

Add a section in `normalize_faculty_data()` to include the source's data in the LLM prompt.

---

## 6. API Reference

### Frontend
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Faculty Finder web application |

### Matching
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/match` | Upload funding opportunity PDF/TXT, get faculty matches |
| POST | `/api/match-text` | Submit expertise text, get faculty matches |
| GET | `/api/faculty` | Browse faculty directory |

---

## 7. Roadmap

| Phase | Status | What |
|-------|--------|------|
| 1 | Done | Faculty directory scraper, JSON data model, matching engine |
| 2 | Done | Enrichment pipeline (UCSD, NIH, PubMed, ORCID), LLM normalizer |
| 3 | Done | Vercel deployment, GitHub Actions enrichment |
| 4 | Done | Three-mode UI (upload, manual entry, expert directory), keyword pre-filter |
| Future | Planned | Committee service data, research integrity checks, multi-school support |
