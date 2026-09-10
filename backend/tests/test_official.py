import json
from datetime import datetime, timedelta

import pytest

from app.sources import official, llm_extract
from app.models import Conference, OfficialCheck


@pytest.fixture
def setup_official(temp_db, monkeypatch):
    from app.db import init_db, SessionLocal
    init_db()
    monkeypatch.setenv('CONFERENCE_FINDER_OFFICIAL_HOURS', '0')
    monkeypatch.setenv('CONFERENCE_FINDER_OFFICIAL_LIMIT', '20')
    monkeypatch.setattr(llm_extract, 'ANTHROPIC_KEY', 'test')
    monkeypatch.setattr(llm_extract, '_fetch_page', lambda url: 'official content')
    year = datetime.now().year + 1
    result = {'acronym': 'TEST', 'year': year,
              'submission_deadline': f'{year}-02-01', '_diverged': []}
    monkeypatch.setattr(llm_extract, 'extract_full_venue_page', lambda *args: result)
    with SessionLocal() as db:
        db.add(Conference(acronym='TEST', year=year, name='Test', predicted=True,
                          source='predicted', cfp_url='https://example.com/cfp',
                          submission_deadline=datetime(year, 1, 1),
                          camera_ready=datetime(year, 5, 1)))
        db.commit()
    return SessionLocal, year, result


def test_promotes_prediction_and_preserves_cache_across_refresh(setup_official, monkeypatch):
    Session, year, _ = setup_official
    assert official.refresh_official()['checked'] == 1
    with Session() as db:
        row = db.query(Conference).one()
        assert row.submission_deadline == datetime(year, 2, 1)
        assert not row.predicted
        assert row.camera_ready is None
        row.submission_deadline = datetime(year, 1, 1)  # stale aggregator snapshot
        db.commit()
    def unexpected(*args):
        pytest.fail('Unchanged pages must not call the LLM')
    monkeypatch.setattr(llm_extract, 'extract_full_venue_page', unexpected)
    assert official.refresh_official()['unchanged'] == 1
    with Session() as db:
        assert db.query(Conference).one().submission_deadline == datetime(year, 2, 1)
        check = db.query(OfficialCheck).one()
        assert check.verified_at and check.content_hash and not check.error


def test_failed_changed_page_preserves_dates_and_retries(setup_official, monkeypatch):
    Session, year, _ = setup_official
    official.refresh_official()
    monkeypatch.setattr(llm_extract, '_fetch_page', lambda url: 'changed content')
    monkeypatch.setattr(llm_extract, 'extract_full_venue_page', lambda *args: None)
    assert official.refresh_official()['errors'] == 1
    with Session() as db:
        assert db.query(Conference).one().submission_deadline == datetime(year, 2, 1)
        assert db.query(OfficialCheck).one().error
    monkeypatch.setattr(llm_extract, 'extract_full_venue_page', lambda *args: {
        'acronym': 'TEST', 'year': year, 'submission_deadline': f'{year}-03-01'})
    assert official.refresh_official()['errors'] == 0
    with Session() as db:
        assert db.query(Conference).one().submission_deadline == datetime(year, 3, 1)


@pytest.mark.parametrize('change', [
    {'acronym': 'OTHER'}, {'year': 2001}, {'_diverged': ['year']},
    {'submission_deadline': 'TBA'}, {'_diverged': ['submission_deadline']},
    {'rounds': [{'round': 2}], '_diverged': ['rounds']},
])
def test_rejects_unsafe_results(setup_official, change):
    Session, year, result = setup_official
    result.update(change)
    assert official.refresh_official()['errors'] == 1
    with Session() as db:
        assert db.query(Conference).one().predicted
        assert db.query(OfficialCheck).one().verified_at is None


def test_rounds_are_applied_separately(setup_official):
    Session, year, result = setup_official
    result['rounds'] = [
        {'round': 1, 'submission_deadline': f'{year}-02-01'},
        {'round': 2, 'submission_deadline': f'{year}-04-01'}]
    official.refresh_official()
    with Session() as db:
        rows = db.query(Conference).order_by(Conference.round).all()
        assert [row.submission_deadline.month for row in rows] == [2, 4]


def test_due_interval_and_limit(setup_official, monkeypatch):
    Session, year, _ = setup_official
    monkeypatch.setenv('CONFERENCE_FINDER_OFFICIAL_ENABLED', '0')
    assert official.refresh_official()['pending'] == 1
    monkeypatch.setenv('CONFERENCE_FINDER_OFFICIAL_ENABLED', '1')
    monkeypatch.setenv('CONFERENCE_FINDER_OFFICIAL_LIMIT', '1')
    assert official.refresh_official()['checked'] == 1
    monkeypatch.setenv('CONFERENCE_FINDER_OFFICIAL_HOURS', '24')
    assert official.refresh_official()['checked'] == 0


def test_missing_key_is_reported(setup_official, monkeypatch):
    Session, _, _ = setup_official
    monkeypatch.setattr(llm_extract, 'ANTHROPIC_KEY', None)
    assert official.refresh_official()['errors'] == 1
    with Session() as db:
        assert 'ANTHROPIC_API_KEY' in db.query(OfficialCheck).one().error


def test_check_status_endpoint(client):
    assert client.get('/api/official-checks').json() == []


def test_failed_pages_do_not_starve_other_venues(setup_official, monkeypatch):
    Session, year, _ = setup_official
    monkeypatch.setenv('CONFERENCE_FINDER_OFFICIAL_LIMIT', '1')
    monkeypatch.setattr(llm_extract, '_fetch_page', lambda url: None)
    official.refresh_official()
    with Session() as db:
        db.add(Conference(acronym='SECOND', year=year, name='Second',
                          cfp_url='https://second.example/cfp'))
        db.commit()
    official.refresh_official()
    with Session() as db:
        assert db.query(OfficialCheck).filter_by(acronym='SECOND').one().checked_at


def test_cached_dates_reapplied_when_checks_disabled(setup_official, monkeypatch):
    Session, year, _ = setup_official
    official.refresh_official()
    with Session() as db:
        db.query(Conference).one().submission_deadline = datetime(year, 1, 1)
        db.commit()
    monkeypatch.setenv('CONFERENCE_FINDER_OFFICIAL_ENABLED', '0')
    official.refresh_official()
    with Session() as db:
        assert db.query(Conference).one().submission_deadline == datetime(year, 2, 1)


def test_discovers_current_edition_without_reusing_old_dates(setup_official, monkeypatch):
    from app.sources import pages
    Session, year, result = setup_official
    monkeypatch.setattr(pages, 'edition_candidates', lambda *args: ['https://example.com/current'])
    monkeypatch.setattr(llm_extract, '_fetch_page', lambda url: 'new edition' if url.endswith('/current') else 'old edition')
    monkeypatch.setattr(llm_extract, 'extract_full_venue_page', lambda page, *args: result if page == 'new edition' else {**result, 'year': year - 1})
    assert official.refresh_official()['errors'] == 0
    with Session() as db:
        row = db.query(Conference).one()
        assert row.cfp_url == 'https://example.com/current'
        assert row.submission_deadline == datetime(year, 2, 1)
        assert db.query(OfficialCheck).one().url == 'https://example.com/current'


def test_official_changes_wait_for_pr_approval(setup_official, monkeypatch):
    from app import review_updates
    Session, year, _ = setup_official
    monkeypatch.setattr(review_updates, 'enabled', lambda: True)
    monkeypatch.setattr(review_updates, 'propose', lambda *args: 'https://github.com/example/repo/pull/1')
    official.refresh_official()
    with Session() as db:
        assert db.query(Conference).one().predicted
        assert db.query(OfficialCheck).one().payload is None
        assert 'Pending review' in db.query(OfficialCheck).one().error
    def unexpected(*args):
        pytest.fail('Do not spend tokens again on the same pending proposal')
    monkeypatch.setattr(llm_extract, 'extract_full_venue_page', unexpected)
    official.refresh_official()
