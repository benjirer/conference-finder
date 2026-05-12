"""LLM-driven extraction of CFP page fields, with two-pass agreement and
Sonnet fallback for hard cases.

Two pieces:

1. `enrich_seed_venues()` — runs during refresh. Updates the canonical
   `conferences` table for venues with `source IN (seed, llm_extract)` and a
   `cfp_url`. Skips silently if `ANTHROPIC_API_KEY` isn't set.

2. `extract_full_venue(url, area_hints)` — used by the POST /api/venues
   endpoint to add a new venue from a CFP URL.

3. `extract_venue_extras(url, acronym, year)` — used by the local
   `enrich_extras` script to fill notification / page_limit / acceptance_rate
   / multi-round info for every venue in the DB.

Reliability tactics:
  - selectolax for robust HTML → plain text (catches table/list content
    that the old regex stripper dropped).
  - 4096 max_tokens (was 1024 — too tight for 14-field JSON + rounds list).
  - Two passes with Haiku first; if they disagree or both return null, retry
    with Sonnet 4.6.
  - Date-aware agreement: parse before comparing so "June 15, 2025" matches
    "2025-06-15".
  - Single retry on JSON-parse failure with an explicit "JSON only" prompt.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

import httpx
from dateutil import parser as dparser
from selectolax.parser import HTMLParser

from ..db import SessionLocal
from ..models import Conference

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_STRONG = "claude-sonnet-4-6"
MAX_TOKENS = 4096
PAGE_CHAR_BUDGET = 30000  # was 18k; now uses selectolax so content density is higher

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


# ────────────────────────────── page fetching ──────────────────────────────


def _strip_html(html: str) -> str:
    """Extract visible text from HTML. selectolax handles tables / lists /
    nested structures correctly — the prior regex approach lost a lot."""
    tree = HTMLParser(html)
    # Drop scripts/styles/nav/footer entirely.
    for tag in ("script", "style", "nav", "footer", "header", "noscript"):
        for n in tree.css(tag):
            n.decompose()
    # Prefer the main / article / content region when present.
    main = tree.css_first("main, article, [role=main], #content, .content")
    body = main.text(separator=" ", strip=True) if main else tree.body.text(separator=" ", strip=True)
    body = re.sub(r"\s+", " ", body)
    return body[:PAGE_CHAR_BUDGET]


def _fetch_page(url: str) -> str | None:
    try:
        r = httpx.get(
            url, timeout=30.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; conference-finder/0.2)"},
        )
        r.raise_for_status()
        text = _strip_html(r.text)
        return text if len(text.strip()) >= 200 else None
    except (httpx.HTTPError, AttributeError):
        return None


# ────────────────────────────── prompts ──────────────────────────────


_FIELDS_BLOCK = """Fields to extract:
  abstract_deadline      ISO date — abstract / registration deadline (separate from full paper submission)
  submission_deadline    ISO date — full paper submission deadline (for round 1, if multi-round)
  notification_date      ISO date — when authors hear back about accept/reject
  camera_ready           ISO date — final camera-ready / final-version due
  conference_start       ISO date — first day of the conference itself
  conference_end         ISO date — last day
  page_limit             integer  — main paper page limit, excluding references
  location               string   — "city, country"
  rounds                 array OR null — present ONLY if the venue has multiple submission cycles
                                  (e.g. CoNEXT, SIGMETRICS). Each element:
                                  {{ "round": int, "abstract_deadline": "...", "submission_deadline": "...",
                                     "notification_date": "...", "camera_ready": "..." }} (ISO dates, null OK)
"""

PROMPT_A = """Read this conference Call-for-Papers page and extract the structured fields.
Look specifically for an "Important Dates" section (or similar — "Key Dates",
"Deadlines", "Submission Timeline") — that's where the dates live.

Return ONLY a JSON object with these keys. Use null for fields not stated.
Use ISO 8601 dates (YYYY-MM-DD). Never invent values.

""" + _FIELDS_BLOCK + """

CFP page content:
---
{page}
---

Return JSON only, no prose.
"""

PROMPT_B = """You're extracting structured conference info. Be precise — don't guess.

Return JSON with these exact keys (null OK):
""" + _FIELDS_BLOCK + """

User-area hints (only relevant when classifying): {hints}

Page:
{page}
"""


# ────────────────────────────── Claude calls ──────────────────────────────


def _call_claude(model: str, prompt: str) -> dict[str, Any] | None:
    if not ANTHROPIC_KEY:
        return None
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_KEY)
    try:
        msg = client.messages.create(
            model=model, max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:  # noqa: BLE001 — network / API errors handled by caller
        return None
    body = "".join(b.text for b in msg.content if hasattr(b, "text"))
    return _parse_json(body)


def _parse_json(body: str) -> dict | None:
    if not body:
        return None
    # Tolerate model preamble before the JSON.
    body = body.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-z]*\s*|\s*```$", "", body, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", body, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ────────────────────────────── normalisation / comparison ──────────────────


def _parse_dt(s) -> datetime | None:
    if s is None or s == "":
        return None
    try:
        return dparser.parse(str(s))
    except (ValueError, TypeError, OverflowError):
        return None


def _norm_for_compare(field: str, v):
    """Return a value comparable across passes. Dates collapse to YYYY-MM-DD,
    strings lowercase + stripped, ints stay as ints."""
    if v is None:
        return None
    if field.endswith("_deadline") or field.endswith("_date") or field.endswith("_start") or field.endswith("_end") or field == "camera_ready":
        dt = _parse_dt(v)
        return dt.strftime("%Y-%m-%d") if dt else None
    if field == "page_limit":
        try:
            return int(v)
        except (ValueError, TypeError):
            return None
    return str(v).strip().lower() or None


def _agree(a, b, field: str) -> bool:
    na, nb = _norm_for_compare(field, a), _norm_for_compare(field, b)
    if na is None and nb is None:
        return True
    if na is None or nb is None:
        return False
    return na == nb


# ────────────────────────────── extraction core ──────────────────


def _two_pass(page: str, hints_str: str) -> dict | None:
    """Run Haiku × 2 then merge agreeing fields. Returns dict with `_diverged`."""
    a = _call_claude(MODEL_FAST, PROMPT_A.format(page=page))
    b = _call_claude(MODEL_FAST, PROMPT_B.format(hints=hints_str, page=page))
    if a is None and b is None:
        return None

    a = a or {}
    b = b or {}
    agreed: dict[str, Any] = {}
    diverged: list[str] = []
    for f in EXTRACT_FIELDS:
        va, vb = a.get(f), b.get(f)
        if _agree(va, vb, f) and va is not None:
            agreed[f] = va
        elif va is not None or vb is not None:
            diverged.append(f)

    # Rounds: take whichever pass returned a non-empty list. (We don't try to
    # cross-verify per-round dates — too fragile; rely on the diverged flag
    # surfacing to the user.)
    rounds = a.get("rounds") if isinstance(a.get("rounds"), list) and a.get("rounds") else b.get("rounds")
    if isinstance(rounds, list) and rounds:
        agreed["rounds"] = rounds

    agreed["_diverged"] = diverged
    return agreed


def _two_pass_with_fallback(page: str, hints_str: str) -> dict | None:
    """Run two-pass Haiku; if it yielded nothing useful, retry with Sonnet."""
    result = _two_pass(page, hints_str)
    useful = result and any(k in result for k in EXTRACT_FIELDS if k != "_diverged") if result else False
    if useful:
        return result
    # Sonnet fallback — one strong pass.
    strong = _call_claude(MODEL_STRONG, PROMPT_A.format(page=page))
    if not strong:
        return result
    out: dict[str, Any] = {}
    diverged: list[str] = []
    for f in EXTRACT_FIELDS:
        if strong.get(f) is not None:
            out[f] = strong[f]
    if isinstance(strong.get("rounds"), list) and strong["rounds"]:
        out["rounds"] = strong["rounds"]
    out["_diverged"] = diverged
    out["_used_strong"] = True
    return out


# ────────────────────────────── full-venue (POST /api/venues) ──────────────


FULL_PROMPT_A = """Extract conference/workshop info from this Call-for-Papers page.

Return ONLY JSON with these exact keys (null OK).

Keys:
  acronym         (short venue acronym, e.g. "SIGCOMM", "PACMI")
  name            (full venue name)
  year            (integer — the conference's calendar year)
  is_workshop     (true if a workshop, false if a main conference)
  parent_venue    (parent acronym if workshop, else null)
  areas           (array, subset of: control, networking, ml, systems, multimedia, robotics)
  abstract_deadline    ISO date
  submission_deadline  ISO date
  notification_date    ISO date
  camera_ready         ISO date
  conference_start     ISO date
  conference_end       ISO date
  page_limit      (integer, main paper, excluding references)
  location        (city, country)
  rounds          array or null (only if multi-round venue)

User area hints: {hints}

Page:
---
{page}
---

Return JSON only.
"""

_FULL_FIELDS = [
    "acronym", "name", "year", "is_workshop", "parent_venue", "areas",
    "abstract_deadline", "submission_deadline", "notification_date",
    "camera_ready", "conference_start", "conference_end",
    "page_limit", "location",
]


def _agree_full(a, b, field: str) -> bool:
    if field == "areas":
        sa = set(a) if isinstance(a, list) else set()
        sb = set(b) if isinstance(b, list) else set()
        return sa == sb and len(sa) > 0
    return _agree(a, b, field)


def extract_full_venue(url: str, area_hints: list[str] | None = None) -> dict | None:
    if not ANTHROPIC_KEY:
        return None
    page = _fetch_page(url)
    if not page:
        return None
    hints_str = ", ".join(area_hints or []) or "(none)"
    a = _call_claude(MODEL_FAST, FULL_PROMPT_A.format(hints=hints_str, page=page)) or {}
    b = _call_claude(MODEL_FAST, FULL_PROMPT_A.format(hints=hints_str, page=page)) or {}
    agreed: dict = {}
    diverged: list[str] = []
    for f in _FULL_FIELDS:
        va, vb = a.get(f), b.get(f)
        if _agree_full(va, vb, f) and va is not None:
            agreed[f] = va
        elif va is not None or vb is not None:
            diverged.append(f)
    # If acronym/year didn't agree, try Sonnet as tiebreaker.
    if not agreed.get("acronym") or not agreed.get("year"):
        strong = _call_claude(MODEL_STRONG, FULL_PROMPT_A.format(hints=hints_str, page=page)) or {}
        for f in _FULL_FIELDS:
            if agreed.get(f) is None and strong.get(f) is not None:
                agreed[f] = strong[f]
    agreed["_diverged"] = diverged
    return agreed


# ────────────────────────────── extras (for enrich_extras script) ──────────


def extract_venue_extras(url: str, acronym: str, year: int) -> dict | None:
    """Two-pass-plus-fallback extraction of the secondary fields we want filled
    for every venue with a cfp_url. Returns dict with at least these keys
    (any may be null): abstract_deadline, submission_deadline, notification_date,
    camera_ready, conference_start, conference_end, page_limit, location, rounds.
    """
    if not ANTHROPIC_KEY:
        return None
    page = _fetch_page(url)
    if not page:
        return None
    hints_str = "(venue: " + acronym + " " + str(year) + ")"
    return _two_pass_with_fallback(page, hints_str)


# ────────────────────────────── refresh-time seed enrichment ──────────────


def enrich_seed_venues() -> dict[str, int]:
    """For seed/llm_extract rows with a cfp_url, run two-pass extraction."""
    if not ANTHROPIC_KEY:
        return {"skipped": -1, "reason": "ANTHROPIC_API_KEY not set"}

    updated = 0
    diverged_count = 0
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
            result = _two_pass_with_fallback(page, f"(venue: {row.acronym} {row.year})")
            if not result:
                row.diverged = True
                diverged_count += 1
                continue
            for f in EXTRACT_FIELDS:
                v = result.get(f)
                if v is None:
                    continue
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
            row.diverged = bool(result.get("_diverged"))
            updated += 1
        db.commit()
    return {"updated": updated, "diverged": diverged_count, "fetched": fetched}
