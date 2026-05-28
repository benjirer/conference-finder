"""Load cached_pc.yaml into the `pc_members` table.

Written by `python -m app.enrich_pc`, applied at refresh time. The pc_url is
also written back onto the canonical Conference row (round=1) so it's visible
in the API + UI.

Idempotent: clears any existing pc_members for a (conference_id) before inserting
the fresh set, so re-running with newer extractions just replaces.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ..db import SessionLocal
from ..models import Conference, PCMember
from . import _common

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "cached_pc.yaml"


def apply_cached_pc() -> dict[str, int]:
    if not CACHE_FILE.exists():
        return {"applied": 0, "reason": "no cached_pc.yaml"}
    raw = _common.safe_yaml_load(CACHE_FILE, {})
    entries = raw.get("entries", []) if isinstance(raw, dict) else []
    venues_filled = 0
    members_total = 0
    skipped_venue_missing = 0

    with SessionLocal() as db:
        for entry in entries:
            acronym = _common.canonical_acronym(entry.get("acronym"))
            year = entry.get("year")
            if not acronym or not year:
                continue
            conf = (
                db.query(Conference)
                .filter_by(acronym=acronym, year=year, round=1)
                .one_or_none()
            )
            if conf is None:
                skipped_venue_missing += 1
                continue
            if entry.get("pc_url") and not conf.pc_url:
                conf.pc_url = entry["pc_url"]
            # Wipe old members for this conference, then re-insert.
            db.query(PCMember).filter_by(conference_id=conf.id).delete()
            db.flush()
            seen_norms: set[str] = set()
            for m in entry.get("members") or []:
                name = m.get("name")
                if not name:
                    continue
                norm = _common.normalize_person_name(name)
                if not norm or norm in seen_norms:
                    continue
                seen_norms.add(norm)
                db.add(PCMember(
                    conference_id=conf.id,
                    name=name,
                    normalized_name=norm,
                    affiliation=m.get("affiliation"),
                    role=m.get("role") or "member",
                ))
                members_total += 1
            venues_filled += 1
        db.commit()
    return {
        "venues_filled": venues_filled,
        "members_total": members_total,
        "skipped_venue_missing": skipped_venue_missing,
    }
