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


def _parse_ts(s):
    return _common.parse_iso_date(s, normalize=False)


def _to_utc(dt, tz_str):
    if dt is None:
        return None
    try:
        return _common.to_utc(dt, tz_str)
    except ValueError:
        return None


_parse_conf_date_range = _common.parse_date_range


def discover_venues():
    """Discover the repository tree; retain curated metadata and offline fallback."""
    venues = dict(VENUE_MAP)
    response = _common.http_get(
        "https://api.github.com/repos/ccfddl/ccf-deadlines/git/trees/main?recursive=1")
    if response is None:
        return venues
    areas = {"NW": ["networking"], "AI": ["ml"], "CG": ["multimedia"],
             "DS": ["systems"], "SE": ["systems"], "SC": ["systems"],
             "MX": ["systems", "ml"]}
    try:
        for entry in response.json().get("tree", []):
            parts = entry.get("path", "").split("/")
            if len(parts) == 3 and parts[0] == "conference" and parts[1] in areas and parts[2].endswith((".yml", ".yaml")):
                venues.setdefault((parts[1], parts[2]), {"areas": areas[parts[1]]})
    except (ValueError, TypeError, AttributeError):
        pass
    return venues


def fetch_ccfddl_venue(category: str, filename: str) -> dict | None:
    url = f"{CCFDDL_RAW}/{category}/{filename}"
    r = _common.http_get(url, timeout=20.0)
    if r is None:
        return None
    docs = _common.safe_yaml_load_text(r.text, None)
    if not docs:
        return None
    return docs[0] if isinstance(docs, list) else docs


def ingest_all() -> dict[str, int]:
    """Pull all configured ccfddl venues; upsert the latest two years for each."""
    now = _common.utc_now()
    upserted = 0
    errors = 0
    with SessionLocal() as db:
        for (cat, fname), meta in discover_venues().items():
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
                    _common.promote_prediction(row)
                    row.name = name
                    row.rounds_total = rounds_total
                    row.areas = json.dumps(meta.get("areas", []))
                    row.tier = meta.get("tier") or _common.normalize_tier(data.get("rank")) or row.tier
                    row.date_metadata = None
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
