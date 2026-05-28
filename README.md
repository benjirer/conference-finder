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
┌──────── refresh pipeline (17 steps, idempotent, no API calls) ────────┐
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
caches so Render never has to make API calls):

- `python -m app.enrich_extras` — fills missing date fields, page limits,
  multi-round info from each venue's CFP page (~$2 for a full pass).
- `python -m app.enrich_pc` — discovers each venue's PC page and extracts the
  member list (~$3 for a full pass).
- `python -m app.enrich` — runs both, then refreshes the DB in one command.

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
Re-run monthly (or with `--force --acronym X` for a single venue). The
local-only design keeps Render's Anthropic spend at $0.

### Tests

```bash
python -m pytest tests/ -q
```

35 smoke tests covering helpers, API endpoints, refresh pipeline, rate-limit
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
(~60 s cold-start to rebuild the DB from sources + committed caches).

### What does and doesn't cost tokens

| Action | Cost | When |
|---|---|---|
| Render cold start | $0 | automatic |
| Daily refreshes / users browsing | $0 | automatic |
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
    user_added.yaml        ← venues from + Add (commit if you want them on Render)
    conferences.db         ← SQLite, regenerated on every refresh (gitignored)
  tests/                   ← pytest smoke tests
  requirements.txt
  pytest.ini
render.yaml                ← Render Blueprint
```
