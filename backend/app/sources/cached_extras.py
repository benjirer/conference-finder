"""Apply cached LLM-extracted extras (page_limit, acceptance_rate, notification,
camera_ready, abstract_deadline, multi-round info) onto canonical rows.

Source file: backend/data/cached_extras.yaml — written by `python -m
app.enrich_extras`. Loaded during refresh as an overlay AFTER venue_stats.yaml
so curated stats still win.

For each cache entry:
  - Find the canonical (acronym, year, round=1) row; fill any null field
  - If `rounds: [...]` is present, upsert one Conference row per round
  - Never overwrite a non-null value (cache is a backfill, not a source of truth)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from dateutil import parser as dparser

from ..db import SessionLocal
from ..models import Conference

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "cached_extras.yaml"

_DATE_FIELDS = (
    "abstract_deadline", "submission_deadline", "notification_date",
    "camera_ready", "conference_start", "conference_end",
)


def _parse(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return dparser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None


def _fill_row(row: Conference, entry: dict) -> int:
    """Fill any null field on row from entry. Returns count of fields updated."""
    updated = 0
    for f in _DATE_FIELDS:
        if f in entry and getattr(row, f) is None:
            parsed = _parse(entry[f])
            if parsed is not None:
                setattr(row, f, parsed.replace(tzinfo=None))
                updated += 1
    if "page_limit" in entry and row.page_limit is None:
        try:
            row.page_limit = int(entry["page_limit"])
            updated += 1
        except (ValueError, TypeError):
            pass
    if "location" in entry and not row.location:
        row.location = str(entry["location"])
        updated += 1
    return updated


def apply_cached_extras() -> dict[str, int]:
    if not CACHE_FILE.exists():
        return {"applied": 0, "reason": "no cached_extras.yaml"}
    raw = yaml.safe_load(CACHE_FILE.read_text()) or {}
    entries = raw.get("entries", [])
    applied = 0
    rounds_added = 0
    fields_updated = 0

    from . import _common as _c
    with SessionLocal() as db:
        for entry in entries:
            acronym = _c.canonical_acronym(entry.get("acronym"))
            year = entry.get("year")
            if not acronym or not year:
                continue

            rounds_data = entry.get("rounds")
            if isinstance(rounds_data, list) and rounds_data:
                # Multi-round venue: create one row per round.
                rounds_total = len(rounds_data)
                for round_entry in rounds_data:
                    try:
                        round_idx = int(round_entry.get("round") or 1)
                    except (ValueError, TypeError):
                        continue
                    row = (
                        db.query(Conference)
                        .filter_by(acronym=acronym, year=year, round=round_idx)
                        .one_or_none()
                    )
                    if row is None:
                        # Clone fields from round-1 row if present.
                        template = (
                            db.query(Conference)
                            .filter_by(acronym=acronym, year=year, round=1)
                            .one_or_none()
                        )
                        if template is None:
                            continue
                        row = Conference(
                            acronym=acronym, year=year, round=round_idx,
                            name=template.name,
                            areas=template.areas,
                            tier=template.tier,
                            h5_index=template.h5_index,
                            cfp_url=template.cfp_url,
                            website=template.website,
                            source="llm_extract",
                            last_verified=datetime.utcnow(),
                        )
                        db.add(row)
                        db.flush()
                        rounds_added += 1
                    row.rounds_total = rounds_total
                    fields_updated += _fill_row(row, round_entry)
                # Also fill any non-round fields on the main row.
                main = (
                    db.query(Conference)
                    .filter_by(acronym=acronym, year=year, round=1)
                    .one_or_none()
                )
                if main is not None:
                    main.rounds_total = rounds_total
                    fields_updated += _fill_row(main, entry)
                applied += 1
            else:
                row = (
                    db.query(Conference)
                    .filter_by(acronym=acronym, year=year, round=1)
                    .one_or_none()
                )
                if row is None:
                    continue
                fields_updated += _fill_row(row, entry)
                applied += 1
        db.commit()
    return {"applied": applied, "rounds_added": rounds_added, "fields_updated": fields_updated}
