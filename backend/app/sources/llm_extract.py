"""LLM-driven extraction of dates from CFP pages, with two-pass verification.

We fetch the CFP URL, strip to plain text, then call Claude twice with slightly
different prompts. If both extractions agree, we update the DB and clear the
`diverged` flag. If they disagree (or one fails), we record the disagreement,
mark `diverged=True`, and leave the existing dates untouched so the user can
review on the dashboard.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

import httpx

from ..db import SessionLocal
from ..models import Conference

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

EXTRACT_FIELDS = [
    "abstract_deadline",
    "submission_deadline",
    "notification_date",
    "camera_ready",
    "conference_start",
    "conference_end",
    "page_limit",
    "location",
]

PROMPT_A = """Extract the following fields from this conference Call-for-Papers page.

Return ONLY a JSON object with these exact keys. Use ISO 8601 timestamps
(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS+ZZ:ZZ). Use null if a field is not stated.
Do not invent dates.

Fields:
  abstract_deadline
  submission_deadline (full paper deadline)
  notification_date (acceptance/rejection notification)
  camera_ready
  conference_start (first day of the conference)
  conference_end (last day)
  page_limit (integer, main paper only, excluding references)
  location (city, country)

CFP page content:
---
{page}
---

Return JSON only.
"""

PROMPT_B = """You are reading a conference Call-for-Papers. Identify these key dates
and details. Return strictly JSON. Use null for missing fields. Use ISO 8601 dates.
Do not guess.

Keys to return: abstract_deadline, submission_deadline, notification_date,
camera_ready, conference_start, conference_end, page_limit (int), location.

Page:
{page}
"""


def _strip_html(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:18000]


def _fetch_page(url: str) -> str | None:
    try:
        r = httpx.get(url, timeout=20.0, follow_redirects=True,
                      headers={"User-Agent": "conference-finder/0.1"})
        r.raise_for_status()
        return _strip_html(r.text)
    except httpx.HTTPError:
        return None


def _claude_extract(page_text: str, prompt_template: str) -> dict[str, Any] | None:
    if not ANTHROPIC_KEY:
        return None
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt_template.format(page=page_text)}],
    )
    body = msg.content[0].text if msg.content else ""
    m = re.search(r"\{.*\}", body, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _norm(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    if not s:
        return None
    return s


def _agree(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    sa, sb = str(a).strip(), str(b).strip()
    # Day-level agreement for dates: compare leading 10 chars (YYYY-MM-DD) if both look like dates.
    if re.match(r"^\d{4}-\d{2}-\d{2}", sa) and re.match(r"^\d{4}-\d{2}-\d{2}", sb):
        return sa[:10] == sb[:10]
    return sa == sb


def _parse_dt(s):
    if s is None:
        return None
    from dateutil import parser as dparser
    try:
        return dparser.parse(str(s))
    except (ValueError, TypeError, OverflowError):
        return None


FULL_PROMPT_A = """Extract conference/workshop info from this Call-for-Papers page.

Return ONLY a JSON object with exactly these keys. Use null for missing fields.
Use ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS) for dates. Do not invent.

Keys:
  acronym         (short venue acronym, e.g. "SIGCOMM", "PACMI", "L4DC")
  name            (full venue name)
  year            (integer — the conference's calendar year)
  is_workshop     (true if a workshop, false if a main conference)
  parent_venue    (parent acronym if workshop, else null)
  areas           (array, subset of: control, networking, ml, systems, multimedia, robotics)
  abstract_deadline
  submission_deadline
  notification_date
  camera_ready
  conference_start
  conference_end
  page_limit      (integer, main paper, excluding references)
  location        (city, country)

Area hints from user (use these if applicable, but do not include any others
that don't fit): {hints}

CFP page:
---
{page}
---

Return JSON only.
"""

FULL_PROMPT_B = """Read this conference Call-for-Papers and identify the venue.
Return strictly JSON. Null for missing fields. ISO 8601 dates. Do not guess.

Keys: acronym, name, year (int), is_workshop (bool), parent_venue,
areas (array from: control, networking, ml, systems, multimedia, robotics),
abstract_deadline, submission_deadline, notification_date, camera_ready,
conference_start, conference_end, page_limit (int), location.

User area hints: {hints}

Page:
{page}
"""

_FULL_FIELDS = [
    "acronym", "name", "year", "is_workshop", "parent_venue", "areas",
    "abstract_deadline", "submission_deadline", "notification_date",
    "camera_ready", "conference_start", "conference_end",
    "page_limit", "location",
]


def _agree_full(a, b, field: str) -> bool:
    """Field-specific equality. Areas compared as sets; everything else via _agree."""
    if field == "areas":
        sa = set(a) if isinstance(a, list) else set()
        sb = set(b) if isinstance(b, list) else set()
        return sa == sb and len(sa) > 0
    return _agree(a, b)


def extract_full_venue(url: str, area_hints: list[str] | None = None) -> dict | None:
    """Two-pass extraction of a full venue record. Returns dict with `_diverged` (list of fields)
    plus extracted values. None if the page can't be fetched or the LLM is unavailable.
    """
    if not ANTHROPIC_KEY:
        return None
    page = _fetch_page(url)
    if not page:
        return None
    hints_str = ", ".join(area_hints or []) or "(none)"
    a = _claude_extract(page, FULL_PROMPT_A.replace("{hints}", hints_str)) or {}
    b = _claude_extract(page, FULL_PROMPT_B.replace("{hints}", hints_str)) or {}
    agreed: dict = {}
    diverged: list[str] = []
    for f in _FULL_FIELDS:
        va, vb = a.get(f), b.get(f)
        if _agree_full(va, vb, f) and va is not None:
            agreed[f] = va
        elif va is not None or vb is not None:
            diverged.append(f)
    agreed["_diverged"] = diverged
    return agreed


def enrich_seed_venues() -> dict[str, int]:
    """For seed/llm_extract rows with a cfp_url, run two-pass extraction."""
    if not ANTHROPIC_KEY:
        return {"skipped": -1, "reason": "ANTHROPIC_API_KEY not set"}

    updated = 0
    diverged = 0
    fetched = 0
    with SessionLocal() as db:
        rows = (
            db.query(Conference)
            .filter(Conference.source.in_(["seed", "llm_extract"]))
            .filter(Conference.cfp_url.isnot(None))
            .all()
        )
        for row in rows:
            page = _fetch_page(row.cfp_url)
            if not page:
                continue
            fetched += 1
            a = _claude_extract(page, PROMPT_A) or {}
            b = _claude_extract(page, PROMPT_B) or {}
            agreed = {}
            for f in EXTRACT_FIELDS:
                va, vb = _norm(a.get(f)), _norm(b.get(f))
                if _agree(va, vb) and va is not None:
                    agreed[f] = va
            if not agreed:
                row.diverged = True
                diverged += 1
                continue

            # Apply agreed fields.
            for f, v in agreed.items():
                if f == "page_limit":
                    try:
                        row.page_limit = int(v)
                    except (ValueError, TypeError):
                        pass
                elif f == "location":
                    row.location = str(v)
                else:
                    parsed = _parse_dt(v)
                    if parsed is not None:
                        setattr(row, f, parsed.replace(tzinfo=None))

            row.source = "llm_extract"
            row.last_verified = datetime.utcnow()
            row.diverged = False
            updated += 1
        db.commit()
    return {"updated": updated, "diverged": diverged, "fetched": fetched}
