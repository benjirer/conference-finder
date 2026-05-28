"""End-to-end refresh smoke test.

Runs the full pipeline against a fresh DB with all network/aggregator steps
patched out, so we exercise the ORM + helpers without hitting the public
internet. Confirms the steps wire together cleanly and produce a usable DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Conference


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def offline_refresh(temp_db, monkeypatch):
    """Patch every network-bound step to be a no-op so we can run refresh offline."""
    from app.sources import (
        ccfddl, aideadlines, ds_deadlines, klb2, noise_lab, confsearch,
        cached_extras, cached_pc, llm_extract, geocode,
    )
    monkeypatch.setattr(ccfddl, "ingest_all", lambda: {"upserted": 0})
    monkeypatch.setattr(aideadlines, "ingest_all", lambda: {"added": 0, "recorded": 0})
    monkeypatch.setattr(ds_deadlines, "ingest_all", lambda: {"added": 0, "recorded": 0})
    monkeypatch.setattr(klb2, "ingest_all", lambda: {"added": 0, "recorded": 0, "file_errors": 0})
    monkeypatch.setattr(noise_lab, "ingest_all", lambda: {"added": 0, "recorded": 0})
    monkeypatch.setattr(confsearch, "ingest_all",
                        lambda: {"added": 0, "recorded": 0, "queries": 0, "skipped_old": 0, "skipped_deleted": 0})


def test_refresh_runs_clean_with_no_aggregators(offline_refresh, capsys):
    """All 16 pipeline steps must complete without raising."""
    from app.refresh import main
    main()
    out = capsys.readouterr().out
    # Every step should have printed its slot label.
    for i in range(1, 17):
        assert f"[{i:>2}/16]" in out or f"[{i:>2}/17]" in out


def test_refresh_picks_up_seed_yaml(offline_refresh, monkeypatch, tmp_path):
    """The seed YAML overlay should land in the DB."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("""\
venues:
  - acronym: TESTCONF
    year: 2026
    areas: [control]
    cfp_url: https://example.com/cfp
""")
    from app.sources import seed
    monkeypatch.setattr(seed, "SEED_FILE", seed_file)

    from app.refresh import main
    main()

    from app.db import SessionLocal
    with SessionLocal() as db:
        row = db.query(Conference).filter_by(acronym="TESTCONF").one_or_none()
        assert row is not None
        assert row.year == 2026
        assert row.cfp_url == "https://example.com/cfp"


def test_predict_step_extrapolates_next_year(offline_refresh, monkeypatch, tmp_path):
    """A real-dated venue today should get a predicted entry one year out."""
    seed_file = tmp_path / "seed.yaml"
    in_three_months = (_utc_now() + timedelta(days=90)).date().isoformat()
    seed_file.write_text(f"""\
venues:
  - acronym: PREDME
    year: 2026
    submission_deadline: "{in_three_months}"
    cfp_url: https://example.com/cfp
""")
    from app.sources import seed
    monkeypatch.setattr(seed, "SEED_FILE", seed_file)

    from app.refresh import main
    main()

    from app.db import SessionLocal
    with SessionLocal() as db:
        future = db.query(Conference).filter_by(acronym="PREDME", year=2027).one_or_none()
        assert future is not None
        assert future.predicted is True
        assert future.source == "predicted"
