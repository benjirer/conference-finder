"""Persist user-added venues to data/user_added.yaml and load them into the DB.

Kept separate from seed_venues.yaml so that hand-curated comments in the seed
file are never overwritten by auto-managed appends.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml
from dateutil import parser as dparser

from ..db import SessionLocal
from ..models import Conference

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
USER_FILE = DATA_DIR / "user_added.yaml"

_DATE_FIELDS = (
    "abstract_deadline", "submission_deadline", "notification_date",
    "camera_ready", "conference_start", "conference_end",
)


def _load_yaml() -> dict:
    if not USER_FILE.exists():
        return {"venues": []}
    raw = yaml.safe_load(USER_FILE.read_text()) or {}
    raw.setdefault("venues", [])
    return raw


def _save_yaml(raw: dict) -> None:
    USER_FILE.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))


def _parse_iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return dparser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None


def append_and_upsert(venue: dict, source_url: str, diverged_fields: list[str]) -> Conference:
    """Append venue dict to user_added.yaml and upsert into DB. Returns the row.

    `venue` is expected to contain LLM-extracted fields (acronym, name, year, ...).
    Diverged fields are recorded in `notes` for the user to review.
    """
    raw = _load_yaml()
    # Avoid YAML duplicates on the same acronym/year.
    raw["venues"] = [v for v in raw["venues"]
                     if not (v.get("acronym") == venue.get("acronym")
                             and v.get("year") == venue.get("year"))]
    raw["venues"].append({**venue, "cfp_url": source_url})
    _save_yaml(raw)

    now = datetime.utcnow()
    with SessionLocal() as db:
        row = (
            db.query(Conference)
            .filter_by(acronym=venue.get("acronym"), year=venue.get("year"))
            .one_or_none()
        )
        if row is None:
            row = Conference(
                acronym=venue.get("acronym"),
                year=venue.get("year"),
                name=venue.get("name") or venue.get("acronym"),
            )
            db.add(row)
        if "name" in venue and venue["name"]:
            row.name = venue["name"]
        if "areas" in venue and venue["areas"]:
            row.areas = json.dumps(venue["areas"])
        row.is_workshop = bool(venue.get("is_workshop"))
        row.parent_venue = venue.get("parent_venue")
        for f in _DATE_FIELDS:
            if f in venue:
                setattr(row, f, _parse_iso(venue.get(f)))
        if "page_limit" in venue and venue["page_limit"] is not None:
            try:
                row.page_limit = int(venue["page_limit"])
            except (ValueError, TypeError):
                pass
        if "location" in venue and venue["location"]:
            row.location = venue["location"]
        row.cfp_url = source_url
        row.website = source_url
        row.source = "user"
        row.last_verified = now
        row.diverged = bool(diverged_fields)
        if diverged_fields:
            row.notes = (row.notes or "") + (
                "\nLLM passes disagreed on: " + ", ".join(diverged_fields)
            )
        db.commit()
        db.refresh(row)
        return row


def ingest_user_added() -> dict[str, int]:
    raw = _load_yaml()
    upserted = 0
    now = datetime.utcnow()
    with SessionLocal() as db:
        for v in raw.get("venues", []):
            acronym = v.get("acronym")
            year = v.get("year")
            if not acronym or not year:
                continue
            row = db.query(Conference).filter_by(acronym=acronym, year=year).one_or_none()
            if row is None:
                row = Conference(acronym=acronym, year=year, name=v.get("name") or acronym)
                db.add(row)
            row.name = v.get("name") or row.name
            if "areas" in v:
                row.areas = json.dumps(v["areas"])
            if "is_workshop" in v:
                row.is_workshop = bool(v["is_workshop"])
            if "parent_venue" in v:
                row.parent_venue = v["parent_venue"]
            for f in _DATE_FIELDS:
                if f in v:
                    setattr(row, f, _parse_iso(v[f]))
            for f in ("page_limit", "format_notes", "tier", "location", "website", "cfp_url", "notes"):
                if f in v and v[f] is not None:
                    setattr(row, f, v[f])
            # Only stamp the source if the row didn't already come from a more
            # authoritative ingester this run.
            if row.source not in ("ccfddl", "llm_extract"):
                row.source = "user"
            row.last_verified = now
            upserted += 1
        db.commit()
    return {"upserted": upserted}
