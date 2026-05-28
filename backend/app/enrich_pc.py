"""Local one-shot extraction of program-committee members per venue.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m app.enrich_pc [--limit N] [--force] [--acronym ACRONYM]
                            [--missing-url-only] [--retry-empty]

For each conference (round=1) with a `cfp_url`:
  1. If pc_url isn't known yet, ask the LLM to discover one (or get an inline list).
  2. Fetch the PC page and extract members via Sonnet.
  3. Cache results in backend/data/cached_pc.yaml.

Commit the YAML so Render picks up enriched PCs without making any API calls.
Re-run after committees change (typically annually).

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

CACHE_FILENAME = "cached_pc.yaml"
TTL_DAYS = 90  # PCs change annually; 90 days is safely fresh

_VALID_ROLES = {"member", "area-chair", "track-chair", "program-chair", "general-chair"}


def _clean_members(raw: list) -> list[dict]:
    """Normalise the role + drop bad rows."""
    out: list[dict] = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        name = m.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        role = m.get("role") or "member"
        if role not in _VALID_ROLES:
            role = "member"
        aff = m.get("affiliation")
        out.append({
            "name": name.strip(),
            "affiliation": (aff.strip() if isinstance(aff, str) and aff.strip() else None),
            "role": role,
        })
    return out


def run(*, limit: int | None = None, force: bool = False,
        acronym: str | None = None, missing_url_only: bool = False,
        retry_empty: bool = False) -> dict[str, Any]:
    """Programmatic entry point used by app.enrich."""
    if not llm_extract.ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY env var not set.")
        return {"error": "no_api_key"}

    init_db()
    cache = EnrichmentCache(CACHE_FILENAME, TTL_DAYS)
    siblings = cache.all_pc_urls_by_acronym()

    with SessionLocal() as db:
        rows = (
            db.query(Conference)
            .filter(Conference.cfp_url.isnot(None))
            .filter(Conference.round == 1)
            .filter(Conference.predicted == False)  # noqa: E712
            .order_by(Conference.submission_deadline.asc().nulls_last())
            .all()
        )

    if acronym:
        rows = [r for r in rows if r.acronym.lower() == acronym.lower()]

    processed = 0
    failed = 0
    skipped_fresh = 0
    members_total = 0

    for r in rows:
        if limit is not None and processed >= limit:
            break
        existing = cache.get(r.acronym, r.year)
        if not force and not retry_empty and cache.is_fresh(r.acronym, r.year):
            skipped_fresh += 1
            continue
        if retry_empty and existing and len(existing.get("members") or []) > 10 and not force:
            skipped_fresh += 1
            continue
        if missing_url_only and r.pc_url:
            continue

        print(f"[{processed + 1}/{len(rows)}] {r.acronym} {r.year} ← {r.cfp_url}")

        pc_url = r.pc_url
        inline_members = None
        if not pc_url:
            pc_url, inline_members = llm_extract.discover_pc_url(
                r.cfp_url, r.website,
                target_year=r.year,
                sibling_pc_urls=siblings.get(r.acronym, []),
            )
            if pc_url:
                print(f"    pc_url discovered: {pc_url}")
            elif inline_members is not None:
                print(f"    inline PC list found on CFP page ({len(inline_members)} candidates)")
            else:
                print(f"    (could not discover PC URL or inline list — skipping)")
                failed += 1
                continue

        if inline_members is not None:
            members = inline_members
        else:
            members = llm_extract.extract_pc_members(pc_url)
            if members is None:
                print(f"    (extraction failed — page unreachable or no members detected)")
                failed += 1
                continue

        clean = _clean_members(members)
        entry = {"pc_url": pc_url, "members": clean, "cfp_url": r.cfp_url}
        cache.put(r.acronym, r.year, entry)
        members_total += len(clean)
        processed += 1
        print(f"    {len(clean)} members captured")

    print()
    print(f"Done. processed={processed}, failed={failed}, skipped_fresh={skipped_fresh}, members_total={members_total}")
    print(f"Cache: {cache.path}")
    return {
        "processed": processed, "failed": failed,
        "skipped_fresh": skipped_fresh, "members_total": members_total,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--acronym", type=str, default=None)
    parser.add_argument("--missing-url-only", action="store_true",
                        help="Skip venues already in seed/cache with a known pc_url, "
                             "only run discovery for venues without one")
    parser.add_argument("--retry-empty", action="store_true",
                        help="Re-run venues currently in cache with ≤10 members "
                             "(catches impostor-page extractions like Shadow PC / Ethics committee).")
    args = parser.parse_args()
    result = run(
        limit=args.limit, force=args.force, acronym=args.acronym,
        missing_url_only=args.missing_url_only, retry_empty=args.retry_empty,
    )
    if result.get("error") == "no_api_key":
        sys.exit(1)


if __name__ == "__main__":
    main()
