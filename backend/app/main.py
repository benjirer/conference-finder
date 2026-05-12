from __future__ import annotations

import json
from datetime import datetime
import hashlib
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db, init_db
from .ical import build_ics
from .models import Conference, SourceRecord
from .sources import llm_extract, user_venues

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Conference Finder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _startup():
    init_db()


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


def _serialize(c: Conference) -> dict:
    def iso(dt: datetime | None):
        return dt.isoformat() if dt else None
    return {
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
        "source": c.source,
        "last_verified": iso(c.last_verified),
        "diverged": c.diverged,
        "diverged_detail": json.loads(c.diverged_detail) if c.diverged_detail else None,
        "predicted": c.predicted,
        "notes": c.notes,
    }


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
    rows = db.query(Conference).all()
    out = []
    now = datetime.utcnow()
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
    return [_serialize(r) for r in rows]


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
        return dt.isoformat() if dt else None
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


class AddVenueIn(BaseModel):
    url: str
    area_hints: list[str] | None = None


@app.post("/api/venues")
def add_venue(body: AddVenueIn):
    """Extract a venue's metadata from a CFP URL via two-pass LLM and persist it."""
    extracted = llm_extract.extract_full_venue(body.url, body.area_hints or [])
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

    row = user_venues.append_and_upsert(extracted, body.url, diverged)
    return _serialize(row)


@app.get("/api/health")
def health():
    return {"ok": True}
