"""Persist user-added venues to data/user_added.yaml and load them into the DB.

Kept separate from seed_venues.yaml so that hand-curated comments in the seed
file are never overwritten by auto-managed appends.
"""
from __future__ import annotations

import json
import os
import tempfile
import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml
from dateutil import parser as dparser

from ..db import SessionLocal, DATA_DIR
from ..models import Conference
from . import _common

USER_FILE = DATA_DIR / "user_added.yaml"

_DATE_FIELDS = (
    "abstract_deadline", "submission_deadline", "notification_date",
    "camera_ready", "conference_start", "conference_end",
)


def _load_yaml() -> dict:
    if not USER_FILE.exists():
        return {"venues": []}
    from . import _common as _c
    raw = _c.safe_yaml_load(USER_FILE, {"venues": []})
    if not isinstance(raw, dict):
        raw = {"venues": []}
    raw.setdefault("venues", [])
    return raw


def _save_yaml(raw: dict) -> None:
    USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=USER_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            yaml.safe_dump(raw, stream, sort_keys=False, allow_unicode=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, USER_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _parse_iso(value):
    return _common.parse_iso_date(value)


def append_and_upsert(venue, source_url, diverged_fields):
    USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USER_FILE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rounds = venue.get("rounds")
        main = _append_and_upsert(venue, source_url, diverged_fields)
        if isinstance(rounds, list):
            for entry in rounds:
                if isinstance(entry, dict) and isinstance(entry.get("round"), int) and entry["round"] > 1:
                    _append_and_upsert({**venue, **entry}, source_url, diverged_fields)
        return main


def _append_and_upsert(venue: dict, source_url: str, diverged_fields: list[str]) -> Conference:
    """Append venue dict to user_added.yaml and upsert into DB. Returns the row.

    `venue` is expected to contain LLM-extracted fields (acronym, name, year, ...).
    Diverged fields are recorded in `notes` for the user to review.
    """
    venue = {**venue, "acronym": _common.canonical_acronym(venue.get("acronym"))}
    raw = _load_yaml()
    # Avoid YAML duplicates on the same acronym/year.
    raw["venues"] = [v for v in raw["venues"]
                     if not (v.get("acronym") == venue.get("acronym")
                             and v.get("year") == venue.get("year")
                             and (v.get("round") or 1) == (venue.get("round") or 1))]
    raw["venues"].append({**venue, "cfp_url": source_url})
    _save_yaml(raw)

    from . import _common as _c
    now = _common.utc_now()
    canon = _c.canonical_acronym(venue.get("acronym"))
    with SessionLocal() as db:
        row = (
            db.query(Conference)
            .filter_by(acronym=canon, year=venue.get("year"), round=venue.get("round") or 1)
            .one_or_none()
        )
        if row is None:
            row = Conference(
                acronym=canon,
                year=venue.get("year"),
                round=venue.get("round") or 1,
                name=venue.get("name") or canon,
            )
            db.add(row)
            db.flush()
        if "name" in venue and venue["name"]:
            row.name = venue["name"]
        if "areas" in venue and venue["areas"]:
            row.areas = json.dumps(venue["areas"])
        row.is_workshop = bool(venue.get("is_workshop"))
        row.parent_venue = venue.get("parent_venue")
        _common.promote_prediction(row)
        for f in _DATE_FIELDS:
            if venue.get(f) is not None:
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
    now = _common.utc_now()
    with SessionLocal() as db:
        from . import _common as _c
        for v in raw.get("venues", []):
            acronym = _c.canonical_acronym(v.get("acronym"))
            year = v.get("year")
            if not acronym or not year:
                continue
            round_idx = int(v.get("round") or 1)
            row = db.query(Conference).filter_by(acronym=acronym, year=year, round=round_idx).one_or_none()
            if row is None:
                row = Conference(acronym=acronym, year=year, round=round_idx, name=v.get("name") or acronym)
                db.add(row)
                db.flush()
            row.name = v.get("name") or row.name
            if "areas" in v:
                row.areas = json.dumps(v["areas"])
            if "is_workshop" in v:
                row.is_workshop = bool(v["is_workshop"])
            if "parent_venue" in v:
                row.parent_venue = v["parent_venue"]
            for f in _DATE_FIELDS:
                if v.get(f) is not None and (getattr(row, f) is None or row.source == "user" or row.predicted):
                    parsed = _parse_iso(v[f])
                    if parsed is not None:
                        _common.promote_prediction(row)
                        setattr(row, f, parsed)
            for f in ("page_limit", "format_notes", "tier", "location", "website", "cfp_url", "notes"):
                if f in v and v[f] is not None:
                    setattr(row, f, v[f])
            # Only stamp the source if the row didn't already come from a more
            # authoritative ingester this run.
            if row.source in (None, "seed", "user", "predicted"):
                row.source = "user"
            # Static replay does not count as fresh verification.
            upserted += 1
        db.commit()
    return {"upserted": upserted}
