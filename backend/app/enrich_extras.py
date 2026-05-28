"""Local one-shot enrichment of every venue with a `cfp_url`.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m app.enrich_extras [--limit N] [--force] [--acronym ACRONYM]

For each venue in the DB:
  1. Skip if `cfp_url` is null
  2. Skip if cached_extras.yaml already has a fresh entry (unless --force)
  3. Run two-pass-plus-fallback LLM extraction against the cfp_url
  4. Append result to backend/data/cached_extras.yaml
  5. Commit the file to git so Render picks it up on next deploy

Re-run periodically (~monthly) — the cache TTL avoids re-doing fresh work.

To run BOTH extras and PC enrichment in one go, use `python -m app.enrich` instead.
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from ._enrich_common import EnrichmentCache
from .db import SessionLocal, init_db
from .models import Conference
from .sources import llm_extract

log = logging.getLogger("conference_finder")

CACHE_FILENAME = "cached_extras.yaml"
TTL_DAYS = 30  # re-extract entries older than this


def run(*, limit: int | None = None, force: bool = False,
        acronym: str | None = None) -> dict[str, Any]:
    """Programmatic entry point used by app.enrich. Returns counters."""
    if not llm_extract.ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY env var not set.")
        return {"error": "no_api_key"}

    init_db()
    cache = EnrichmentCache(CACHE_FILENAME, TTL_DAYS)

    with SessionLocal() as db:
        rows = (
            db.query(Conference)
            .filter(Conference.cfp_url.isnot(None))
            .filter(Conference.predicted == False)  # noqa: E712
            .order_by(Conference.submission_deadline.asc().nulls_last())
            .all()
        )

    if acronym:
        rows = [r for r in rows if r.acronym.lower() == acronym.lower()]

    processed = 0
    failed = 0
    skipped_fresh = 0
    for r in rows:
        if limit is not None and processed >= limit:
            break
        if not force and cache.is_fresh(r.acronym, r.year):
            skipped_fresh += 1
            continue
        print(f"[{processed + 1}/{len(rows)}] {r.acronym} {r.year} ← {r.cfp_url}")
        result = llm_extract.extract_venue_extras(r.cfp_url, r.acronym, r.year)
        if not result:
            print("    (extraction failed — page unreachable, API error, or no usable content)")
            failed += 1
            continue
        entry: dict[str, Any] = {"cfp_url": r.cfp_url}
        for f in llm_extract.EXTRACT_FIELDS:
            if result.get(f) is not None:
                entry[f] = result[f]
        if isinstance(result.get("rounds"), list) and result["rounds"]:
            entry["rounds"] = result["rounds"]
        if result.get("_used_strong"):
            entry["model"] = "sonnet"
        if result.get("_diverged"):
            entry["diverged_fields"] = list(result["_diverged"])
        cache.put(r.acronym, r.year, entry)
        processed += 1

    print()
    print(f"Done. processed={processed}, failed={failed}, skipped_fresh={skipped_fresh}")
    print(f"Cache written: {cache.path}")
    return {"processed": processed, "failed": failed, "skipped_fresh": skipped_fresh}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="cap on number of venues to process this run")
    parser.add_argument("--force", action="store_true",
                        help="re-extract even if cache entry is fresh")
    parser.add_argument("--acronym", type=str, default=None,
                        help="only process this acronym")
    args = parser.parse_args()
    result = run(limit=args.limit, force=args.force, acronym=args.acronym)
    if result.get("error") == "no_api_key":
        sys.exit(1)


if __name__ == "__main__":
    main()
