from __future__ import annotations

import collections
import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .db import get_db, init_db
from .ical import build_ics
from .models import Conference, PCMember, SourceRecord
from .sources import llm_extract, user_venues

log = logging.getLogger("conference_finder")

STATIC_DIR = Path(__file__).parent / "static"

# ────────────────────────── rate limiting + URL validation ──────────────────────────

ADD_VENUE_WINDOW_SEC = 3600   # 1 hour
ADD_VENUE_MAX_PER_WINDOW = 5  # per client IP

# {ip: deque[timestamps]} — in-process token bucket. Reset on container restart,
# which is fine: each Render cold start gives us a fresh quota.
_add_venue_hits: dict[str, collections.deque] = {}


def _client_ip(req: Request) -> str:
    """Best-effort client IP. Render sets X-Forwarded-For; fall back to peer."""
    xff = req.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


def _enforce_add_venue_rate_limit(req: Request) -> None:
    ip = _client_ip(req)
    now = time.time()
    dq = _add_venue_hits.setdefault(ip, collections.deque())
    while dq and dq[0] < now - ADD_VENUE_WINDOW_SEC:
        dq.popleft()
    if len(dq) >= ADD_VENUE_MAX_PER_WINDOW:
        retry_in = int(dq[0] + ADD_VENUE_WINDOW_SEC - now)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {ADD_VENUE_MAX_PER_WINDOW} adds per hour. Retry in ~{retry_in}s.",
        )
    dq.append(now)


def _validate_cfp_url(url: str) -> str:
    """Reject obviously-bad URLs before burning any tokens."""
    if not isinstance(url, str):
        raise HTTPException(status_code=422, detail="url must be a string")
    url = url.strip()
    if len(url) < 8 or len(url) > 2048:
        raise HTTPException(status_code=422, detail="url length out of range (8..2048 chars)")
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise HTTPException(status_code=422, detail="url must start with http:// or https://")
    if not p.netloc or "." not in p.netloc:
        raise HTTPException(status_code=422, detail="url must include a real hostname")
    # Block obvious local / internal hosts to prevent SSRF abuse.
    host = p.hostname or ""
    if host in {"localhost"} or host.startswith("127.") or host.startswith("10.") \
       or host.startswith("192.168.") or host.endswith(".local") or host.endswith(".internal"):
        raise HTTPException(status_code=422, detail="url must point to a public hostname")
    return url


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan event — runs once on startup, replaces the deprecated
    `@app.on_event('startup')` pattern."""
    init_db()
    from .scheduler import start_refresh_worker
    stop, thread = start_refresh_worker()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="Conference Finder", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _asset_version() -> str:
    """Hash of the bundled JS+CSS for cache-busting. Cheap enough to recompute per request."""
    h = hashlib.sha1()
    for f in ("app.js", "styles.css"):
        try:
            h.update((STATIC_DIR / f).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


@app.get("/", response_class=HTMLResponse)
def root():
    html = (STATIC_DIR / "index.html").read_text()
    v = _asset_version()
    html = html.replace('/static/app.js"', f'/static/app.js?v={v}"')
    html = html.replace('/static/styles.css"', f'/static/styles.css?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, max-age=0"})


def _serialize(c: Conference, pc_members_count: int = 0) -> dict:
    def iso(dt: datetime | None):
        return dt.isoformat() + "Z" if dt else None
    metadata = json.loads(c.date_metadata or "{}")
    result = {
        "date_metadata": metadata,
        "id": c.id,
        "acronym": c.acronym,
        "name": c.name,
        "year": c.year,
        "round": c.round,
        "rounds_total": c.rounds_total,
        "areas": json.loads(c.areas or "[]"),
        "topics": json.loads(c.topics or "[]"),
        "is_workshop": c.is_workshop,
        "parent_venue": c.parent_venue,
        "abstract_deadline": iso(c.abstract_deadline),
        "submission_deadline": iso(c.submission_deadline),
        "notification_date": iso(c.notification_date),
        "camera_ready": iso(c.camera_ready),
        "conference_start": iso(c.conference_start),
        "conference_end": iso(c.conference_end),
        "timezone": c.timezone,
        "page_limit": c.page_limit,
        "format_notes": c.format_notes,
        "h5_index": c.h5_index,
        "acceptance_rate": c.acceptance_rate,
        "tier": c.tier,
        "tier_predicted": c.tier_predicted,
        "location": c.location,
        "latitude": c.latitude,
        "longitude": c.longitude,
        "website": c.website,
        "cfp_url": c.cfp_url,
        "pc_url": c.pc_url,
        "pc_members_count": pc_members_count,
        "source": c.source,
        "last_verified": iso(c.last_verified),
        "diverged": c.diverged,
        "diverged_detail": json.loads(c.diverged_detail) if c.diverged_detail else None,
        "predicted": c.predicted,
        "notes": c.notes,
    }
    for field, info in metadata.items():
        if field in result and result[field] and info.get('precision') == 'date':
            result[field] = result[field][:10]
    return result


def _filter(
    db: Session,
    area: list[str] | None,
    workshops: str,
    deadline: str,
    predicted: str,
    diverged: str,
    year: list[int] | None,
    q: str | None,
) -> list[Conference]:
    from .sources import _common as _c
    rows = db.query(Conference).all()
    out = []
    now = _c.utc_now()
    for r in rows:
        r_areas = json.loads(r.areas or "[]")
        if area and not (set(area) & set(r_areas)):
            continue
        if year and r.year not in year:
            continue
        if workshops == "only" and not r.is_workshop:
            continue
        if workshops == "exclude" and r.is_workshop:
            continue
        if predicted == "only" and not r.predicted:
            continue
        if predicted == "exclude" and r.predicted:
            continue
        if diverged == "only" and not r.diverged:
            continue
        if diverged == "exclude" and r.diverged:
            continue

        # Deadline-state filter operates on submission_deadline (or abstract if no submission).
        primary_dl = r.submission_deadline or r.abstract_deadline
        if deadline == "upcoming":
            if primary_dl is None or primary_dl < now:
                continue
        elif deadline == "passed":
            if primary_dl is None or primary_dl >= now:
                continue
        # deadline == "all" — no filter

        if q:
            blob = f"{r.acronym} {r.name} {r.location or ''}".lower()
            if q.lower() not in blob:
                continue
        out.append(r)
    out.sort(key=lambda c: (c.submission_deadline or c.conference_start or datetime.max))
    return out


@app.get("/api/conferences")
def list_conferences(
    db: Session = Depends(get_db),
    area: list[str] | None = Query(default=None),
    workshops: str = Query(default="all", pattern="^(all|only|exclude)$"),
    deadline: str = Query(default="upcoming", pattern="^(upcoming|passed|all)$"),
    predicted: str = Query(default="all", pattern="^(all|only|exclude)$"),
    diverged: str = Query(default="all", pattern="^(all|only|exclude)$"),
    year: list[int] | None = Query(default=None),
    q: str | None = Query(default=None),
):
    rows = _filter(db, area, workshops, deadline, predicted, diverged, year, q)
    # PC members are stored on round=1 rows; reflect the same count on every
    # round of the same (acronym, year) so the UI can show "has PC data" on
    # any row.
    from sqlalchemy import func
    pc_rows = (
        db.query(Conference.acronym, Conference.year, func.count(PCMember.id))
        .join(PCMember, PCMember.conference_id == Conference.id)
        .filter(Conference.round == 1)
        .group_by(Conference.acronym, Conference.year)
        .all()
    )
    pc_count_by_key = {(a, y): n for a, y, n in pc_rows}
    return [_serialize(r, pc_count_by_key.get((r.acronym, r.year), 0)) for r in rows]


@app.get("/api/years")
def list_years(db: Session = Depends(get_db)):
    rows = db.query(Conference.year).distinct().order_by(Conference.year).all()
    return [r[0] for r in rows]


@app.get("/api/conferences/{conf_id}/sources")
def conference_sources(conf_id: int, db: Session = Depends(get_db)):
    """All per-source records (raw aggregator data) for one conference row."""
    c = db.query(Conference).filter_by(id=conf_id).one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="conference not found")
    records = (
        db.query(SourceRecord)
        .filter_by(acronym=c.acronym, year=c.year)
        .order_by(SourceRecord.source)
        .all()
    )
    def iso(dt: datetime | None):
        return dt.isoformat() + "Z" if dt else None
    return {
        "acronym": c.acronym,
        "year": c.year,
        "canonical_source": c.source,
        "diverged": c.diverged,
        "sources": [
            {
                "source": r.source,
                "abstract_deadline": iso(r.abstract_deadline),
                "submission_deadline": iso(r.submission_deadline),
                "notification_date": iso(r.notification_date),
                "conference_start": iso(r.conference_start),
                "conference_end": iso(r.conference_end),
                "name": r.name,
                "location": r.location,
                "link": r.link,
                "fetched_at": iso(r.fetched_at),
            }
            for r in records
        ],
    }


@app.get("/api/conferences/{conf_id}/pc")
def conference_pc(conf_id: int, db: Session = Depends(get_db)):
    """Full PC member list for one conference row."""
    c = db.query(Conference).filter_by(id=conf_id).one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="conference not found")
    # Look up PC on round=1 (we don't store per-round PCs).
    canonical_id = c.id
    if c.round != 1:
        canonical = db.query(Conference).filter_by(acronym=c.acronym, year=c.year, round=1).one_or_none()
        if canonical is not None:
            canonical_id = canonical.id
    members = (
        db.query(PCMember)
        .filter_by(conference_id=canonical_id)
        .order_by(PCMember.role, PCMember.name)
        .all()
    )
    return {
        "acronym": c.acronym,
        "year": c.year,
        "pc_url": c.pc_url,
        "members_total": len(members),
        "members": [
            {"name": m.name, "normalized_name": m.normalized_name,
             "affiliation": m.affiliation, "role": m.role}
            for m in members
        ],
    }


class ComparePCIn(BaseModel):
    # Cap at 20 to keep the response bounded and prevent DoS via huge lists.
    conference_ids: list[int] = Field(..., min_length=2, max_length=20)

    @field_validator("conference_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        # Preserve order, drop duplicates.
        seen: set[int] = set()
        out: list[int] = []
        for cid in v:
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
        if len(out) < 2:
            raise ValueError("Provide at least 2 distinct conference_ids.")
        return out


@app.post("/api/pc/compare")
def compare_pc(body: ComparePCIn, db: Session = Depends(get_db)):
    """Intersection-style comparison across N conferences.

    Returns:
      - venues:        per-venue metadata + PC size
      - intersection:  list of normalized_names present in ALL selected venues,
                       with per-venue (name as written, affiliation, role) tuples
      - pairwise:      {[id_a, id_b]: count} overlap counts for each pair
    """
    confs = db.query(Conference).filter(Conference.id.in_(body.conference_ids)).all()
    if len(confs) != len(set(body.conference_ids)):
        raise HTTPException(status_code=404, detail="Some conference_ids were not found.")

    venue_meta: dict[int, dict] = {}
    members_by_venue: dict[int, dict[str, PCMember]] = {}
    for c in confs:
        canonical_id = c.id
        if c.round != 1:
            canonical = db.query(Conference).filter_by(acronym=c.acronym, year=c.year, round=1).one_or_none()
            if canonical is not None:
                canonical_id = canonical.id
        members = db.query(PCMember).filter_by(conference_id=canonical_id).all()
        members_by_venue[c.id] = {m.normalized_name: m for m in members}
        venue_meta[c.id] = {
            "id": c.id, "acronym": c.acronym, "year": c.year,
            "round": c.round, "rounds_total": c.rounds_total,
            "pc_url": c.pc_url, "pc_size": len(members),
        }

    # Intersection of normalized names across ALL selected venues.
    name_sets = [set(d.keys()) for d in members_by_venue.values()]
    common = set.intersection(*name_sets) if name_sets and all(name_sets) else set()

    intersection = []
    for norm in sorted(common):
        # Use the longest written-name across venues as the display name.
        names = [members_by_venue[cid][norm].name for cid in body.conference_ids]
        display = max(names, key=len)
        per_venue = {}
        for cid in body.conference_ids:
            m = members_by_venue[cid][norm]
            per_venue[str(cid)] = {
                "name": m.name, "affiliation": m.affiliation, "role": m.role,
            }
        intersection.append({
            "normalized_name": norm,
            "name": display,
            "per_venue": per_venue,
        })

    # Pairwise overlap counts — useful when comparing 3+ venues.
    pairwise = []
    ids = list(body.conference_ids)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            overlap = members_by_venue[a].keys() & members_by_venue[b].keys()
            pairwise.append({"a": a, "b": b, "count": len(overlap)})

    return {
        "venues": [venue_meta[i] for i in body.conference_ids],
        "intersection_size": len(intersection),
        "intersection": intersection,
        "pairwise": pairwise,
    }


@app.get("/api/areas")
def list_areas(db: Session = Depends(get_db)):
    out: set[str] = set()
    for r in db.query(Conference).all():
        out.update(json.loads(r.areas or "[]"))
    return sorted(out)


@app.get("/calendar.ics")
def calendar_feed(
    db: Session = Depends(get_db),
    area: list[str] | None = Query(default=None),
    workshops: str = Query(default="all", pattern="^(all|only|exclude)$"),
    deadline: str = Query(default="upcoming", pattern="^(upcoming|passed|all)$"),
    predicted: str = Query(default="all", pattern="^(all|only|exclude)$"),
    diverged: str = Query(default="all", pattern="^(all|only|exclude)$"),
    year: list[int] | None = Query(default=None),
):
    rows = _filter(db, area, workshops, deadline, predicted, diverged, year, q=None)
    return Response(content=build_ics(rows), media_type="text/calendar; charset=utf-8")


_AREA_VOCAB = {"control", "networking", "ml", "systems", "multimedia", "robotics"}


class AddVenueIn(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    area_hints: list[str] | None = Field(default=None, max_length=10)

    @field_validator("area_hints")
    @classmethod
    def _filter_areas(cls, v):
        if not v:
            return v
        return [a for a in v if isinstance(a, str) and a.lower() in _AREA_VOCAB]


@app.post("/api/venues")
def add_venue(body: AddVenueIn, request: Request):
    """Extract a venue's metadata from a CFP URL via two-pass LLM and persist it.

    Rate-limited per client IP (5/hour) and URL-validated to prevent token-burn
    abuse on the public Render deployment.
    """
    _enforce_add_venue_rate_limit(request)
    url = _validate_cfp_url(body.url)
    extracted = llm_extract.extract_full_venue(url, body.area_hints or [])
    if extracted is None:
        raise HTTPException(
            status_code=502,
            detail=(
                "LLM extraction failed. Either ANTHROPIC_API_KEY is unset, the "
                "URL is unreachable, or the page returned no usable text."
            ),
        )
    diverged = extracted.pop("_diverged", [])
    if not extracted.get("acronym") or not extracted.get("year"):
        raise HTTPException(
            status_code=422,
            detail=(
                "Two-pass extraction couldn't agree on the venue's acronym or year. "
                f"Fields that diverged: {diverged}. Try editing data/user_added.yaml manually."
            ),
        )
    # Year must be int.
    try:
        extracted["year"] = int(extracted["year"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Extracted year is not an integer.")

    from . import review_updates
    review_url = None
    if review_updates.enabled():
        try:
            review_url = review_updates.propose('user', f"{extracted['acronym']} {extracted['year']}",
                {'venue': extracted, 'url': url, 'diverged': diverged, 'submitted_at': datetime.utcnow().isoformat()})
            return {**extracted, 'review_url': review_url, 'status': 'pending_review'}
        except Exception as exc:
            import httpx
            log.exception('Could not create venue review proposal')
            detail = 'GitHub could not save the venue proposal.'
            if isinstance(exc, httpx.HTTPStatusError):
                detail += f' GitHub returned HTTP {exc.response.status_code}.'
                if exc.response.status_code in (401, 403):
                    detail += ' Check the token permissions for Contents and Pull requests.'
            elif isinstance(exc, httpx.RequestError):
                detail += ' GitHub could not be reached.'
            raise HTTPException(status_code=503, detail=detail + ' The update is not confirmed.')
    row = user_venues.append_and_upsert(extracted, body.url, diverged)
    return {**_serialize(row), "review_url": review_url}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/refresh-status")
def refresh_status():
    from .scheduler import status
    return dict(status)


@app.get("/api/official-checks")
def official_checks(db: Session = Depends(get_db)):
    from .models import OfficialCheck
    return [{"acronym": check.acronym, "year": check.year, "url": check.url,
             "checked_at": check.checked_at.isoformat() + "Z" if check.checked_at else None,
             "verified_at": check.verified_at.isoformat() + "Z" if check.verified_at else None,
             "error": check.error}
            for check in db.query(OfficialCheck).order_by(OfficialCheck.checked_at.desc()).all()]
