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

import logging
import time
from datetime import timezone as _tz
from pathlib import Path

import httpx
import yaml
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Conference, SourceRecord


def utc_now():
    """Naive UTC datetime — replaces deprecated `datetime.utcnow()`.

    The whole codebase stores naive UTC datetimes (no tzinfo) in SQLite.
    Centralising the call avoids a deprecation warning on each use.
    """
    from datetime import datetime
    return datetime.now(_tz.utc).replace(tzinfo=None)

log = logging.getLogger("conference_finder")

# ────────────────────────── safe network + filesystem helpers ──────────────────────────

DEFAULT_UA = "Mozilla/5.0 (compatible; conference-finder/0.3)"


def http_get(url: str, *, timeout: float = 30.0, retries: int = 2,
             backoff: float = 0.8, headers: dict | None = None) -> httpx.Response | None:
    """GET with retries on transient errors (5xx + connection failures).

    Returns the Response on the first 2xx response, or None if all attempts
    fail. Designed so every aggregator can use one consistent retry policy
    rather than each rolling its own.
    """
    merged_headers = {"User-Agent": DEFAULT_UA}
    if headers:
        merged_headers.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True, headers=merged_headers)
        except httpx.HTTPError as e:
            last_err = e
        else:
            # Retry on 5xx; treat 4xx as permanent.
            if r.status_code < 500:
                if r.is_success:
                    return r
                # 4xx — give up immediately, don't waste retries.
                log.debug("http_get %s -> %s (no retry on 4xx)", url, r.status_code)
                return None
            last_err = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))
    log.debug("http_get %s gave up after %d retries: %s", url, retries, last_err)
    return None


def safe_yaml_load(path: Path, default):
    """Load a YAML file, returning `default` on any parse error or missing file.

    Never raises. Used by every cache/overlay loader so a single corrupted YAML
    can't take down the refresh pipeline.
    """
    if not path.exists():
        return default
    try:
        raw = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError) as e:
        log.warning("safe_yaml_load(%s): %s — falling back to default", path, e)
        return default
    return raw if raw is not None else default


def safe_yaml_load_text(text: str, default):
    """Parse a YAML string; return default on error. For inline text already fetched over HTTP."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        log.warning("safe_yaml_load_text failed: %s", e)
        return default
    return raw if raw is not None else default


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
    "ieee/acm cgo": "CGO",
    "acm/ieee cgo": "CGO",
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


def normalize_person_name(name: str | None) -> str:
    """Collapse a person name to a form usable for cross-venue intersection.

    Lower-cases, strips accents, drops middle initials and dots, collapses spaces.
    Doesn't try to handle "Last, First" vs "First Last" — assume the source uses
    a consistent ordering. False positives ("J. Wang" matching two different
    Wangs) are filtered downstream by also showing affiliation.
    """
    if not name:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))  # strip accents
    s = s.lower()
    s = re.sub(r"[.\-_,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = s.split()
    # Drop single-letter "middle initials" only when ≥2 multi-letter tokens remain.
    # Otherwise "J. Smith" would collapse to just "smith" and false-match any Smith.
    long_tokens = [t for t in tokens if len(t) > 1]
    if len(long_tokens) >= 2:
        return " ".join(long_tokens)
    return " ".join(tokens)


def min_year() -> int:
    """Earliest conference year worth keeping. Anything older is pruned on
    ingest and dropped from the canonical / source_records tables by the
    cleanup step in `refresh.py`. Current policy: keep last year + everything
    onward (so today is 2026 → keep 2025+; this still surfaces venues whose
    submission_deadline has passed but whose conference instance was the
    most recent prior edition)."""
    return utc_now().year - 1


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


def to_utc(dt: datetime, tz_str: str | None = None) -> datetime:
    """Normalize offsets and IANA timezones before SQLite drops tzinfo."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if dt.tzinfo is None:
        name = (tz_str or "UTC-12").strip()
        if name.upper() in {"AOE", "ANYWHERE ON EARTH"}:
            name = "UTC-12"
        match = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{1,2})(?::(\d{2}))?", name, re.I)
        if match:
            minutes = int(match[2]) * 60 + int(match[3] or 0)
            zone = timezone(timedelta(minutes=minutes * (1 if match[1] == "+" else -1)))
        else:
            try:
                zone = ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"Unknown timezone: {name}")
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_ccfddl_timestamp(s, tz_str: str | None = None) -> datetime | None:
    dt = parse_iso_date(s, normalize=False)
    if dt is None:
        return None
    try:
        return to_utc(dt, tz_str)
    except ValueError:
        return None


def parse_iso_date(s, *, normalize=True) -> datetime | None:
    if s is None or s == "":
        return None
    value = str(s).strip()
    # Never let dateutil silently invent the current year/month/day.
    if not re.search(r"\b\d{4}\b", value):
        return None
    value = re.sub(r"(\d{4}-\d{2}-\d{2})[ T]24:00(?::00)?", r"\1 23:59:59", value)
    try:
        from dateutil import parser as dparser
        # Different defaults expose incomplete dates without rejecting prose dates.
        dt = dparser.parse(value, default=datetime(2000, 1, 1))
        other = dparser.parse(value, default=datetime(2000, 2, 2))
        if dt != other:
            return None
        return to_utc(dt, "UTC") if normalize else dt
    except (ValueError, TypeError, OverflowError):
        return None


def promote_prediction(row):
    """Remove all estimated dates before accepting a real edition."""
    if row.predicted:
        for field in ("abstract_deadline", "submission_deadline", "notification_date",
                      "camera_ready", "conference_start", "conference_end"):
            setattr(row, field, None)
        row.predicted = False
        row.notes = None


def parse_date_range(date_str: str | None, year: int) -> tuple[datetime | None, datetime | None]:
    """Best-effort parse of free-form 'date' fields like 'July 11-19, 2025'."""
    if not date_str:
        return None, None
    from dateutil import parser as dparser
    s = str(date_str).strip()
    # Day-first ranges, common on European conference websites.
    match = re.fullmatch(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)[,\s]+(\d{4})", s)
    if match:
        try:
            return (dparser.parse(f"{match[1]} {match[3]} {match[4]}"),
                    dparser.parse(f"{match[2]} {match[3]} {match[4]}"))
        except ValueError:
            return None, None
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:to|[-–])\s*(\d{4}-\d{2}-\d{2})", s)
    if match:
        return parse_iso_date(match[1]), parse_iso_date(match[2])
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
        priority = {"confsearch": 0, "noise-lab": 1, "klb2": 2,
                    "ds-deadlines": 3, "aideadlines": 4}
        if row.predicted or priority.get(source_name, -1) >= priority.get(row.source, 99):
            promote_prediction(row)
            for field, value in {
                "submission_deadline": submission_deadline,
                "abstract_deadline": abstract_deadline,
                "notification_date": notification_date,
                "conference_start": conference_start, "conference_end": conference_end,
                "website": website, "cfp_url": cfp_url, "location": location,
            }.items():
                if value is not None:
                    setattr(row, field, value)
            row.source = source_name
            row.last_verified = utc_now()
        # Existing row — don't overwrite the canonical source's data, but
        # backfill any field that's still null. Lets confsearch contribute
        # notification dates to a ccfddl-claimed row, aideadlines contribute
        # abstract deadlines, etc.
        new_tier = normalize_tier(tier)
        if row.tier is None and new_tier is not None:
            row.tier = new_tier
        if row.h5_index is None and h5_index is not None:
            row.h5_index = h5_index
        if row.submission_deadline is None and submission_deadline is not None:
            row.submission_deadline = submission_deadline
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
