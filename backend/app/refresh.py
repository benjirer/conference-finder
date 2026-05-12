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
    _run("[ 1/17] confsearch.ethz.ch",       confsearch.ingest_all)
    _run("[ 2/17] noise-lab",                noise_lab.ingest_all)
    _run("[ 3/17] klb2/conference-calendar", klb2.ingest_all)
    _run("[ 4/17] ds-deadlines",             ds_deadlines.ingest_all)
    _run("[ 5/17] aideadlines",              aideadlines.ingest_all)
    _run("[ 6/17] ccfddl",                   ccfddl.ingest_all)
    _run("[ 7/17] seed YAML",                seed.ingest_seed)
    _run("[ 8/17] user_added YAML",          user_venues.ingest_user_added)
    _run("[ 9/17] cleanup old years",        cleanup.cleanup_old_years)
    _run("[10/17] stats overlay",            seed.apply_stats)
    _run("[11/17] cached LLM extras",        cached_extras.apply_cached_extras)
    _run("[12/17] LLM enrichment",           llm_extract.enrich_seed_venues)
    _run("[13/17] classify missing areas",   areas_classify.classify_missing_areas)
    _run("[14/17] geocode locations",        geocode.assign_coordinates)
    _run("[15/17] reconcile",                reconcile.reconcile)
    _run("[16/17] predict next-year",        predict.predict_next_year)
    _run("[17/17] predict missing tiers",    tier_predict.predict_tiers)


if __name__ == "__main__":
    main()
    sys.exit(0)
