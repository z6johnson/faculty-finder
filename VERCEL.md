# Vercel deployment branch (frozen JSON backend)

This branch — **`json-stable`** — is the last snapshot of the app *before* the
SQLite migration. The app reads `data/*.json` directly (no database, no build
step), so it runs on Vercel's serverless, read-only filesystem.

## Purpose
A working Vercel fallback while the SQLite version is finished on Render. Point
Vercel's **Production Branch** here (Project → Settings → Git → Production Branch)
and redeploy.

## Important
- **Frozen.** This branch does not receive new enrichment. Active development and
  all data updates happen on `render-sqlite` (deployed to Render).
- **Do not** point Vercel at `main` or `render-sqlite` — those run the SQLite read
  layer, which needs a deploy-time database build that Vercel doesn't perform
  (you'd get "Faculty DB not found" 500s).
- Decommission this branch once Render is the sole production deployment.
