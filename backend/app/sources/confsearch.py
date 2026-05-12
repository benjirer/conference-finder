"""Cross-check dates against confsearch.ethz.ch.

confsearch is a search engine — the only API endpoint that returns data is
`/api/search-engine/?query=<term>`. We query it once per distinct acronym
already in the DB to gather a cross-source record, plus a small set of seed
queries to pick up venues we might be missing.

Date format from confsearch is free-form:
    "Jan 26, 2024 (Feb 02, 2024)"          → first = abstract, paren = paper
    "Jul 24, 2024; Jul 31, 2024;"          → multiple rounds, semi-colons
    "Jan 26, 2024"                         → single date
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx
from dateutil import parser as dparser
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Conference
from . import _common

URL = "https://confsearch.ethz.ch/api/search-engine/?query={q}"
SOURCE_NAME = "confsearch"

# Extra queries we always run so we can discover venues the other aggregators miss.
SEED_QUERIES = [
    "control", "decision", "learning", "networking", "robotics",
    "communications", "signal processing", "information theory",
]


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return dparser.parse(s, fuzzy=True)
    except (ValueError, TypeError, OverflowError):
        return None


def _split_deadline_field(raw: str | None) -> tuple[datetime | None, datetime | None]:
    """Returns (abstract_deadline, submission_deadline). Handles their formats."""
    if not raw:
        return None, None
    # "Jan 26, 2024 (Feb 02, 2024)" pattern.
    m = re.match(r"^(.+?)\s*\((.+?)\)\s*$", raw)
    if m:
        abstract = _parse_date(m.group(1))
        submission = _parse_date(m.group(2))
        # confsearch swap convention seems unstable; assume parens = the
        # later/main paper deadline.
        if abstract and submission and abstract > submission:
            abstract, submission = submission, abstract
        return abstract, submission
    # "Jul 24, 2024; Jul 31, 2024;" — pick the latest.
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    if not parts:
        return None, None
    parsed = [_parse_date(p) for p in parts]
    parsed = [p for p in parsed if p]
    if not parsed:
        return None, None
    parsed.sort()
    return (parsed[0] if len(parsed) > 1 else None), parsed[-1]


def _fetch(query: str) -> list[dict]:
    try:
        r = httpx.get(URL.format(q=query), timeout=20.0,
                      headers={"Content-Type": "application/json"})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [v for v in data.values() if isinstance(v, dict)]


def ingest_all() -> dict[str, int]:
    """Query confsearch for each distinct acronym in our DB + a few seed terms.

    Filter aggressively: only keep entries where confsearch's `start` date
    suggests a conference in [last_year .. last_year + 3]. Anything older is
    stale and not worth cross-checking.
    """
    recorded = 0
    added = 0
    queries: set[str] = set(SEED_QUERIES)
    with SessionLocal() as db:
        for row in db.execute(select(Conference.acronym).distinct()).all():
            queries.add(row[0])

    now = datetime.utcnow()
    year_min = now.year - 1
    year_max = now.year + 3

    seen: dict[tuple[str, int], dict] = {}
    skipped_old = 0
    skipped_deleted = 0
    for q in queries:
        for item in _fetch(q):
            if item.get("deleted"):
                skipped_deleted += 1
                continue
            acronym = (item.get("acronym") or item.get("id_acro") or "").strip()
            if not acronym:
                continue
            start = _parse_date(item.get("start"))
            end = _parse_date(item.get("end"))
            abstract, submission = _split_deadline_field(item.get("deadline"))
            notif = _parse_date(item.get("notification"))
            year_basis = start or submission or notif
            if year_basis is None:
                continue
            year = year_basis.year
            if year < year_min or year > year_max:
                skipped_old += 1
                continue
            key = (acronym, year)
            if key in seen:
                continue
            seen[key] = {
                "acronym": acronym, "year": year,
                "name": item.get("name") or acronym,
                "link": item.get("www"),
                "location": item.get("location"),
                "abstract_deadline": abstract,
                "submission_deadline": submission,
                "notification_date": notif,
                "conference_start": start,
                "conference_end": end,
                "rank": item.get("rank"),
            }

    with SessionLocal() as db:
        for v in seen.values():
            _common.upsert_source_record(
                db,
                acronym=v["acronym"], year=v["year"], source=SOURCE_NAME,
                name=v["name"], link=v["link"],
                abstract_deadline=v["abstract_deadline"],
                submission_deadline=v["submission_deadline"],
                notification_date=v["notification_date"],
                conference_start=v["conference_start"],
                conference_end=v["conference_end"],
                location=v["location"],
            )
            recorded += 1
            if _common.upsert_conference_secondary(
                db,
                acronym=v["acronym"], year=v["year"], name=v["name"],
                abstract_deadline=v["abstract_deadline"],
                submission_deadline=v["submission_deadline"],
                notification_date=v["notification_date"],
                conference_start=v["conference_start"],
                conference_end=v["conference_end"],
                location=v["location"],
                website=v["link"], cfp_url=v["link"],
                source_name=SOURCE_NAME,
                tier=v["rank"],
            ):
                added += 1
        db.commit()
    return {"added": added, "recorded": recorded, "queries": len(queries),
            "skipped_old": skipped_old, "skipped_deleted": skipped_deleted}
