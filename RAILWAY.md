# Deploying Research Alignment on Railway

A step-by-step guide to running Research Alignment as a single container on
[Railway](https://railway.app), with a persistent volume for the database and
the sensitive EAH extract. Everything after the initial setup is operated from
the browser — no CLI, no GitHub Actions.

The same container image and `/data` volume lift directly onto a UCSD-managed
on-prem VM later (see [Migrating off Railway](#migrating-off-railway-later)).

---

## What you'll end up with

```
Railway service (built from the Dockerfile in this repo)
├── Public site:  https://<your-app>.up.railway.app
│     /                      faculty discovery UI
│     /api/faculty, /api/match…
│     /admin                 password-gated admin (you operate everything here)
├── In-process scheduler     weekly enrichment + EAH reconcile (no GitHub Actions)
└── Volume mounted at /data  (private; survives redeploys)
      app.db                 live SQLite database
      state/*.json           writable faculty snapshots
      private/EAH*.csv        the sensitive HR extract — never in git
      backups/
```

---

## Before you start

You need:

1. **This repo on GitHub** (Railway deploys from it). Use the branch you want to
   deploy — e.g. `main` after this work merges.
2. **A Railway account** — sign up at [railway.app](https://railway.app) (GitHub
   login is easiest).
3. **LLM API credentials** for enrichment + matching (LiteLLM-compatible): an API
   key, a base URL, and a model id.
4. **A strong admin password** you choose now (you'll paste it as `ADMIN_PASSWORD`).
5. *(Optional)* NCBI and Semantic Scholar API keys for higher enrichment rate limits.

---

## Step 1 — Create the project from this repo

1. In the Railway dashboard, click **New Project → Deploy from GitHub repo**.
2. Authorize Railway for your GitHub account/org if prompted, then pick
   **`research-alignment`**.
3. Railway detects the repo's **`Dockerfile`** and creates a service that builds
   from it. Let the first build start — it will not be healthy until you add the
   volume and env vars below, which is expected.

> If Railway picked a different branch than you intend, open the service →
> **Settings → Source** and set the correct branch.

---

## Step 2 — Add the persistent volume

This is the most important step: without a volume, the database and the uploaded
EAH extract are wiped on every redeploy.

1. Open the service → **Variables/Settings → Volumes** (or right-click the service
   canvas → **Add Volume**).
2. Create a volume with **Mount path = `/data`**.
3. Attach it to this service.

The container's `docker-entrypoint.sh` writes everything under `/data`
(`app.db`, `state/`, `private/`, `backups/`).

---

## Step 3 — Set environment variables

Open the service → **Variables** and add the following.

**Required:**

| Variable | Value | Purpose |
|---|---|---|
| `SECRET_KEY` | a long random string | Signs admin session cookies. If unset, sessions reset on every restart. |
| `ADMIN_PASSWORD` | your chosen password | Unlocks `/admin`. If unset, the admin area stays locked. |
| `LITELLM_API_KEY` | your LLM key | Enrichment + matching |
| `LITELLM_API_BASE` | your LLM base URL | Enrichment + matching |
| `LITELLM_MODEL` | e.g. `openai/api-gpt-oss-120b` | Model id |
| `OPENALEX_MAILTO` | a contact email | Joins the OpenAlex polite pool — without it, enrichment and identity dossier fetches get rate-limited (429s) |

**Already defaulted by the Dockerfile** (only set these to override):

| Variable | Default | Notes |
|---|---|---|
| `FACULTY_DB_PATH` | `/data/app.db` | SQLite DB on the volume |
| `DATA_STATE_DIR` | `/data/state` | Writable faculty JSON snapshots |
| `EAH_CSV_PATH` | `/data/private/EAH Active Academics.csv` | Private EAH extract location |
| `PORT` | provided by Railway | gunicorn binds `0.0.0.0:$PORT` automatically |

**Optional:**

| Variable | Default | Purpose |
|---|---|---|
| `NCBI_API_KEY` | — | Higher PubMed rate limits |
| `S2_API_KEY` | — | Higher Semantic Scholar rate limits |
| `EAH_RECONCILE_HOUR` | `6` | UTC hour of the weekly EAH reconcile |
| `ENABLE_SCHEDULER` | `true` | Set `false` to disable the in-app weekly scheduler |

> Generate a strong `SECRET_KEY` locally with:
> `python -c "import secrets; print(secrets.token_hex(32))"`

---

## Step 4 — Generate a public domain

1. Service → **Settings → Networking → Public Networking**.
2. Click **Generate Domain** (Railway maps it to the container's `$PORT`).
3. *(Optional)* Set **Healthcheck Path** to `/` under Settings.

---

## Step 5 — Deploy and watch the first boot

Trigger a redeploy (Railway redeploys automatically after the volume/variables
change). On first boot the entrypoint:

1. Seeds `/data/state` from the committed faculty JSON.
2. Builds `/data/app.db` from that JSON (`migrate_json_to_sqlite.py`).
3. Starts gunicorn (single worker, in-process scheduler).

In **Deploy Logs** you should see lines like:

```
[entrypoint] seeding faculty.json into /data/state
[entrypoint] building SQLite database at /data/app.db
[entrypoint] starting gunicorn (single writer worker)
```

Subsequent redeploys reuse the existing volume — they do **not** re-seed or
overwrite your live data.

---

## Step 6 — Verify it's up

1. Open `https://<your-app>.up.railway.app/` — the faculty discovery UI loads.
2. `GET /api/faculty` returns faculty JSON.
3. Go to `https://<your-app>.up.railway.app/admin`, sign in with `ADMIN_PASSWORD`,
   and you should see the dashboard with four tiles.

---

## Step 7 — Upload the EAH extract (employment verification)

This is how the sensitive HR data enters the system — it is written only to the
private volume and is never committed to the repo.

1. Admin → **EAH Sync**.
2. Choose your **EAH Active Academics** CSV export and click **Upload & reconcile**.
3. The file is saved to `EAH_CSV_PATH`, reconciliation runs, and the live database
   is rebuilt. You'll see matched / removed-inactive / added counts per school.

Repeat whenever you have a fresh extract. (A weekly automatic reconcile also runs
if an extract is present.)

---

## Step 8 — Run / schedule enrichment

- Admin → **Enrichment** → **Run now** for a school to enrich on demand. Runs
  execute in the background; track them under **Recent jobs** (refresh to update).
- The **weekly schedule** (shown on the same page) runs automatically in-process —
  HWSPH Sun 00:00, SIO 02:00, Jacobs 04:00 UTC, EAH reconcile at `EAH_RECONCILE_HOUR`.
- Coverage, job history, and the audit log are on the **Status & audit** page.

> A full enrichment run hits external academic APIs and the LLM and can take many
> minutes per school — this is why it runs in the background rather than blocking
> a request.

---

## Backups

The SQLite DB lives on the volume. To snapshot it:

- Admin curation and reconciles already keep the JSON snapshots in `/data/state`
  current; copy them (or `/data/app.db`) off the volume periodically.
- From a Railway shell on the service you can also run a WAL-safe backup:
  `sqlite3 /data/app.db ".backup /data/backups/app-$(date +%F).db"`.

---

## Migrating off Railway later

Because the service is just a container plus a `/data` volume, moving to the UCSD
on-prem VM is a lift-and-shift:

1. Install Docker on the VM.
2. `git clone` the repo (code only — no PII) and build the same image, or run it
   via `docker compose`.
3. Copy the `/data` directory (`app.db` + `state/` + `private/` + `backups/`) onto
   the VM's volume.
4. Set the same environment variables and start the container.
5. Swap the password login for UCSD SSO (Shibboleth) at that point.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/admin` rejects every password | `ADMIN_PASSWORD` not set — add it in Variables and redeploy. |
| Admin logs you out after each redeploy | `SECRET_KEY` not set (ephemeral key per boot) — set a fixed `SECRET_KEY`. |
| Data resets on redeploy | No volume mounted at `/data`, or a different mount path — re-check Step 2. |
| Build fails to find the Dockerfile | Wrong branch/root in Settings → Source. |
| EAH upload "could not be read" | The file isn't the EAH Active Academics CSV export (it expects 3 header rows then columns). |
| No "Weekly schedule" shown | `ENABLE_SCHEDULER=false`, or the scheduler failed to start — check deploy logs. |
| Enrichment job ends in `failed` | Usually missing/invalid `LITELLM_*` — verify the key/base/model; details show in the job row and logs. |
