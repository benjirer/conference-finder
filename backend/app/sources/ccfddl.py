"""Ingester for the ccfddl (ccf-deadlines) community-maintained YAML repo.

We map a curated subset of ccfddl venues to our internal `areas` taxonomy
(control / networking / ml / systems / multimedia). Each ccfddl file lists all
historical years; we ingest the most recent two future-or-recent years per venue.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import yaml

from ..db import SessionLocal
from ..models import Conference
from . import _common

CCFDDL_RAW = "https://raw.githubusercontent.com/ccfddl/ccf-deadlines/main/conference"

# (category, filename) -> areas + workshop flag.
# Categories follow ccfddl directory names: NW, AI, CG, MX, SC, DS, SE.
VENUE_MAP: dict[tuple[str, str], dict[str, Any]] = {
    # Networking
    ("NW", "sigcomm.yml"):   {"areas": ["networking"], "tier": "A*"},
    ("NW", "nsdi.yml"):      {"areas": ["networking", "systems"], "tier": "A*"},
    ("NW", "imc.yml"):       {"areas": ["networking"], "tier": "A"},
    ("NW", "conext.yml"):    {"areas": ["networking"], "tier": "A"},
    ("NW", "mobicom.yml"):   {"areas": ["networking"], "tier": "A*"},
    ("NW", "mobisys.yml"):   {"areas": ["networking", "systems"], "tier": "A"},
    ("NW", "sensys.yml"):    {"areas": ["networking", "systems"], "tier": "A"},
    ("NW", "ipsn.yml"):      {"areas": ["networking"], "tier": "A"},
    ("NW", "infocom.yml"):   {"areas": ["networking"], "tier": "A"},
    ("NW", "mmsys.yml"):     {"areas": ["networking", "multimedia"], "tier": "B"},
    ("NW", "apnet.yml"):     {"areas": ["networking"], "tier": "B"},
    ("NW", "icnp.yml"):      {"areas": ["networking"], "tier": "B"},
    ("NW", "iwqos.yml"):     {"areas": ["networking"], "tier": "B"},
    ("NW", "nossdav.yml"):   {"areas": ["networking", "multimedia"], "tier": "C"},
    # AI / ML
    ("AI", "nips.yml"):      {"areas": ["ml"], "tier": "A*"},
    ("AI", "icml.yml"):      {"areas": ["ml"], "tier": "A*"},
    ("AI", "iclr.yml"):      {"areas": ["ml"], "tier": "A*"},
    ("AI", "aistats.yml"):   {"areas": ["ml"], "tier": "A"},
    ("AI", "uai.yml"):       {"areas": ["ml"], "tier": "A"},
    ("AI", "aaai.yml"):      {"areas": ["ml"], "tier": "A*"},
    ("AI", "ijcai.yml"):     {"areas": ["ml"], "tier": "A*"},
    ("AI", "colt.yml"):      {"areas": ["ml"], "tier": "A"},
    # Multimedia
    ("CG", "mm.yml"):        {"areas": ["multimedia", "ml"], "tier": "A*"},
    ("CG", "mmasia.yml"):    {"areas": ["multimedia"], "tier": "B"},
    ("CG", "icme.yml"):      {"areas": ["multimedia"], "tier": "B"},
    # Systems-adjacent (DS = distributed systems / SE = software eng)
    # Note: SOSP/OSDI are NOT in ccfddl — they're added via seed_venues.yaml.
    ("DS", "eurosys.yml"):   {"areas": ["systems"], "tier": "A"},
    ("DS", "atc.yml"):       {"areas": ["systems"], "tier": "A"},
    ("DS", "hpdc.yml"):      {"areas": ["systems"], "tier": "A"},
    ("DS", "ppopp.yml"):     {"areas": ["systems"], "tier": "A"},
    # MX = interdisciplinary / mixed (includes MLSys)
    ("MX", "mlsys.yml"):     {"areas": ["ml", "systems"], "tier": "A"},
    # Robotics (control-adjacent)
    ("AI", "icra.yml"):      {"areas": ["control", "robotics"], "tier": "A"},
    ("AI", "iros.yml"):      {"areas": ["control", "robotics"], "tier": "A"},
    ("AI", "corl.yml"):      {"areas": ["control", "robotics", "ml"], "tier": "A"},
    ("AI", "rss.yml"):       {"areas": ["control", "robotics"], "tier": "A*"},
}


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    # ccfddl uses "YYYY-MM-DD HH:MM:SS" without TZ; we treat as UTC-12 (AoE-like)
    # if the venue's timezone is unset. The per-venue `timezone` field is applied
    # by the caller. Returning naive datetime here; caller offsets to UTC.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _tz_offset_hours(tz_str: str | None) -> int:
    if not tz_str:
        return -12  # AoE-like default
    tz_str = tz_str.strip().upper().replace("UTC", "")
    if not tz_str:
        return 0
    try:
        return int(tz_str)
    except ValueError:
        return -12


def _to_utc(local_dt: datetime | None, tz_str: str | None) -> datetime | None:
    if local_dt is None:
        return None
    offset = _tz_offset_hours(tz_str)
    return (local_dt - timedelta(hours=offset)).replace(tzinfo=timezone.utc).replace(tzinfo=None)


def _parse_conf_date_range(date_str: str | None, year: int) -> tuple[datetime | None, datetime | None]:
    """Best-effort parse of ccfddl's free-form `date` field, e.g. 'July 11-19, 2025'."""
    if not date_str:
        return None, None
    from dateutil import parser as dparser
    import re
    m = re.match(r"^([A-Za-z]+)\s+(\d+)\s*[-–]\s*(\d+)[,\s]+(\d{4})", date_str.strip())
    if m:
        month, d1, d2, yr = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        try:
            start = dparser.parse(f"{month} {d1}, {yr}")
            end = dparser.parse(f"{month} {d2}, {yr}")
            return start, end
        except (ValueError, OverflowError):
            pass
    # Cross-month range, e.g. "July 28 - August 2, 2025"
    m2 = re.match(r"^([A-Za-z]+)\s+(\d+)\s*[-–]\s*([A-Za-z]+)\s+(\d+)[,\s]+(\d{4})", date_str.strip())
    if m2:
        try:
            start = dparser.parse(f"{m2.group(1)} {m2.group(2)}, {m2.group(5)}")
            end = dparser.parse(f"{m2.group(3)} {m2.group(4)}, {m2.group(5)}")
            return start, end
        except (ValueError, OverflowError):
            pass
    try:
        single = dparser.parse(date_str, default=datetime(year, 1, 1))
        return single, single
    except (ValueError, OverflowError):
        return None, None


def fetch_ccfddl_venue(category: str, filename: str) -> dict | None:
    url = f"{CCFDDL_RAW}/{category}/{filename}"
    try:
        r = httpx.get(url, timeout=20.0)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    docs = yaml.safe_load(r.text)
    if not docs:
        return None
    return docs[0] if isinstance(docs, list) else docs


def ingest_all() -> dict[str, int]:
    """Pull all configured ccfddl venues; upsert the latest two years for each."""
    now = datetime.utcnow()
    upserted = 0
    errors = 0
    with SessionLocal() as db:
        for (cat, fname), meta in VENUE_MAP.items():
            data = fetch_ccfddl_venue(cat, fname)
            if not data:
                errors += 1
                continue
            acronym = _common.canonical_acronym(data.get("title", "").strip())
            name = data.get("description", "").strip() or acronym
            confs = data.get("confs", []) or []
            # Keep entries whose conference_end is within the future, or the
            # last two entries — whichever yields more.
            confs_sorted = sorted(confs, key=lambda c: c.get("year", 0))
            relevant = confs_sorted[-2:] if len(confs_sorted) >= 2 else confs_sorted
            for cf in relevant:
                year = cf.get("year")
                if not year:
                    continue
                if year < _common.min_year():
                    continue
                tz = cf.get("timezone")
                # ccfddl's `timeline` is a list — venues with multiple review
                # cycles (CoNEXT, SIGMETRICS, …) list one entry per round.
                timeline_list = cf.get("timeline") or [{}]
                rounds_total = len(timeline_list) if len(timeline_list) > 1 else None
                start, end = _parse_conf_date_range(cf.get("date"), year)
                for idx, tl in enumerate(timeline_list, start=1):
                    abstract = _to_utc(_parse_ts(tl.get("abstract_deadline")), tz)
                    deadline = _to_utc(_parse_ts(tl.get("deadline")), tz)
                    row = (
                        db.query(Conference)
                        .filter_by(acronym=acronym, year=year, round=idx)
                        .one_or_none()
                    )
                    if row is None:
                        row = Conference(acronym=acronym, year=year, round=idx, name=name)
                        db.add(row)
                        db.flush()
                    row.name = name
                    row.rounds_total = rounds_total
                    row.areas = json.dumps(meta.get("areas", []))
                    row.tier = meta.get("tier") or _common.normalize_tier(data.get("rank")) or row.tier
                    row.abstract_deadline = abstract
                    row.submission_deadline = deadline
                    row.conference_start = start
                    row.conference_end = end
                    row.timezone = tz
                    row.location = cf.get("place")
                    row.website = cf.get("link")
                    row.cfp_url = cf.get("link")
                    row.source = "ccfddl"
                    row.last_verified = now
                    row.is_workshop = False
                    upserted += 1

                    _common.upsert_source_record(
                        db,
                        acronym=acronym, year=year, source="ccfddl", round=idx,
                        name=name, link=cf.get("link"),
                        abstract_deadline=abstract,
                        submission_deadline=deadline,
                        conference_start=start, conference_end=end,
                        location=cf.get("place"),
                    )
        db.commit()
    return {"upserted": upserted, "errors": errors}
