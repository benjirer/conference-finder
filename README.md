# Conference Finder

A locally-hostable tracker for conferences and workshops at the intersection of
**control**, **networked systems**, and **machine learning**. Provides a web
dashboard with filterable deadlines, a subscribable iCalendar feed, an
interactive world map for "what's near here?", and PC-membership comparison
across venues.

Single-binary backend (FastAPI + SQLite), no Node/npm. Designed to run free on
[Render](https://render.com) — see [Deploying](#deploying-for-free-on-render).

## What's in the dashboard

- **~480 venues across 6 aggregators** automatically pulled (no manual upkeep):
  ccfddl, aideadlines, ds-deadlines, klb2/conference-calendar, noise-lab,
  confsearch.ethz.ch. ~200 LLM-enriched with full date fields, page limits,
  acceptance rates, multi-round info. ~145 with extracted PC member lists.
- **Light & dark theme** with theme-following CartoDB tiles for the map.
- **Filters**: areas (control / networking / ml / systems / multimedia / robotics),
  workshops, deadline state (upcoming / passed / all), predicted, diverged
  (sources disagree), year, free-text search.
- **Sortable columns**: every column. Date columns sort chronologically, tier
  by rank order, acceptance rate numerically.
- **World map** (🌍 button): click anywhere to set an anchor; venues re-sort
  by haversine distance.
- **PC compare**: enter compare mode → tick venues that have PC data → see
  intersection + per-venue affiliations + pairwise overlap counts.
- **Calendar subscription** with per-deadline all-day **and** 1-hour timed
  events, both UTC-anchored. Predicted dates marked `[Predicted]`.
- **`+ Add venue`** button: paste a CFP URL → two-pass LLM extraction → result
  persisted (rate-limited per IP).

## Architecture

```
┌──────── refresh pipeline (aggregators + official CFP checks) ────────┐
│                                                                       │
│  1. confsearch                                                        │
│  2. noise-lab                                                         │
│  3. klb2                       ← aggregator ingest (lowest priority)  │
│  4. ds-deadlines                                                      │
│  5. aideadlines                                                       │
│  6. ccfddl                     ← aggregator ingest (highest priority) │
│  7. seed_venues.yaml           ← curated control venues + workshops   │
│  8. user_added.yaml            ← venues added via /api/venues         │
│  9. cleanup old years                                                 │
│ 10. venue_stats.yaml overlay   ← h5_index / accept rate / page limit  │
│ 11. cached_extras.yaml         ← LLM-extracted dates etc (local cache)│
│ 12. cached_pc.yaml             ← LLM-extracted PC members (local)     │
│ 13. classify missing areas     ← name-keyword classifier              │
│ 14. geocode locations          ← static city dict                     │
│ 15. reconcile                  ← cross-source date verification       │
│ 16. predict next-year          ← extrapolate one year forward         │
│ 17. predict missing tiers      ← h5_index heuristic                   │
│ 18. official CFP checks       ← changed pages only; API key required  │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
                              ▼
                         SQLite DB
                              ▼
                ┌──────────────────────────┐
                │  FastAPI + static HTML   │  ←  Leaflet world map,
                │  dashboard + calendar    │     theme toggle, PC compare,
                │  + ICS feed              │     filters, sort, /api/*
                └──────────────────────────┘
```

**Two side-cars for one-shot LLM enrichment** (run *locally*, commit their YAML
caches for bulk enrichment):

- `python -m app.enrich_extras` — fills missing date fields, page limits,
  multi-round info from each venue's CFP page (~$2 for a full pass).
- `python -m app.enrich_pc` — discovers each venue's PC page and extracts the
  member list (~$3 for a full pass).
- `python -m app.enrich` — runs both, then refreshes the DB in one command.

## Reliability and deployment configuration

- Public aggregators refresh in the background every six hours, starting one
  shortly after the web server starts. Set `CONFERENCE_FINDER_REFRESH_HOURS=0`
  to disable this (for an external scheduler); otherwise use a positive number.
  Run one Uvicorn worker to avoid duplicate refresh jobs. Sleeping/stopped hosts
  cannot run periodic checks. `/api/refresh-status` reports the latest run and
  source failures. `/api/health` remains the lightweight health check.
- ccfddl coverage is discovered from its repository in the supported categories,
  with the curated venue list as a fallback if discovery fails. This broadens
  coverage; it does not guarantee every conference is represented.
- Published editions replace predicted dates; later aggregator updates can
  change existing deadlines. Static seed dates do not overwrite live dates.
- Deadline offsets and IANA timezones are normalized to UTC, and the API marks
  timestamps with `Z`. Incomplete dates are left unknown. Extraction follows up
  to three same-site CFP/dates links and removes struck-out text.
- User additions use locked, atomic YAML writes. Set
  `CONFERENCE_FINDER_DATA_DIR` to a **persistent mounted directory** to keep
  `conferences.db` and `user_added.yaml` across deploys. Bundled seeds and
  enrichment caches still load from the repository. Copy any existing
  `backend/data/user_added.yaml` into the mounted directory when migrating.
  Back up this directory; user additions are not disposable cache data.
- The included free/ephemeral Render configuration does **not** provide durable
  storage. Setting a directory variable without mounting durable storage is
  insufficient. Provision a persistent volume or deploy on a host with durable
  local storage before relying on public additions surviving redeploys.
- Every refresh also checks official CFP/homepage URLs for current and future
  editions, including user additions and predictions. Checks run after source
  ingestion and replay accepted official dates, so stale aggregator data cannot
  undo an official correction. Newly predicted editions are included immediately.
- All due official pages are fetched each sweep using six concurrent workers
  (`CONFERENCE_FINDER_FETCH_WORKERS`). `CONFERENCE_FINDER_OFFICIAL_LIMIT=0`
  means no page-count cap; a positive value is an optional cap. The minimum
  interval per edition remains 24 hours (`CONFERENCE_FINDER_OFFICIAL_HOURS`).
  `CONFERENCE_FINDER_EXTRACTION_LIMIT=100` separately bounds changed-page
  extraction attempts per sweep; queued pages retry next time. Scanned-PDF
  transcription also uses the API, separately from this extraction limit.
  Set `CONFERENCE_FINDER_OFFICIAL_ENABLED=0` to disable live checks.
- Official-page text is hashed before extraction. Unchanged successful pages
  need no model calls. Changed/new pages use the existing two-pass extractor
  and require `ANTHROPIC_API_KEY`; this incurs API usage. Venue identity, edition,
  date ordering, and submission-round agreement are checked before accepting
  dates. Failures retain the last accepted dates and retry on a later run.
- `/api/official-checks` exposes each URL, check time, verification time, and
  error. The `official_checks` SQLite table saves hashes and accepted dates on
  the configured runtime volume. Back up the database along with user YAML.
- PDFs use text extraction, with a cached document-model transcription fallback
  for scans. JavaScript app shells use Playwright Chromium; install it with
  `python -m playwright install chromium` (Linux hosts may also need
  `python -m playwright install --with-deps chromium`). Failed linked pages
  no longer discard readable content from the main CFP.
- When the known page identifies an old edition, discovery follows next-edition
  links and probes year URL variants. Candidate pages must still pass identity
  and date validation. This cannot discover every site's naming convention.
- Official snapshots stop overriding source data after a failed check or 14 days
  without verification (`CONFERENCE_FINDER_OFFICIAL_MAX_AGE_DAYS`). Explicitly
  retracted dates are cleared when extraction agrees on withdrawal. Merely
  missing dates are not treated as retracted.
- Per-field `date_metadata` records official confirmation, source, verification
  time and date/date-time precision. The dashboard shows stale/confirmed state.
  Confirmed date-only values remain dates in the API and generate no invented
  final-hour calendar event. Older source data without this metadata is shown
  as unverified.

### Review updates through GitHub

Set `CONFERENCE_FINDER_GITHUB_TOKEN` (or `GITHUB_TOKEN`) and
`CONFERENCE_FINDER_GITHUB_REPO=benjirer/conference-finder` to enable PR review.
The token needs repository Contents and Pull requests read/write permissions.
Local credentials can be stored in `backend/.env`, which is ignored by Git.

Scheduled official checks collect changes in **one open PR**. Later checks update
that PR and replace the pending snapshot for an edition when its dates change
again. The PR includes a table of previous/proposed dates and source links;
`backend/data/scheduled_review.md` holds the full table for large batches.
After the PR is merged or closed, the next new proposal starts a fresh PR from
the default branch. The workflow concurrency group serializes scheduled writers.

User additions still get individual PRs with JSON snapshots under
`backend/data/reviewed_updates/`; identical submissions reuse their PR.
Both user additions and official date changes remain pending until merged.
Merged snapshots are imported on refresh and survive redeployment because they
are in Git. A rejected/closed PR is not automatically reopened.

The scheduled `.github/workflows/venue-updates.yml` runs independently of the web
server. Add `ANTHROPIC_API_KEY` to repository Actions secrets and enable
**Allow GitHub Actions to create and approve pull requests** in repository
Actions settings; the workflow only creates PRs and never approves or merges.
Its `GITHUB_TOKEN` is supplied by Actions automatically. Runtime state uses an
Actions cache as an optimization; merged snapshots remain the durable record.
The web process can still run its own checks; set
`CONFERENCE_FINDER_OFFICIAL_ENABLED=0` there to avoid duplicate paid work when
using Actions. GitHub scheduling is not a precise timer.

### Live verification

`python backend/scripts/check_live_pages.py` checks public HTML/PDF retrieval.
`python backend/scripts/check_live_extraction.py` uses the API key to compare
extracted dates from three real CFPs (including CGO submission rounds) against manually checked values. Neither
command modifies venue data or creates PRs. Unit tests use mocked API responses;
live scripts must be run separately. `check_browser_and_scanned_pdf.py` exercises a real isolated browser and a scanned-PDF fixture; `--app` verifies the Data status dialog and recheck form. Set `CONFERENCE_FINDER_BROWSER_EXECUTABLE` to use an existing compatible Chrome executable instead of Playwright’s downloaded browser.

## Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.refresh                # initial data load (~60 s)
uvicorn app.main:app --port 8000     # serve at http://localhost:8000
```

That's the full daily workflow. No API key required for any of the public-data
features.

### Optional: enrich with LLM data

For richer PC compare / notification / camera-ready / page-limit fields:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m app.enrich                 # one command: extras + pc + refresh
```

Run once, then commit `backend/data/cached_extras.yaml` and `cached_pc.yaml`.
Re-run monthly (or with `--force --acronym X` for a single venue). Scheduled official-page checks also use the key when page content changes.

### Tests

```bash
python -m pytest tests/ -q
```

Regression and smoke tests covering helpers, API endpoints, refresh pipeline, rate-limit
guard, enrichment cache.

## Deploying for free on Render

`render.yaml` at the repo root is a Render Blueprint. Free tier, no credit card.

1. Push to GitHub.
2. <https://dashboard.render.com> → **New → Blueprint** → pick the repo.
3. (Optional) in the service's **Environment** tab, set
   `ANTHROPIC_API_KEY` if you want the **+ Add venue** button to work in
   production. Without it, that button degrades gracefully; everything else
   keeps working.

Render auto-deploys on every push. The free tier sleeps after 15 min idle
(the server starts before the background data refresh finishes).

### What does and doesn't cost tokens

| Action | Cost | When |
|---|---|---|
| Render cold start | API usage for new/changed official pages | automatic |
| Scheduled refreshes | API usage for changed official pages; unchanged pages skip extraction | automatic |
| Users browsing | $0 in LLM usage | on request |
| `+ Add venue` button | ~$0.01 | only when someone clicks |
| `python -m app.enrich` (local) | ~$3–5 full pass | only when you run it |

## Hardening notes (deployed-ready)

- **YAML loaders** never raise — corrupt cache files log a warning and the
  pipeline continues with defaults.
- **HTTP fetches** use a shared retry helper (`_common.http_get`) that retries
  5xx and connection failures, gives up immediately on 4xx.
- **`POST /api/venues`** is rate-limited at 5/hour per client IP, validates
  URL scheme (http/https only), max length 2048, and blocks SSRF targets
  (localhost / private IPs / `.local`).
- **`POST /api/pc/compare`** caps `conference_ids` at 20.
- **Schema migration** in `db.py` only drops tables when
  `CONFERENCE_FINDER_NO_DESTRUCTIVE=1` is *not* set (default: allowed,
  since the DB is reproducible).

## Adding venues

1. **Already in ccfddl?** Add a one-line entry to
   `app/sources/ccfddl.py::VENUE_MAP` with the right `areas` and `tier`.
2. **Not in any aggregator?** Add a block to `backend/data/seed_venues.yaml`
   (or click **+ Add venue** in the dashboard — both write to the same DB).
3. Re-run `python -m app.refresh`.

## File layout

```
backend/
  app/
    main.py                ← FastAPI app + endpoints
    refresh.py             ← the 17-step pipeline
    enrich.py              ← single-command LLM enrichment
    enrich_extras.py       ← LLM-extract dates etc
    enrich_pc.py           ← LLM-extract PC members
    _enrich_common.py      ← shared cache class
    models.py              ← SQLAlchemy models
    db.py                  ← engine + migrations
    ical.py                ← calendar feed generator
    sources/               ← 18 ingest/processing modules (one per aggregator)
    static/                ← dashboard HTML / CSS / JS (vanilla, no build step)
  data/
    seed_venues.yaml       ← curated venues (commit changes)
    venue_stats.yaml       ← per-acronym h5/accept/page (commit changes)
    cached_extras.yaml     ← LLM cache for dates (commit, auto-generated)
    cached_pc.yaml         ← LLM cache for PCs   (commit, auto-generated)
    user_added.yaml        ← local default; use durable DATA_DIR in production
    conferences.db         ← SQLite, regenerated on every refresh (gitignored)
  tests/                   ← pytest smoke tests
  requirements.txt
  pytest.ini
render.yaml                ← Render Blueprint
```
