"""Load curated YAML venues + apply stats overlay onto every conference row."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml
from dateutil import parser as dparser

from ..db import SessionLocal
from ..models import Conference

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SEED_FILE = DATA_DIR / "seed_venues.yaml"
STATS_FILE = DATA_DIR / "venue_stats.yaml"

_DATE_FIELDS = (
    "abstract_deadline", "submission_deadline", "notification_date",
    "camera_ready", "conference_start", "conference_end",
)


def _parse_iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return dparser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None


def ingest_seed() -> dict[str, int]:
    if not SEED_FILE.exists():
        return {"upserted": 0}
    raw = yaml.safe_load(SEED_FILE.read_text()) or {}
    venues = raw.get("venues", [])
    now = datetime.utcnow()
    upserted = 0
    with SessionLocal() as db:
        for v in venues:
            from . import _common as _c
            acronym = _c.canonical_acronym(v.get("acronym"))
            year = v.get("year")
            if not acronym or not year:
                continue
            round_idx = int(v.get("round") or 1)
            row = db.query(Conference).filter_by(acronym=acronym, year=year, round=round_idx).one_or_none()
            if row is None:
                row = Conference(acronym=acronym, year=year, round=round_idx, name=v.get("name", acronym))
                db.add(row)
                db.flush()
            row.name = v.get("name") or row.name
            if "areas" in v:
                row.areas = json.dumps(v["areas"])
            if "topics" in v:
                row.topics = json.dumps(v["topics"])
            for f in _DATE_FIELDS:
                if f in v:
                    setattr(row, f, _parse_iso(v[f]))
            for f in ("page_limit", "format_notes", "tier", "location",
                      "website", "cfp_url", "is_workshop", "parent_venue", "notes"):
                if f in v:
                    setattr(row, f, v[f])
            row.source = "seed"
            row.last_verified = now
            upserted += 1
        db.commit()
    return {"upserted": upserted}


def apply_stats() -> dict[str, int]:
    """Overlay stats (h5_index, acceptance_rate, page_limit, format_notes) keyed by acronym."""
    if not STATS_FILE.exists():
        return {"updated": 0}
    raw = yaml.safe_load(STATS_FILE.read_text()) or {}
    stats = raw.get("stats", {})
    updated = 0
    with SessionLocal() as db:
        from . import _common as _c
        for acronym, fields in stats.items():
            acronym = _c.canonical_acronym(acronym)
            rows = db.query(Conference).filter_by(acronym=acronym).all()
            for row in rows:
                for k, val in fields.items():
                    # Don't overwrite a non-null page_limit/format_notes already
                    # populated by a more specific seed entry.
                    if k in ("page_limit", "format_notes") and getattr(row, k):
                        continue
                    setattr(row, k, val)
                updated += 1
        db.commit()
    return {"updated": updated}
