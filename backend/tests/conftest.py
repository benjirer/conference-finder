"""Shared pytest fixtures.

We point the DB at a temp file per test so the production conferences.db is
never touched. The app's `db.engine` is monkey-patched to use the temp file.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the app's SQLAlchemy engine + every cached SessionLocal at a
    fresh empty SQLite file.

    Why we patch every module: most modules `from ..db import SessionLocal` at
    import time, so swapping just `db.SessionLocal` doesn't reach those cached
    references. We iterate over every loaded `app.*` module and rebind.
    """
    import sys
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_file = tmp_path / "test.db"
    monkeypatch.setenv("CONFERENCE_FINDER_NO_DESTRUCTIVE", "0")
    monkeypatch.setenv("CONFERENCE_FINDER_REFRESH_HOURS", "0")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("CONFERENCE_FINDER_GITHUB_TOKEN", raising=False)

    new_engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False},
    )
    new_session_local = sessionmaker(bind=new_engine, autoflush=False, autocommit=False)

    from app import db as db_mod
    monkeypatch.setattr(db_mod, "engine", new_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session_local)

    # Reach every cached `SessionLocal` reference across `app.*` modules.
    for name, mod in list(sys.modules.items()):
        if not name.startswith("app.") or mod is None:
            continue
        if getattr(mod, "SessionLocal", None) is not None and mod is not db_mod:
            monkeypatch.setattr(mod, "SessionLocal", new_session_local)

    from app.sources import user_venues
    monkeypatch.setattr(user_venues, 'USER_FILE', tmp_path / 'user_added.yaml')
    yield db_file


@pytest.fixture
def client(temp_db):
    """A FastAPI TestClient bound to the temp DB. Resets the in-process
    rate-limit counters before each test so tests don't bleed into each other.
    """
    from fastapi.testclient import TestClient
    from app.main import app, _add_venue_hits
    from app import db as db_mod
    db_mod.init_db()
    _add_venue_hits.clear()
    with TestClient(app) as c:
        yield c
