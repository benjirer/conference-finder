"""Shared helpers for the multi-source ingestion pipeline.

- `upsert_source_record` writes one row to the source_records table.
- `upsert_conference_if_missing` adds a venue to the canonical conferences
  table only if a row for that (acronym, year) doesn't already exist —
  secondary sources should not overwrite ccfddl/seed/user data.
- `parse_aideadlines_yaml` decodes a list-of-conferences YAML in the shape
  used by both aideadlines and ds-deadlines.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Conference, SourceRecord


_TIER_MAP = {
    # CORE-style ranks
    "A*": "A*", "A**": "A*", "AA": "A*", "A1": "A*",
    "A": "A", "A2": "A",
    "B": "B", "B1": "B", "B2": "B",
    "C": "C", "C1": "C", "C2": "C",
    # Numeric flavours sometimes used by ds-deadlines etc.
    "1": "A*", "2": "A", "3": "B", "4": "C",
}


def normalize_tier(raw) -> str | None:
    """Map a heterogeneous rank value (string / list / dict) into our 4-bucket tier.

    Accepts:
      - str like "A*", "A", "B1"
      - list whose first non-empty element is the canonical rank (ds-deadlines)
      - dict with `core`, `ccf`, or `thcpl` keys (ccfddl)
    Returns None for unknown / empty / "N/A".
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        for key in ("core", "ccf", "thcpl"):
            t = normalize_tier(raw.get(key))
            if t is not None:
                return t
        return None
    if isinstance(raw, list):
        for item in raw:
            t = normalize_tier(item)
            if t is not None:
                return t
        return None
    s = str(raw).strip().upper()
    if not s or s in ("N/A", "NA", "NONE", "UNKNOWN"):
        return None
    return _TIER_MAP.get(s)


# ────────────────────────── acronym canonicalisation ──────────────────────────
# Different aggregators use different conventions for the same venue:
#   "NSDI"     vs "USENIX NSDI"
#   "ATC"      vs "USENIX ATC"
#   "ICSE"     vs "ACM/IEEE ICSE"
# We pick a canonical short form for each and rewrite at ingest so the
# `(acronym, year, round)` unique key actually does its job.
_ALIASES: dict[str, str] = {
    "usenix nsdi":   "NSDI",
    "usenix atc":    "ATC",
    "usenix osdi":   "OSDI",
    "usenix fast":   "FAST",
    "usenix sec":    "USENIX Security",
    "ieee icdcs":    "ICDCS",
    "ieee icde":     "ICDE",
    "ieee infocom":  "INFOCOM",
    "ieee icassp":   "ICASSP",
    "ieee s&p":      "S&P",
    "ieee sp":       "S&P",
    "acm/ieee icse": "ICSE",
    "acm icse":      "ICSE",
    "acm mobicom":   "MobiCom",
    "acm sigmod":    "SIGMOD",
    "acm sigcomm":   "SIGCOMM",
    "acm podc":      "PODC",
    "acm mm":        "MM",
    "acm css":       "CCS",
    "acm ccs":       "CCS",
    "acm-sigcomm":   "SIGCOMM",
    "icml":          "ICML",
    "ndss":          "NDSS",
}


def canonical_acronym(raw: str | None) -> str | None:
    """Map a heterogeneous source-supplied acronym to its canonical short form.
    Case-insensitive lookup; falls back to the input if no alias matches."""
    if not raw:
        return raw
    s = " ".join(str(raw).split())  # collapse whitespace
    return _ALIASES.get(s.lower(), s)


def min_year() -> int:
    """Earliest conference year worth keeping. Anything older is pruned on
    ingest and dropped from the canonical / source_records tables by the
    cleanup step in `refresh.py`. Current policy: keep last year + everything
    onward (so today is 2026 → keep 2025+; this still surfaces venues whose
    submission_deadline has passed but whose conference instance was the
    most recent prior edition)."""
    return datetime.utcnow().year - 1


def _tz_offset_hours(tz_str: str | None) -> int:
    if not tz_str:
        return -12
    s = tz_str.strip().upper()
    if s.startswith("UTC"):
        s = s[3:]
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        pass
    # IANA names — best-effort mapping. Without zoneinfo we approximate.
    return -12


def parse_ccfddl_timestamp(s: str | None, tz_str: str | None = None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    offset = _tz_offset_hours(tz_str)
    return (dt - timedelta(hours=offset)).replace(tzinfo=timezone.utc).replace(tzinfo=None)


def parse_iso_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    # Handle "24:00" end-of-day idiom by normalising to 23:59:59 same date.
    s = re.sub(r"(\d{4}-\d{2}-\d{2})\s+24:00(?::00)?", r"\1 23:59:59", s)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # Fall back to fuzzy parsing.
    try:
        from dateutil import parser as dparser
        return dparser.parse(s)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_date_range(date_str: str | None, year: int) -> tuple[datetime | None, datetime | None]:
    """Best-effort parse of free-form 'date' fields like 'July 11-19, 2025'."""
    if not date_str:
        return None, None
    from dateutil import parser as dparser
    s = date_str.strip()
    m = re.match(r"^([A-Za-z]+)\s+(\d+)\s*[-–]\s*(\d+)[,\s]+(\d{4})", s)
    if m:
        month, d1, d2, yr = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        try:
            return dparser.parse(f"{month} {d1}, {yr}"), dparser.parse(f"{month} {d2}, {yr}")
        except (ValueError, OverflowError):
            pass
    m2 = re.match(r"^([A-Za-z]+)\s+(\d+)\s*[-–]\s*([A-Za-z]+)\s+(\d+)[,\s]+(\d{4})", s)
    if m2:
        try:
            return (
                dparser.parse(f"{m2.group(1)} {m2.group(2)}, {m2.group(5)}"),
                dparser.parse(f"{m2.group(3)} {m2.group(4)}, {m2.group(5)}"),
            )
        except (ValueError, OverflowError):
            pass
    try:
        single = dparser.parse(s, default=datetime(year, 1, 1))
        return single, single
    except (ValueError, OverflowError):
        return None, None


def upsert_source_record(
    db, *, acronym: str, year: int, source: str,
    round: int = 1,
    name: str | None = None, link: str | None = None,
    abstract_deadline: datetime | None = None,
    submission_deadline: datetime | None = None,
    notification_date: datetime | None = None,
    conference_start: datetime | None = None,
    conference_end: datetime | None = None,
    location: str | None = None,
) -> SourceRecord | None:
    """Idempotent upsert keyed on (acronym, year, source). Flush after insert
    so subsequent queries in the same session see the new row — prevents
    UNIQUE-constraint violations when the input has duplicate entries.

    Returns None and writes nothing when `year` is below the active cutoff
    (`min_year()`) — keeps the source_records table tidy."""
    if year < min_year():
        return None
    acronym = canonical_acronym(acronym)
    row = (
        db.query(SourceRecord)
        .filter_by(acronym=acronym, year=year, source=source, round=round)
        .one_or_none()
    )
    if row is None:
        row = SourceRecord(
            acronym=acronym, year=year, source=source, round=round,
            fetched_at=datetime.utcnow(),
        )
        db.add(row)
        db.flush()
    row.name = name
    row.link = link
    row.abstract_deadline = abstract_deadline
    row.submission_deadline = submission_deadline
    row.notification_date = notification_date
    row.conference_start = conference_start
    row.conference_end = conference_end
    row.location = location
    row.fetched_at = datetime.utcnow()
    return row


def upsert_conference_secondary(
    db, *, acronym: str, year: int, name: str,
    round: int = 1,
    rounds_total: int | None = None,
    areas: list[str] | None = None,
    abstract_deadline: datetime | None = None,
    submission_deadline: datetime | None = None,
    notification_date: datetime | None = None,
    conference_start: datetime | None = None,
    conference_end: datetime | None = None,
    location: str | None = None,
    website: str | None = None,
    cfp_url: str | None = None,
    source_name: str,
    tier: str | None = None,
    h5_index: int | None = None,
    is_workshop: bool = False,
) -> bool:
    """Create the canonical conferences row only if one doesn't yet exist.

    Used by secondary aggregators (aideadlines, ds-deadlines, klb2, noise-lab,
    confsearch) so they can contribute *new* venues without overwriting data
    from higher-priority sources (ccfddl, seed, user). Returns True if a row
    was created.

    Flushes after add so subsequent queries in the same session see the new row
    — prevents UNIQUE-constraint violations when the input has duplicates.

    Skips silently if `year` is below `min_year()`.
    """
    if year < min_year():
        return False
    acronym = canonical_acronym(acronym)
    row = db.query(Conference).filter_by(acronym=acronym, year=year, round=round).one_or_none()
    if row is not None:
        # Existing row — don't overwrite the canonical source's data, but
        # backfill any field that's still null. Lets confsearch contribute
        # notification dates to a ccfddl-claimed row, aideadlines contribute
        # abstract deadlines, etc.
        new_tier = normalize_tier(tier)
        if row.tier is None and new_tier is not None:
            row.tier = new_tier
        if row.h5_index is None and h5_index is not None:
            row.h5_index = h5_index
        if row.abstract_deadline is None and abstract_deadline is not None:
            row.abstract_deadline = abstract_deadline
        if row.notification_date is None and notification_date is not None:
            row.notification_date = notification_date
        if row.conference_start is None and conference_start is not None:
            row.conference_start = conference_start
        if row.conference_end is None and conference_end is not None:
            row.conference_end = conference_end
        if row.location is None and location:
            row.location = location
        return False
    row = Conference(
        acronym=acronym, year=year, name=name,
        round=round, rounds_total=rounds_total,
        areas=json.dumps(areas or []),
        abstract_deadline=abstract_deadline,
        submission_deadline=submission_deadline,
        notification_date=notification_date,
        conference_start=conference_start,
        conference_end=conference_end,
        location=location,
        website=website,
        cfp_url=cfp_url,
        source=source_name,
        last_verified=datetime.utcnow(),
        is_workshop=is_workshop,
        tier=normalize_tier(tier),
        h5_index=h5_index,
    )
    db.add(row)
    db.flush()
    return True


# ────────────────────────── shared aideadlines/ds-deadlines parser ──────────────────────────
# Both repos use the same YAML schema:
#   - title, year, deadline, abstract_deadline, timezone, start, end, place, link, sub, hindex


def normalize_aideadlines_entry(entry: dict) -> dict | None:
    """Extract our internal fields from one aideadlines/ds-deadlines record."""
    title = (entry.get("title") or "").strip()
    year = entry.get("year")
    if not title or not year:
        return None
    tz = entry.get("timezone")
    deadline = parse_ccfddl_timestamp(entry.get("deadline"), tz)
    abstract = parse_ccfddl_timestamp(entry.get("abstract_deadline"), tz)
    notif = parse_iso_date(entry.get("notification_deadline") or entry.get("notification"))
    start = parse_iso_date(entry.get("start"))
    end = parse_iso_date(entry.get("end"))
    if start is None and end is None:
        start, end = parse_date_range(entry.get("date"), int(year))
    sub = entry.get("sub")
    if isinstance(sub, str):
        sub_list = [s.strip() for s in sub.split(",") if s.strip()]
    elif isinstance(sub, list):
        sub_list = sub
    else:
        sub_list = []
    return {
        "acronym": title,
        "year": int(year),
        "name": entry.get("full_name") or title,
        "link": entry.get("link"),
        "submission_deadline": deadline,
        "abstract_deadline": abstract,
        "notification_date": notif,
        "conference_start": start,
        "conference_end": end,
        "location": entry.get("place"),
        "sub_categories": sub_list,
        "hindex": entry.get("hindex"),
        "rank": entry.get("rank"),
    }


# ────────────────────────── category → areas mapping ──────────────────────────
# aideadlines/ds-deadlines use category codes; we map to our internal areas.
_SUB_TO_AREAS: dict[str, list[str]] = {
    # aideadlines codes
    "ML": ["ml"],
    "CV": ["ml"],
    "NLP": ["ml"],
    "RO": ["control", "robotics"],
    "SP": ["ml"],
    "DM": ["ml"],
    "AP": ["ml"],
    "KR": ["ml"],
    "HCI": [],
    "IR": ["ml"],
    "SM": ["ml"],
    "MISC": [],
    # ds-deadlines codes
    "BC": ["systems"],
    "CS": ["systems"],
    "DB": ["systems"],
    "DS": ["systems"],
    "ES": ["systems"],
    "NET": ["networking"],
    "PER": ["systems"],
    "SE": ["systems"],
}


def map_subs_to_areas(subs: Iterable[str]) -> list[str]:
    out: set[str] = set()
    for s in subs or []:
        out.update(_SUB_TO_AREAS.get(s.strip().upper(), []))
    return sorted(out)
