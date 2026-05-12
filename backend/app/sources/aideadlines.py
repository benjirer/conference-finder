"""Ingest abhshkdz/ai-deadlines (`aideadlines.org`).

YAML lives at https://raw.githubusercontent.com/abhshkdz/ai-deadlines/master/_data/conferences.yml.
Records new venues, and always writes per-entry SourceRecord rows for
cross-source verification.
"""
from __future__ import annotations

import httpx
import yaml

from ..db import SessionLocal
from . import _common

URL = "https://raw.githubusercontent.com/abhshkdz/ai-deadlines/master/_data/conferences.yml"
SOURCE_NAME = "aideadlines"


def ingest_all() -> dict[str, int]:
    try:
        r = httpx.get(URL, timeout=30.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        return {"error": str(e), "added": 0, "recorded": 0}
    entries = yaml.safe_load(r.text) or []
    added = 0
    recorded = 0
    with SessionLocal() as db:
        for raw in entries:
            n = _common.normalize_aideadlines_entry(raw)
            if not n:
                continue
            areas = _common.map_subs_to_areas(n.get("sub_categories") or [])
            _common.upsert_source_record(
                db,
                acronym=n["acronym"], year=n["year"], source=SOURCE_NAME,
                name=n["name"], link=n["link"],
                abstract_deadline=n["abstract_deadline"],
                submission_deadline=n["submission_deadline"],
                notification_date=n["notification_date"],
                conference_start=n["conference_start"],
                conference_end=n["conference_end"],
                location=n["location"],
            )
            recorded += 1
            if _common.upsert_conference_secondary(
                db,
                acronym=n["acronym"], year=n["year"], name=n["name"],
                areas=areas,
                abstract_deadline=n["abstract_deadline"],
                submission_deadline=n["submission_deadline"],
                notification_date=n["notification_date"],
                conference_start=n["conference_start"],
                conference_end=n["conference_end"],
                location=n["location"],
                website=n["link"], cfp_url=n["link"],
                source_name=SOURCE_NAME,
                h5_index=n.get("hindex") if isinstance(n.get("hindex"), int) else None,
                tier=n.get("rank"),
            ):
                added += 1
        db.commit()
    return {"added": added, "recorded": recorded}
