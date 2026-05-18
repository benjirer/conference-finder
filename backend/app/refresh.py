"""Top-level refresh pipeline.

Order matters: lower-priority aggregators run first so higher-priority sources
overwrite them in the canonical `conferences` table. Each aggregator ALSO
writes to `source_records` so the reconcile step can detect cross-source
disagreements regardless of which one's data ended up canonical.

  1.  confsearch         (lowest priority — stale data, broad coverage)
  2.  noise-lab          (HTML scrape — networking only)
  3.  klb2               (per-society YAML — comms/IT/signal proc)
  4.  ds-deadlines       (YAML — distributed systems/blockchain/SE)
  5.  aideadlines        (YAML — AI/CV/NLP)
  6.  ccfddl             (YAML — broad CS, our primary aggregator)
  7.  seed YAML          (curated control venues + workshops)
  8.  user_added YAML    (venues added via /api/venues)
  9.  stats overlay      (h5_index / acceptance_rate / page_limit per acronym)
  10. LLM enrichment     (two-pass extraction for seed venues with cfp_url)
  11. reconcile          (cross-source verification: sets diverged flag)
  12. predict            (synthesize next-year rows where missing)

Run with:   python -m app.refresh
"""
from __future__ import annotations

import sys

from .db import init_db
from .sources import (
    ccfddl, seed, user_venues, llm_extract, predict, reconcile,
    aideadlines, ds_deadlines, klb2, noise_lab, confsearch, tier_predict, cleanup,
    cached_extras, areas_classify, geocode,
)


def _run(label, fn):
    print(f"{label}...")
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 — top-level pipeline guard
        print(f"      ERROR: {e!r}")
        return
    print(f"      {result}")


def main():
    init_db()
    _run("[ 1/16] confsearch.ethz.ch",       confsearch.ingest_all)
    _run("[ 2/16] noise-lab",                noise_lab.ingest_all)
    _run("[ 3/16] klb2/conference-calendar", klb2.ingest_all)
    _run("[ 4/16] ds-deadlines",             ds_deadlines.ingest_all)
    _run("[ 5/16] aideadlines",              aideadlines.ingest_all)
    _run("[ 6/16] ccfddl",                   ccfddl.ingest_all)
    _run("[ 7/16] seed YAML",                seed.ingest_seed)
    _run("[ 8/16] user_added YAML",          user_venues.ingest_user_added)
    _run("[ 9/16] cleanup old years",        cleanup.cleanup_old_years)
    _run("[10/16] stats overlay",            seed.apply_stats)
    _run("[11/16] cached LLM extras",        cached_extras.apply_cached_extras)
    # The live `llm_extract.enrich_seed_venues` step was removed from the refresh
    # pipeline on purpose: it ran on every Render cold start and burned ~30 Anthropic
    # API calls per wake. Coverage is provided instead by the locally-curated
    # `cached_extras.yaml` (step 11). To refresh enrichment data, run
    # `python -m app.enrich_extras` locally and commit the updated cache.
    # The `+ Add venue` flow still uses `llm_extract.extract_full_venue` on demand,
    # which is fine because it's user-triggered, not automatic.
    _run("[12/16] classify missing areas",   areas_classify.classify_missing_areas)
    _run("[13/16] geocode locations",        geocode.assign_coordinates)
    _run("[14/16] reconcile",                reconcile.reconcile)
    _run("[15/16] predict next-year",        predict.predict_next_year)
    _run("[16/16] predict missing tiers",    tier_predict.predict_tiers)


if __name__ == "__main__":
    main()
    sys.exit(0)
