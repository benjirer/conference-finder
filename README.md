# Conference Finder

A locally-hosted tracker for conferences and workshops at the intersection of
**control**, **networked systems**, and **machine learning**. Provides a web
dashboard with filterable deadlines and a subscribable iCalendar feed.

## What's in it now

After a fresh refresh: ~480 distinct conference/workshop venues across 6
aggregators + curated seed + user-added, plus ~230 next-year predictions.

Aggregators (in priority order — later ones win when sources disagree):

| Source | Coverage | Format |
|---|---|---|
| [ccfddl](https://github.com/ccfddl/ccf-deadlines) | Broad CS (SIGCOMM, NSDI, NeurIPS, ICML, ICRA, …) | YAML in GitHub repo |
| [aideadlines](https://github.com/abhshkdz/ai-deadlines) | AI / CV / NLP (CVPR, ECCV, ACL, EMNLP, …) | YAML in GitHub repo |
| [ds-deadlines](https://github.com/ds-deadlines/ds-deadlines.github.io) | Distributed systems / blockchain / SE | YAML in GitHub repo |
| [klb2/conference-calendar](https://github.com/klb2/conference-calendar) | IEEE Comms / Signal Processing / Vehicular Tech | Per-society YAML |
| [noise-lab](https://noise-lab.net/networking-deadlines/) | Networking deadlines (small set) | HTML scrape |
| [confsearch.ethz.ch](https://confsearch.ethz.ch) | Broad conference search | JSON API (per-acronym query) |

Each aggregator also writes a per-source row to the `source_records` table.
A reconciliation step compares every venue's `submission_deadline` across all
sources and flags any (acronym, year) where two aggregators disagree by more
than one day. Click the red "verify" chip in the UI to see the per-source
breakdown.

Curated layers:

- `backend/data/seed_venues.yaml` — hand-curated CDC, ACC, ECC, L4DC, HSCC,
  SOSP, OSDI, plus workshops (PACMI/HotOS @ SOSP, ML-for-Systems @ NeurIPS,
  HotNets/NetAI @ SIGCOMM). Overwrites aggregator data.
- `backend/data/venue_stats.yaml` — per-acronym h5-index, acceptance rate,
  page limit. Applied to every row regardless of source.
- `backend/data/user_added.yaml` — venues added via the **+ Add venue** button.

## Architecture

```
6 aggregators (in priority order, low → high):
  confsearch → noise-lab → klb2 → ds-deadlines → aideadlines → ccfddl
                          │
                          ├──► conferences table (canonical merged view)
                          └──► source_records table (per-source raw values)

seed_venues.yaml    ──► conferences (overwrites)
user_added.yaml     ──► conferences (overwrites)
LLM enrichment      ──► conferences (two-pass agreement)
reconcile           ──► sets `diverged=true` when source_records disagree
predict             ──► synthesizes next-year rows

           ▼
      SQLite ──► FastAPI ──► dashboard (static HTML/JS) + /calendar.ics
```

Two-pass LLM extraction: each seed/user-added venue's `cfp_url` is sent to
Claude twice with different prompts. A field only updates the DB if both
extractions produce the same value.

Cross-source verification: the reconcile step compares each source's
`submission_deadline` for every (acronym, year). If any two disagree by more
than one day, `diverged=true` is set. Click the red "verify" chip in the UI
to open a modal showing every source's recorded values side-by-side.

## Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional but recommended — enables LLM auto-extraction of seed venues:
export ANTHROPIC_API_KEY=sk-ant-...

# Initial data load:
python -m app.refresh

# Run the server:
uvicorn app.main:app --port 8000
```

Open http://localhost:8000.

No Node, no npm. The frontend is plain HTML/CSS/JS served from
`backend/app/static/` by the same FastAPI process.

## Daily refresh

Add a launchd job (macOS) or cron entry:

```cron
0 4 * * *  cd /path/to/conference-finder/backend && /path/to/.venv/bin/python -m app.refresh >> refresh.log 2>&1
```

The refresh:

1. Pulls latest ccfddl YAMLs and upserts the most recent two years per venue.
2. Re-applies `seed_venues.yaml` and `venue_stats.yaml`.
3. (If `ANTHROPIC_API_KEY` is set) re-extracts dates for seed venues with a `cfp_url`.

## Calendar subscription

Click **Subscribe to calendar** in the UI — the modal gives a filter-aware ICS
URL. Or hit `http://localhost:8000/calendar.ics` directly. Each row in the DB
emits up to five events: abstract / paper / notification / camera-ready
deadlines (each a 30-min block at the local deadline time, normalised to UTC)
and an all-day block for the conference itself.

- **Apple Calendar**: File → New Calendar Subscription → paste URL.
- **Google Calendar**: Other calendars → From URL → paste URL.
  (Google refreshes external feeds every 8–24 h.)
- **Outlook**: Add calendar → Subscribe from web.

## Adding / editing venues

1. **Venue already in ccfddl?** Add a one-line entry to
   `backend/app/sources/ccfddl.py:VENUE_MAP` with the right `areas` and `tier`.
2. **Venue not in ccfddl?** Add a block to `backend/data/seed_venues.yaml`.
   Leave dates `null` and set `cfp_url` — the LLM extractor will fill them in
   on the next refresh (with two-pass verification).
3. Re-run `python -m app.refresh`.

The seed file has an entry for **PACMI @ SOSP** with `cfp_url: null` as a
placeholder; once you confirm the workshop name/URL, edit it and re-refresh.

## Reliability notes

Every row carries `source` (`ccfddl` / `seed` / `llm_extract`) and
`last_verified` timestamp, both visible in the UI's rightmost column. A red
"verify" chip means the two-pass LLM extraction disagreed — those rows are
worth double-checking against the official CFP.

`venue_stats.yaml` is point-in-time. Refresh annually from
[Google Scholar Metrics](https://scholar.google.com/citations?view_op=top_venues)
and conference websites.

## Deploying for free on Render

`render.yaml` at the repo root is a Render Blueprint — Render will read it and
spin up the service automatically. Free tier ($0/month, no credit card).

Steps:

1. **Initialise git and push to GitHub** (Render needs a Git remote):

   ```bash
   cd /path/to/conference-finder
   git init -b main
   git add .
   git commit -m "Initial commit"
   # Create an empty repo on github.com, then:
   git remote add origin git@github.com:YOURUSER/conference-finder.git
   git push -u origin main
   ```

2. **Sign up at <https://dashboard.render.com>** (GitHub login works, no card required).

3. **New → Blueprint → connect the repo**. Render reads `render.yaml`,
   builds the Python service, and gives you a URL like
   `https://conference-finder.onrender.com`.

4. **(Optional)** in the Render dashboard, add `ANTHROPIC_API_KEY` as an
   environment variable on the service. Without it, the **+ Add venue**
   button and LLM-extract step gracefully degrade (other 6 sources still work).

### What to expect

- **Cold start ~60 s.** Free instances sleep after 15 min idle; the next
  request wakes the container, which runs `python -m app.refresh` before
  uvicorn binds. The refresh rebuilds the entire DB from public sources, so
  data is always current — at the cost of that first-request latency.
- **Ephemeral disk.** Anything you add via the **+ Add venue** button (which
  writes `user_added.yaml`) is wiped on every restart. If you want
  persistence, swap SQLite for a Supabase free Postgres tier and persist
  `user_added.yaml` in an object store — out of scope here.
- **Daily refresh:** the cold-start refresh means data is rebuilt every time
  someone visits after a sleep, so you don't need a separate cron job.

## Migrating to paid hosting later

The backend is stateless FastAPI over SQLite. Drop it behind any reverse proxy
(Caddy / nginx). For multi-user hosting with persistence:

- Swap SQLite → Postgres by editing `db.py`.
- Add an auth layer in `main.py` (FastAPI has built-in `Depends`-based auth).
- Containerize: `python:3.12-slim` + `COPY backend/ /app` + `CMD ["uvicorn", ...]`.
