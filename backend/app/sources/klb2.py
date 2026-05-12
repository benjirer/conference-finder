"""Ingest klb2/conference-calendar.

The repo has multiple society YAMLs under `data/`:
    comsoc.yml   IEEE Communications Society      (ICC, GLOBECOM, WCNC, VTC...)
    eurasip.yml  European Signal Processing
    itsoc.yml    IEEE Information Theory
    itss.yml     IEEE Intelligent Transportation
    spsoc.yml    IEEE Signal Processing
    vde.yml      VDE (German EE)
    vts.yml      IEEE Vehicular Technology

Each entry has:
    name, abbreviation, url, organization, topic, type
    dates:     list of [start_date, end_date] pairs (one per year)
    deadline:  list of dates (one per year), but indexed via the *previous*
               year (so dates[0] is for 2023 -> deadline[0] is for the 2023
               conference, taken late 2022). Their HTML aligns them by index.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, date as date_cls

import httpx

from ..db import SessionLocal
from . import _common

SOURCE_NAME = "klb2"

DATA_FILES = ["comsoc.yml", "eurasip.yml", "itsoc.yml", "itss.yml",
              "spsoc.yml", "vde.yml", "vts.yml"]
GH_API = "https://api.github.com/repos/klb2/conference-calendar/contents/data"

# Society → our internal areas.
SOCIETY_AREAS: dict[str, list[str]] = {
    "comsoc":  ["networking"],
    "eurasip": ["ml"],
    "itsoc":   ["networking", "ml"],
    "itss":    ["control"],
    "spsoc":   ["ml"],
    "vde":     ["control"],
    "vts":     ["networking", "control"],
}


def _to_dt(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date_cls):
        return datetime(val.year, val.month, val.day)
    return _common.parse_iso_date(str(val))


def _fetch_file(filename: str) -> list | None:
    try:
        r = httpx.get(f"{GH_API}/{filename}", timeout=30.0)
        r.raise_for_status()
        meta = r.json()
        content = base64.b64decode(meta["content"]).decode()
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    import yaml
    try:
        return yaml.safe_load(content) or []
    except yaml.YAMLError:
        return None


def ingest_all() -> dict[str, int]:
    added = 0
    recorded = 0
    errors = 0
    with SessionLocal() as db:
        for filename in DATA_FILES:
            society = filename.replace(".yml", "")
            areas = SOCIETY_AREAS.get(society, [])
            entries = _fetch_file(filename)
            if entries is None:
                errors += 1
                continue
            for raw in entries:
                acronym = (raw.get("abbreviation") or "").strip()
                name = (raw.get("name") or acronym).strip()
                if not acronym:
                    continue
                dates_list = raw.get("dates") or []
                deadlines = raw.get("deadline") or []
                # Their index alignment: dates[i] is conference i; deadline[i]
                # is the call-for-papers deadline that *precedes* dates[i].
                for i, conf_dates in enumerate(dates_list):
                    if not conf_dates:
                        continue
                    try:
                        start = _to_dt(conf_dates[0])
                        end = _to_dt(conf_dates[1]) if len(conf_dates) > 1 else start
                    except (TypeError, IndexError):
                        continue
                    if start is None:
                        continue
                    year = start.year
                    deadline = _to_dt(deadlines[i]) if i < len(deadlines) else None
                    _common.upsert_source_record(
                        db,
                        acronym=acronym, year=year, source=SOURCE_NAME,
                        name=name, link=raw.get("url"),
                        submission_deadline=deadline,
                        conference_start=start, conference_end=end,
                    )
                    recorded += 1
                    if _common.upsert_conference_secondary(
                        db,
                        acronym=acronym, year=year, name=name,
                        areas=areas,
                        submission_deadline=deadline,
                        conference_start=start, conference_end=end,
                        website=raw.get("url"), cfp_url=raw.get("url"),
                        source_name=SOURCE_NAME,
                        is_workshop=(raw.get("type") == "workshop"),
                    ):
                        added += 1
        db.commit()
    return {"added": added, "recorded": recorded, "file_errors": errors}
