import io
import json
from datetime import datetime, timedelta

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.sources import pages, llm_extract, official
from app import review_updates


def test_pdf_text_extraction():
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 50 700 Td (TEST 2027 Paper submission deadline: February 1, 2027) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    content = io.BytesIO()
    writer.write(content)
    response = httpx.Response(200, content=content.getvalue(), request=httpx.Request('GET', 'https://example.com/cfp.pdf'))
    text, _ = pages.read_response(response)
    assert 'February 1, 2027' in text


def test_javascript_shell_uses_renderer(monkeypatch):
    monkeypatch.setattr(pages, 'rendered_html', lambda url: '<main>TEST 2027 Submission deadline February 1, 2027</main>')
    response = httpx.Response(200, text='<div id="app"></div><script src="app.js"></script>', request=httpx.Request('GET', 'https://example.com'))
    assert 'February 1' in pages.read_response(response)[0]


def test_discovers_new_edition_link_and_url_variant(monkeypatch):
    monkeypatch.setattr(llm_extract, '_public_get', lambda url: httpx.Response(200,
        text='<a href="https://new.example/2027/">2027 edition</a>', request=httpx.Request('GET', url)))
    urls = pages.edition_candidates('https://old.example/2026/', 2027)
    assert 'https://new.example/2027/' in urls
    assert 'https://old.example/2027/' in urls


def test_stale_cache_does_not_overwrite_newer_aggregator(temp_db):
    from app.db import init_db, SessionLocal
    from app.models import Conference, OfficialCheck
    init_db()
    year = datetime.now().year
    with SessionLocal() as db:
        row = Conference(acronym='TEST', year=year, name='Test', submission_deadline=datetime(year, 3, 1))
        db.add(row)
        check = OfficialCheck(acronym='TEST', year=year, url='https://example.com',
            verified_at=datetime.now() - timedelta(days=30), payload=json.dumps([{'round': 1, 'submission_deadline': f'{year}-02-01'}]))
        db.add(check)
        db.flush()
        assert official._apply(db, check) == 0
        assert row.submission_deadline == datetime(year, 3, 1)


def test_withdrawal_and_date_only_precision(temp_db):
    from app.db import init_db, SessionLocal
    from app.models import Conference, OfficialCheck
    from app.main import _serialize
    from app.ical import _deadline_events
    init_db()
    year = datetime.now().year
    validated = official._validated({'acronym': 'TEST', 'year': year,
        'withdrawn': ['submission_deadline'], 'notification_date': f'{year}-04-01'}, 'TEST', year)
    with SessionLocal() as db:
        row = Conference(acronym='TEST', year=year, name='Test', submission_deadline=datetime(year, 3, 1))
        db.add(row)
        check = OfficialCheck(acronym='TEST', year=year, url='https://example.com', verified_at=datetime.now(), payload=json.dumps(validated))
        db.add(check)
        db.flush()
        official._apply(db, check)
        assert row.submission_deadline is None
        assert _serialize(row)['notification_date'] == f'{year}-04-01'
        assert 'final hour' not in '\n'.join(_deadline_events(row, row.notification_date, 'Notification'))


def test_pr_is_idempotent_and_does_not_merge(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith('/conference-finder'):
            return httpx.Response(200, json={'default_branch': 'main'})
        if request.url.path.endswith('/pulls') and request.method == 'GET':
            return httpx.Response(200, json=[{'html_url': 'https://github.com/benjirer/conference-finder/pull/1'}])
        raise AssertionError(str(request.url))
    real_client = httpx.Client
    monkeypatch.setattr(review_updates.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert review_updates.propose('user', 'TEST 2027', {'venue': {}}).endswith('/1')
    assert all(method == 'GET' for method, _ in calls)


def test_add_venue_returns_pending_without_mutation(client, monkeypatch):
    from app.models import Conference
    from app.db import SessionLocal
    monkeypatch.setattr(review_updates, 'enabled', lambda: True)
    monkeypatch.setattr(review_updates, 'propose', lambda *args: 'https://github.com/example/repo/pull/1')
    monkeypatch.setattr(llm_extract, 'extract_full_venue', lambda *args: {'acronym': 'TEST', 'year': 2027})
    response = client.post('/api/venues', json={'url': 'https://example.com/cfp'})
    assert response.json()['status'] == 'pending_review'
    with SessionLocal() as db:
        assert db.query(Conference).count() == 0


def test_new_pr_creates_branch_and_snapshot_without_merging(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    writes = []
    def handler(request):
        path = request.url.path
        if request.method == 'GET':
            if path.endswith('/conference-finder'):
                return httpx.Response(200, json={'default_branch': 'main'})
            if path.endswith('/pulls'):
                return httpx.Response(200, json=[])
            if path.endswith('/git/ref/heads/main'):
                return httpx.Response(200, json={'object': {'sha': 'base-sha'}})
            return httpx.Response(404, json={})
        body = json.loads(request.content)
        writes.append((request.method, path, body))
        if path.endswith('/pulls'):
            return httpx.Response(201, json={'html_url': 'https://github.com/benjirer/conference-finder/pull/2'})
        return httpx.Response(201, json={})
    real_client = httpx.Client
    monkeypatch.setattr(review_updates.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    assert review_updates.propose('user', 'TEST 2027', {'venue': {'acronym': 'TEST'}}).endswith('/2')
    assert [method for method, _, _ in writes] == ['POST', 'PUT', 'POST']
    assert writes[0][2]['sha'] == 'base-sha'
    assert writes[-1][2]['base'] == 'main'


def test_failed_link_does_not_discard_main_page(monkeypatch):
    responses = {'https://example.com': '<main>TEST 2027 deadline May 1, 2027 <a href="/cfp">CFP</a></main>',
                 'https://example.com/cfp': '<script src="x.js"></script>'}
    monkeypatch.setattr(llm_extract, '_public_get', lambda url: httpx.Response(200, text=responses[url], request=httpx.Request('GET', url)))
    def broken_browser(url):
        raise RuntimeError('browser unavailable')
    monkeypatch.setattr(pages, 'rendered_html', broken_browser)
    assert 'May 1, 2027' in pages.fetch_page('https://example.com')


def test_ephemeral_production_requires_review_storage(client, monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    monkeypatch.setattr(review_updates, 'enabled', lambda: False)
    response = client.post('/api/venues', json={'url': 'https://example.com/cfp'})
    assert response.status_code == 503
    assert 'CONFERENCE_FINDER_GITHUB_TOKEN' in response.json()['detail']
