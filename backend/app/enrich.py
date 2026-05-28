"""Single-command enrichment pipeline.

Runs both `enrich_extras` and `enrich_pc` against the current DB, then refreshes
the canonical conferences table to apply the new caches.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python -m app.enrich                  # extras + pc + refresh (the common case)
    python -m app.enrich --no-refresh     # extras + pc only
    python -m app.enrich --kinds extras   # extras only
    python -m app.enrich --kinds pc       # pc only
    python -m app.enrich --acronym SIGCOMM --force   # re-extract one venue (both kinds)

After running, commit the updated cached_extras.yaml + cached_pc.yaml so Render
picks them up without making any API calls of its own.
"""
from __future__ import annotations

import argparse
import sys

from . import enrich_extras, enrich_pc, refresh


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run all LLM-based enrichments and refresh the DB."
    )
    parser.add_argument(
        "--kinds", default="extras,pc",
        help="Comma-separated subset of {extras,pc} (default: both)",
    )
    parser.add_argument(
        "--no-refresh", action="store_true",
        help="Skip the final `app.refresh` step (useful when you'll run it yourself).",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap on venues processed per kind.")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract even when the cache entry is fresh.")
    parser.add_argument("--acronym", type=str, default=None,
                        help="Only process this acronym (case-insensitive).")
    args = parser.parse_args()

    kinds = {k.strip().lower() for k in args.kinds.split(",") if k.strip()}
    unknown = kinds - {"extras", "pc"}
    if unknown:
        print(f"ERROR: unknown kinds: {sorted(unknown)}. Valid: extras, pc.")
        sys.exit(2)

    any_failed = False

    if "extras" in kinds:
        print()
        print("═" * 60)
        print("  Enriching extras (dates, page_limit, accept-rate, rounds…)  ")
        print("═" * 60)
        r = enrich_extras.run(limit=args.limit, force=args.force, acronym=args.acronym)
        if r.get("error") == "no_api_key":
            any_failed = True

    if "pc" in kinds:
        print()
        print("═" * 60)
        print("  Enriching PC members                                        ")
        print("═" * 60)
        r = enrich_pc.run(limit=args.limit, force=args.force, acronym=args.acronym)
        if r.get("error") == "no_api_key":
            any_failed = True

    if not args.no_refresh:
        print()
        print("═" * 60)
        print("  Refreshing canonical DB to apply caches                     ")
        print("═" * 60)
        refresh.main()

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
