"""Scrape https://noise-lab.net/networking-deadlines/.

The page has no underlying public data repo, but the HTML is regular: one
`<div class="conf  ... CONF ">` per venue × year × deadline, each containing:
    <h2><a href="URL">ACRONYM YEAR</a></h2>
    <div class="meta">FULL_NAME<br>... // <a ...>LOCATION</a></div>
    <span class="deadline-time">YYYY-MM-DD HH:MM:SS</span>
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx

from ..db import SessionLocal
from . import _common

URL = "https://noise-lab.net/networking-deadlines/"
SOURCE_NAME = "noise-lab"

# Anchor on each conf div via its `id="venue2026-N"` attribute; the next
# deadline-time span belongs to that venue/round. This avoids any reliance on
# nested-div balancing in regex.
ANCHOR_RX = re.compile(
    r'<div id="([a-z0-9]+)(\d{4})-(\d+)"[^>]*class="conf[^"]*\bCONF\b[^"]*"[^>]*>',
    re.IGNORECASE,
)
H2_RX = re.compile(
    r'<h2><a href="([^"]+)">([A-Za-z][A-Za-z0-9+/\- ]+?)\s+\d{4}</a></h2>',
    re.IGNORECASE,
)
META_RX = re.compile(r'<div class="meta">(.*?)</div>', re.DOTALL)
DEADLINE_RX = re.compile(
    r'<span class="deadline-time">\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\s*</span>',
)
LOCATION_RX = re.compile(r'q=([^"]+)"')


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def ingest_all() -> dict[str, int]:
    try:
        r = httpx.get(URL, timeout=30.0, follow_redirects=True,
                      headers={"User-Agent": "conference-finder/0.1"})
        r.raise_for_status()
    except httpx.HTTPError as e:
        return {"error": str(e), "added": 0, "recorded": 0}
    html = r.text

    # Track multiple deadlines per (acronym, year) — we take the earliest, since
    # noise-lab lists each round of a rolling-deadline venue as its own block.
    seen: dict[tuple[str, int], dict] = {}
    anchors = list(ANCHOR_RX.finditer(html))
    for i, anchor in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else anchor.start() + 4000
        block = html[anchor.start():end]
        year = int(anchor.group(2))
        h2 = H2_RX.search(block)
        if not h2:
            continue
        link, acronym = h2.group(1), h2.group(2).strip()

        meta_match = META_RX.search(block)
        full_name = None
        location = None
        if meta_match:
            meta_text = meta_match.group(1)
            # The first sentence is the full name; the location is hidden in a
            # google-search href.
            loc_m = LOCATION_RX.search(meta_text)
            if loc_m:
                location = loc_m.group(1).replace("+", " ")
            cleaned = _strip_tags(meta_text)
            # Strip out the location text from the name.
            if location:
                cleaned = cleaned.split("//")[0].strip()
            full_name = cleaned or acronym

        dl_match = DEADLINE_RX.search(block)
        if not dl_match:
            continue
        deadline = _common.parse_iso_date(dl_match.group(1))
        if deadline is None:
            continue

        key = (acronym, year)
        existing = seen.get(key)
        if existing is None or deadline < existing["submission_deadline"]:
            seen[key] = {
                "acronym": acronym, "year": year, "name": full_name or acronym,
                "link": link, "location": location,
                "submission_deadline": deadline,
            }

    added = 0
    recorded = 0
    with SessionLocal() as db:
        for v in seen.values():
            _common.upsert_source_record(
                db,
                acronym=v["acronym"], year=v["year"], source=SOURCE_NAME,
                name=v["name"], link=v["link"],
                submission_deadline=v["submission_deadline"],
                location=v["location"],
            )
            recorded += 1
            if _common.upsert_conference_secondary(
                db,
                acronym=v["acronym"], year=v["year"], name=v["name"],
                areas=["networking"],
                submission_deadline=v["submission_deadline"],
                location=v["location"],
                website=v["link"], cfp_url=v["link"],
                source_name=SOURCE_NAME,
            ):
                added += 1
        db.commit()
    return {"added": added, "recorded": recorded}
