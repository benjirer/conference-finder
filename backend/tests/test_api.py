"""API-surface smoke tests.

Each test starts with an empty DB (via the temp_db fixture) so we don't depend
on production data. We seed a couple of rows directly through the models when
we need them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Conference, PCMember


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_minimal(db, **overrides):
    """Insert one canonical row + return its id."""
    row = Conference(
        acronym=overrides.get("acronym", "TEST"),
        year=overrides.get("year", 2026),
        round=1,
        name=overrides.get("name", "Test Conference"),
        submission_deadline=overrides.get(
            "submission_deadline", _utc_now() + timedelta(days=30)
        ),
        source="seed",
    )
    db.add(row)
    db.commit()
    return row.id


# ───────────────── health + list endpoints ─────────────────

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Conference Finder" in r.text
    # Cache-bust hash should be present.
    assert "?v=" in r.text


def test_static_assets(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/favicon.svg").status_code == 200


def test_list_conferences_empty(client):
    r = client.get("/api/conferences?deadline=all&predicted=all")
    assert r.status_code == 200
    assert r.json() == []


def test_list_conferences_with_seed(client):
    from app.db import SessionLocal
    with SessionLocal() as db:
        cid = _seed_minimal(db)
    r = client.get("/api/conferences?deadline=all&predicted=all")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == cid
    assert rows[0]["pc_members_count"] == 0


def test_calendar_ics_serves(client):
    r = client.get("/calendar.ics?deadline=all&predicted=all")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VCALENDAR" in r.text


def test_list_years(client):
    from app.db import SessionLocal
    with SessionLocal() as db:
        _seed_minimal(db, year=2026)
        _seed_minimal(db, acronym="OTHER", year=2025)
    r = client.get("/api/years")
    assert r.status_code == 200
    assert r.json() == [2025, 2026]


# ───────────────── PC compare guards ─────────────────

def test_compare_pc_rejects_single_id(client):
    r = client.post("/api/pc/compare", json={"conference_ids": [1]})
    assert r.status_code == 422


def test_compare_pc_rejects_too_many_ids(client):
    r = client.post("/api/pc/compare", json={"conference_ids": list(range(1, 25))})
    assert r.status_code == 422


def test_compare_pc_dedups(client):
    r = client.post("/api/pc/compare", json={"conference_ids": [1, 1]})
    # After dedup, only 1 unique → still fails min_length.
    assert r.status_code == 422


def test_compare_pc_basic(client):
    from app.db import SessionLocal
    with SessionLocal() as db:
        a = _seed_minimal(db, acronym="A", year=2026)
        b = _seed_minimal(db, acronym="B", year=2026)
        # One shared person.
        for cid, name in [(a, "Alice Smith"), (b, "Alice Smith"), (a, "Bob Jones")]:
            db.add(PCMember(
                conference_id=cid, name=name,
                normalized_name=name.lower(),
                affiliation=None, role="member",
            ))
        db.commit()
    r = client.post("/api/pc/compare", json={"conference_ids": [a, b]})
    assert r.status_code == 200
    body = r.json()
    assert body["intersection_size"] == 1
    assert body["intersection"][0]["name"] == "Alice Smith"


# ───────────────── add-venue guards ─────────────────

def test_add_venue_rejects_bad_url_shape(client):
    r = client.post("/api/venues", json={"url": "not-a-url"})
    assert r.status_code == 422


def test_add_venue_rejects_ftp(client):
    r = client.post("/api/venues", json={"url": "ftp://example.com/cfp"})
    assert r.status_code == 422


def test_add_venue_rejects_localhost(client):
    r = client.post("/api/venues", json={"url": "http://localhost/admin"})
    assert r.status_code == 422


def test_add_venue_rejects_private_ip(client):
    r = client.post("/api/venues", json={"url": "http://10.0.0.1/cfp"})
    assert r.status_code == 422


def test_add_venue_rate_limited(client, monkeypatch):
    # Force the LLM extractor to return None (no key set) so we don't make real
    # API calls. The rate-limit counter increments BEFORE that, so we hit 429
    # on the 6th attempt regardless.
    from app.sources import llm_extract
    monkeypatch.setattr(llm_extract, "extract_full_venue", lambda *a, **kw: None)
    for _ in range(5):
        r = client.post("/api/venues", json={"url": "https://example.com/cfp"})
        # 502 because extract_full_venue returns None; that's fine, what we're
        # testing is that the rate limiter lets us through 5 times.
        assert r.status_code in (502, 422)
    r = client.post("/api/venues", json={"url": "https://example.com/cfp"})
    assert r.status_code == 429
