"""Local one-shot enrichment of every venue with a `cfp_url`.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m app.enrich_extras [--limit N] [--force]

For each venue in the DB:
  1. Skip if `cfp_url` is null
  2. Skip if cached_extras.yaml already has a fresh entry (unless --force)
  3. Run two-pass-plus-fallback LLM extraction against the cfp_url
  4. Append result to backend/data/cached_extras.yaml
  5. Commit the file to git so Render picks it up on next deploy

The refresh pipeline loads cached_extras.yaml as an overlay (applied AFTER
venue_stats.yaml), filling in null fields per (acronym, year, round).

Designed to be re-run periodically (~monthly) — caching by `last_extracted`
timestamp avoids re-doing work that's still fresh.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .db import SessionLocal, init_db
from .models import Conference
from .sources import llm_extract

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_FILE = DATA_DIR / "cached_extras.yaml"
CACHE_TTL_DAYS = 30  # re-extract if older than this


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"entries": []}
    raw = yaml.safe_load(CACHE_FILE.read_text()) or {}
    raw.setdefault("entries", [])
    return raw


def _save_cache(raw: dict) -> None:
    CACHE_FILE.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))


def _cache_key(acronym: str, year: int) -> str:
    return f"{acronym}|{year}"


def _is_fresh(entry: dict) -> bool:
    ts = entry.get("extracted_at")
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(str(ts))
    except ValueError:
        return False
    return (datetime.utcnow() - when) < timedelta(days=CACHE_TTL_DAYS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="cap on number of venues to process this run")
    parser.add_argument("--force", action="store_true", help="re-extract even if cache entry is fresh")
    parser.add_argument("--acronym", type=str, default=None, help="only process this acronym")
    args = parser.parse_args()

    if not llm_extract.ANTHROPIC_KEY:
        print("ERROR: ANTHROPIC_API_KEY env var not set.")
        sys.exit(1)

    init_db()
    cache = _load_cache()
    entries_by_key = {_cache_key(e["acronym"], e["year"]): e for e in cache["entries"]}

    with SessionLocal() as db:
        rows = (
            db.query(Conference)
            .filter(Conference.cfp_url.isnot(None))
            .filter(Conference.predicted == False)  # noqa: E712 — SQLAlchemy idiom
            .order_by(Conference.submission_deadline.asc().nulls_last())
            .all()
        )

    if args.acronym:
        rows = [r for r in rows if r.acronym.lower() == args.acronym.lower()]

    processed = 0
    failed = 0
    skipped_fresh = 0
    for r in rows:
        if args.limit is not None and processed >= args.limit:
            break
        key = _cache_key(r.acronym, r.year)
        existing = entries_by_key.get(key)
        if existing and not args.force and _is_fresh(existing):
            skipped_fresh += 1
            continue
        print(f"[{processed + 1}/{len(rows)}] {r.acronym} {r.year} ← {r.cfp_url}")
        result = llm_extract.extract_venue_extras(r.cfp_url, r.acronym, r.year)
        if not result:
            print("    (extraction failed — page unreachable, API error, or no usable content)")
            failed += 1
            continue
        entry = {
            "acronym": r.acronym,
            "year": r.year,
            "extracted_at": datetime.utcnow().isoformat(timespec="seconds"),
            "cfp_url": r.cfp_url,
        }
        for f in llm_extract.EXTRACT_FIELDS:
            if result.get(f) is not None:
                entry[f] = result[f]
        if isinstance(result.get("rounds"), list) and result["rounds"]:
            entry["rounds"] = result["rounds"]
        if result.get("_used_strong"):
            entry["model"] = "sonnet"
        if result.get("_diverged"):
            entry["diverged_fields"] = list(result["_diverged"])
        entries_by_key[key] = entry
        cache["entries"] = list(entries_by_key.values())
        _save_cache(cache)  # write after each entry so partial runs aren't lost
        processed += 1

    print()
    print(f"Done. processed={processed}, failed={failed}, skipped_fresh={skipped_fresh}")
    print(f"Cache written: {CACHE_FILE}")


if __name__ == "__main__":
    main()
