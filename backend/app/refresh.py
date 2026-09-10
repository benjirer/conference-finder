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
import json
import os
from pathlib import Path

from .db import init_db
from .sources import (
    ccfddl, seed, user_venues, llm_extract, predict, reconcile,
    aideadlines, ds_deadlines, klb2, noise_lab, confsearch, tier_predict, cleanup,
    cached_extras, areas_classify, geocode, cached_pc, official,
)


def _run(label, fn):
    print(f"{label}...")
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 — top-level pipeline guard
        print(f"      ERROR: {e!r}")
        return
    print(f"      {result}")
    return result


def main():
    init_db()
    from .review_updates import import_approved
    results = {}

    def run(label, fn):
        results[label] = _run(label, fn)

    run("[aliases] consolidate venue aliases", cleanup.canonicalize_existing)
    run("[ 1/17] confsearch.ethz.ch",       confsearch.ingest_all)
    run("[ 2/17] noise-lab",                noise_lab.ingest_all)
    run("[ 3/17] klb2/conference-calendar", klb2.ingest_all)
    run("[ 4/17] ds-deadlines",             ds_deadlines.ingest_all)
    run("[ 5/17] aideadlines",              aideadlines.ingest_all)
    run("[ 6/17] ccfddl",                   ccfddl.ingest_all)
    run("[ 7/17] seed YAML",                seed.ingest_seed)
    run("[ 8/17] user_added YAML",          user_venues.ingest_user_added)
    run("[reviewed] approved venue updates", import_approved)
    run("[ 9/17] cleanup old years",        cleanup.cleanup_old_years)
    run("[10/17] stats overlay",            seed.apply_stats)
    run("[11/17] cached LLM extras",        cached_extras.apply_cached_extras)
    run("[12/17] cached PC members",        cached_pc.apply_cached_pc)
    run("[13/17] classify missing areas",   areas_classify.classify_missing_areas)
    run("[14/17] geocode locations",        geocode.assign_coordinates)
    run("[16/17] predict next-year",        predict.predict_next_year)
    run("[17/17] predict missing tiers",    tier_predict.predict_tiers)
    run("[official] verify CFP pages",       official.refresh_official)
    run("[15/17] reconcile",                reconcile.reconcile)

    return results


if __name__ == "__main__":
    results = main()
    report_path = os.environ.get('CONFERENCE_FINDER_REFRESH_REPORT')
    if report_path:
        Path(report_path).write_text(json.dumps(results, indent=2))
    # Unexpected pipeline exceptions must not silently pass deployment checks.
    sys.exit(1 if any(value is None for value in results.values()) else 0)
