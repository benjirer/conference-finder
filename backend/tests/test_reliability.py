from datetime import datetime

import httpx
import pytest

from app.sources import _common, user_venues, llm_extract, ccfddl


@pytest.mark.parametrize('value,zone,expected', [
    ('2026-07-01 12:00:00', 'Europe/Zurich', datetime(2026, 7, 1, 10)),
    ('2026-01-01 12:00:00', 'Europe/Zurich', datetime(2026, 1, 1, 11)),
    ('2026-07-01 12:00:00', 'UTC+05:30', datetime(2026, 7, 1, 6, 30)),
    ('2026-07-01T12:00:00+02:00', 'AoE', datetime(2026, 7, 1, 10)),
])
def test_deadline_timezones(value, zone, expected):
    assert _common.parse_ccfddl_timestamp(value, zone) == expected


@pytest.mark.parametrize('value', ['TBA', '2026', 'July 2026', 'July 5', '2026-99-32'])
def test_incomplete_dates_are_not_invented(value):
    assert _common.parse_iso_date(value) is None


def test_prediction_replaced_and_deadline_updated(temp_db):
    from app.db import init_db, SessionLocal
    from app.models import Conference
    init_db()
    with SessionLocal() as db:
        db.add(Conference(acronym='TEST', name='Test', year=2027, predicted=True,
                          source='predicted', submission_deadline=datetime(2027, 1, 1),
                          camera_ready=datetime(2027, 3, 1)))
        db.commit()
        for day in (2, 9):
            _common.upsert_conference_secondary(db, acronym='TEST', name='Test', year=2027,
                submission_deadline=datetime(2027, 2, day), source_name='aideadlines')
            db.commit()
            row = db.query(Conference).one()
            assert row.submission_deadline == datetime(2027, 2, day)
            assert not row.predicted
            assert row.camera_ready is None


def test_user_venue_survives_database_rebuild(temp_db, tmp_path, monkeypatch):
    from app.db import init_db, SessionLocal
    from app.models import Conference
    init_db()
    monkeypatch.setattr(user_venues, 'USER_FILE', tmp_path / 'user.yaml')
    user_venues.append_and_upsert({'acronym': 'USENIX NSDI', 'year': 2027,
        'submission_deadline': '2026-09-30T23:00:00-07:00'}, 'https://example.com', [])
    with SessionLocal() as db:
        db.query(Conference).delete()
        db.commit()
    user_venues.ingest_user_added()
    with SessionLocal() as db:
        row = db.query(Conference).one()
        assert row.acronym == 'NSDI'
        assert row.submission_deadline == datetime(2026, 10, 1, 6)


def test_fetches_linked_dates_and_removes_superseded_text(monkeypatch):
    pages = {
        'https://example.com': '<main>Test Conference 2027<a href="/cfp">Call for papers</a></main>',
        'https://example.com/cp': '',
        'https://example.com/cfp': '<main>Submission deadline <del>June 1, 2027</del> June 15, 2027. Conference July 5–8.</main>',
    }
    monkeypatch.setattr(llm_extract, '_public_get', lambda url: httpx.Response(200,
        text=pages[url], request=httpx.Request('GET', url)))
    text = llm_extract._fetch_page('https://example.com')
    assert 'June 15, 2027' in text
    assert 'June 1, 2027' not in text


def test_discovery_adds_venues_and_preserves_curated_metadata(monkeypatch):
    monkeypatch.setattr(_common, 'http_get', lambda *a, **k: httpx.Response(200, json={
        'tree': [{'path': 'conference/NW/newconf.yml'}, {'path': 'conference/AI/icml.yml'}]}))
    venues = ccfddl.discover_venues()
    assert venues['NW', 'newconf.yml']['areas'] == ['networking']
    assert venues['AI', 'icml.yml']['tier'] == 'A*'


def test_model_dates_compare_times_not_just_days():
    assert not llm_extract._agree('2026-07-01T10:00:00Z', '2026-07-01T12:00:00Z', 'submission_deadline')
    assert llm_extract._agree('2026-07-01T10:00:00Z', '2026-07-01T12:00:00+02:00', 'submission_deadline')


def test_day_first_date_range():
    assert _common.parse_date_range('5–8 July 2027', 2027) == (datetime(2027, 7, 5), datetime(2027, 7, 8))


def test_api_marks_utc(client):
    from app.main import _serialize
    from app.models import Conference
    row = Conference(acronym='TEST', name='Test', year=2027,
                     submission_deadline=datetime(2026, 10, 1, 6))
    assert _serialize(row)['submission_deadline'] == '2026-10-01T06:00:00Z'


def test_refresh_status(client):
    response = client.get('/api/refresh-status')
    assert response.status_code == 200
    assert response.json()['running'] is False


def test_alias_merge_replaces_prediction_and_keeps_id(temp_db):
    from app.db import init_db, SessionLocal
    from app.models import Conference, PCMember
    from app.sources.cleanup import canonicalize_existing
    init_db()
    with SessionLocal() as db:
        existing = Conference(acronym='CGO', year=2027, name='CGO', predicted=True, source='predicted')
        alias = Conference(acronym='IEEE/ACM CGO', year=2027, name='CGO', source='ccfddl', submission_deadline=datetime(2026, 6, 12, 11, 59, 59))
        db.add_all([existing, alias])
        db.flush()
        original_id = existing.id
        db.add(PCMember(conference_id=alias.id, name='Test Person', normalized_name='test person'))
        db.commit()
    assert canonicalize_existing()['merged'] == 1
    with SessionLocal() as db:
        row = db.query(Conference).one()
        assert row.id == original_id
        assert not row.predicted
        assert row.submission_deadline == datetime(2026, 6, 12, 11, 59, 59)
        assert db.query(PCMember).one().conference_id == original_id


def test_round_agreement_ignores_order_and_equivalent_timezones():
    left = [{'round': 1, 'submission_deadline': '2026-06-11T23:59:59-12:00'},
            {'round': 2, 'submission_deadline': '2026-09-10'}]
    right = [{'submission_deadline': '2026-09-10', 'round': 2},
             {'submission_deadline': '2026-06-12T11:59:59Z', 'round': 1}]
    assert llm_extract._agree_full(left, right, 'rounds')
    right[0]['submission_deadline'] = '2026-09-11'
    assert not llm_extract._agree_full(left, right, 'rounds')


def test_round_conflict_keeps_agreed_submission_dates():
    first = [{'round': 1, 'submission_deadline': '2026-06-11'},
             {'round': 2, 'submission_deadline': '2026-09-10', 'notification_date': '2026-11-02'}]
    second = [{'round': 1, 'submission_deadline': '2026-06-11'},
              {'round': 2, 'submission_deadline': '2026-09-10', 'notification_date': '2026-11-09'}]
    merged, disputed = llm_extract._merge_round_dates(first, second)
    assert merged[1]['submission_deadline'] == '2026-09-10'
    assert 'notification_date' not in merged[1]
    assert disputed == ['rounds.2.notification_date']
