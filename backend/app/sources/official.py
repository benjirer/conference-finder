"""Recheck official CFPs; only accept matching editions and agreed date fields.

State lives in SQLite on the configured runtime volume. Successful extractions
are replayed after aggregator ingestion, so unchanged official pages still win.
A failed fetch/extraction never advances the accepted hash or destroys dates.
"""
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from ..db import SessionLocal
from ..models import Conference, OfficialCheck, PendingReview
from . import _common, llm_extract

DATE_FIELDS = ("abstract_deadline", "submission_deadline", "notification_date",
               "camera_ready", "conference_start", "conference_end")


def _validated(result, acronym, year):
    if not isinstance(result, dict):
        raise ValueError("Extraction returned no usable result")
    disputed = set(result.get("_diverged") or [])
    if disputed.intersection({"acronym", "year"}):
        raise ValueError("Extraction disagreed on venue identity")
    if (_common.canonical_acronym(result.get("acronym") or "").casefold() != acronym.casefold()
            or result.get("year") != year):
        raise ValueError("Page identifies a different venue or edition")
    rounds = result.get("rounds")
    if rounds and "rounds" in disputed:
        raise ValueError("Extraction disagreed on submission rounds")
    entries = rounds if rounds else [{"round": 1, **result}]
    if not isinstance(entries, list):
        raise ValueError("Invalid submission rounds")
    withdrawn = result.get("withdrawn") or []
    if "withdrawn" in disputed:
        withdrawn = []
    accepted = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid submission round")
        idx = entry.get("round", 1)
        if type(idx) is not int or idx < 1 or idx in seen:
            raise ValueError("Invalid or duplicate submission round")
        seen.add(idx)
        clean = {"round": idx}
        for field in DATE_FIELDS:
            value = entry.get(field)
            if field in ("conference_start", "conference_end"):
                value = result.get(field)
            if field in withdrawn:
                clean[field] = None
                continue
            if value is None or field in disputed or f'rounds.{idx}.{field}' in disputed:
                continue
            date = _common.parse_iso_date(value)
            if date is None or not year - 1 <= date.year <= year + 1:
                raise ValueError(f"Invalid {field}")
            if field.startswith("conference_") and date.year != year:
                raise ValueError("Conference dates belong to another edition")
            clean[field] = date.date().isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)) else date.isoformat()
        for first, last in (("abstract_deadline", "submission_deadline"),
                            ("submission_deadline", "notification_date"),
                            ("conference_start", "conference_end")):
            if clean.get(first) and clean.get(last) and clean[first] > clean[last]:
                raise ValueError(f"Inconsistent {first} / {last}")
        if len(clean) > 1:
            accepted.append(clean)
    if not accepted:
        raise ValueError("No agreed dates for this edition")
    return accepted


def _apply(db, check):
    if not check.payload:
        return 0
    changed = 0
    expires = timedelta(days=float(os.environ.get('CONFERENCE_FINDER_OFFICIAL_MAX_AGE_DAYS', '14')))
    stale = bool(check.error) or not check.verified_at or _common.utc_now() - check.verified_at > expires
    template = db.query(Conference).filter_by(acronym=check.acronym, year=check.year, round=1).first()
    if template is None:
        return 0
    for entry in json.loads(check.payload):
        row = db.query(Conference).filter_by(acronym=check.acronym, year=check.year, round=entry['round']).first()
        if row is None:
            if stale:
                continue
            row = Conference(acronym=check.acronym, year=check.year, round=entry['round'],
                             name=template.name, areas=template.areas, website=template.website,
                             cfp_url=check.url, is_workshop=template.is_workshop,
                             parent_venue=template.parent_venue)
            db.add(row)
        metadata = json.loads(row.date_metadata or '{}')
        if stale:
            for field in DATE_FIELDS:
                if metadata.get(field, {}).get('source') == check.url:
                    metadata[field]['status'] = 'stale'
            row.date_metadata = json.dumps(metadata)
            continue
        _common.promote_prediction(row)
        for field in DATE_FIELDS:
            if field in entry:
                value = _common.parse_iso_date(entry[field])
                changed += getattr(row, field) != value
                setattr(row, field, value)
                metadata[field] = {'status': 'confirmed' if value else 'withdrawn',
                    'precision': 'date' if value and len(entry[field]) == 10 else 'datetime',
                    'source': check.url, 'checked_at': check.verified_at.isoformat() + 'Z'}
        row.date_metadata = json.dumps(metadata)
        row.source = 'official'
        row.last_verified = check.verified_at
        db.flush()
        record = _common.upsert_source_record(db, acronym=row.acronym, year=row.year,
            round=row.round, source='official', name=row.name, link=check.url,
            **{field: _common.parse_iso_date(entry.get(field)) for field in DATE_FIELDS
               if field != 'camera_ready'})
        if record is not None:
            record.fetched_at = check.verified_at
    return changed


def refresh_official():
    now = _common.utc_now()
    hours = max(0, float(os.environ.get('CONFERENCE_FINDER_OFFICIAL_HOURS', '24')))
    limit = max(0, int(os.environ.get('CONFERENCE_FINDER_OFFICIAL_LIMIT', '0')))
    extraction_limit = max(0, int(os.environ.get('CONFERENCE_FINDER_EXTRACTION_LIMIT', '100')))
    stats = dict(extracted=0, checked=0, unchanged=0, updated_fields=0, errors=0, pending=0)
    with SessionLocal() as db:
        rows = db.query(Conference).filter(Conference.year >= now.year).all()
        checks = []
        seen = set()
        for row in rows:
            url = row.cfp_url or row.website
            key = (row.acronym, row.year, url)
            if not url or key in seen:
                continue
            seen.add(key)
            check = db.query(OfficialCheck).filter_by(acronym=row.acronym, year=row.year, url=url).first()
            if check is None:
                check = OfficialCheck(acronym=row.acronym, year=row.year, url=url)
                db.add(check)
                db.flush()
            stats['updated_fields'] += _apply(db, check)
            checks.append((check, row.predicted or row.submission_deadline is None))
        db.commit()
        # Oldest check first prevents repeatedly failing tentative sites starving others.
        checks.sort(key=lambda pair: (pair[0].checked_at or now - timedelta(days=36500), not pair[1]))
        due = [check for check, _ in checks if not check.checked_at or now - check.checked_at >= timedelta(hours=hours)]
        selected = (due[:limit] if limit else due) if os.environ.get('CONFERENCE_FINDER_OFFICIAL_ENABLED', '1') != '0' else []
        stats['pending'] = len(due) - len(selected)
        def fetch(item):
            url, year = item
            try:
                page = llm_extract._fetch_page(url)
                if page:
                    return page, None, url
                from .pages import edition_candidates
                for candidate in edition_candidates(url, year):
                    page = llm_extract._fetch_page(candidate)
                    if page and str(year) in page:
                        return page, None, candidate
                return None, None, url
            except Exception as exc:
                return None, str(exc), url
        workers = max(1, min(16, int(os.environ.get('CONFERENCE_FINDER_FETCH_WORKERS', '6'))))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fetched = dict(zip([check.id for check in selected], pool.map(fetch, [(check.url, check.year) for check in selected])))
        for check in selected:
            if check.checked_at and now - check.checked_at < timedelta(hours=hours):
                continue
            stats['checked'] += 1
            check.checked_at = now
            # Release SQLite's write lock before network/model calls.
            db.commit()
            try:
                page, fetch_error, fetched_url = fetched[check.id]
                if not page:
                    raise ValueError(fetch_error or 'Official page could not be read')
                digest = hashlib.sha256(page.encode()).hexdigest()
                pending_key = f'{check.acronym}:{check.year}'
                pending = db.get(PendingReview, pending_key)
                if pending and pending.content_hash == digest and digest != check.content_hash:
                    check.error = 'Pending review: ' + pending.review_url
                    db.commit()
                    continue
                if digest == check.content_hash and check.payload:
                    check.error = None
                    check.verified_at = now
                    stats['unchanged'] += 1
                else:
                    if extraction_limit and stats['extracted'] >= extraction_limit:
                        check.checked_at = None
                        stats['pending'] += 1
                        db.commit()
                        continue
                    if not llm_extract.ANTHROPIC_KEY:
                        raise ValueError('ANTHROPIC_API_KEY required for changed official pages')
                    stats['extracted'] += 1
                    result = llm_extract.extract_full_venue_page(page, [
                        f"Extract only {check.acronym} {check.year}; return null identity if that edition is absent."])
                    try:
                        payload = _validated(result, check.acronym, check.year)
                    except ValueError as original:
                        # Only edition mismatch warrants discovering a replacement URL.
                        if result and result.get('year') == check.year:
                            raise
                        from .pages import edition_candidates
                        found = False
                        for candidate in edition_candidates(check.url, check.year):
                            candidate_page = llm_extract._fetch_page(candidate)
                            if not candidate_page:
                                continue
                            if extraction_limit and stats['extracted'] >= extraction_limit:
                                break
                            stats['extracted'] += 1
                            candidate_result = llm_extract.extract_full_venue_page(candidate_page, [f'{check.acronym} {check.year}'])
                            try:
                                payload = _validated(candidate_result, check.acronym, check.year)
                            except ValueError:
                                continue
                            result = candidate_result
                            fetched_url = candidate
                            digest = hashlib.sha256(candidate_page.encode()).hexdigest()
                            found = True
                            break
                        else:
                            raise original
                        if not found:
                            raise original
                    from .. import review_updates
                    if review_updates.enabled() and check.payload != json.dumps(payload):
                        review_url = review_updates.propose('official', f'{check.acronym} {check.year}', {
                            'acronym': check.acronym, 'year': check.year, 'url': fetched_url,
                            'dates': payload, 'verified_at': now.isoformat(), 'content_hash': digest,
                            'warnings': result.get('_diverged', [])})
                        db.merge(PendingReview(key=pending_key, content_hash=digest, review_url=review_url))
                        check.error = 'Pending review: ' + review_url
                        db.commit()
                        continue
                    if fetched_url != check.url:
                        for row in db.query(Conference).filter_by(acronym=check.acronym, year=check.year):
                            row.cfp_url = fetched_url
                        check.url = fetched_url
                    check.payload = json.dumps(payload)
                    check.content_hash = digest
                    check.verified_at = now
                    check.error = None
                stats['updated_fields'] += _apply(db, check)
            except Exception as exc:
                check.error = str(exc)[:1000]
                stats['errors'] += 1
                _apply(db, check)  # Mark the affected field metadata stale immediately.
            db.commit()
        db.commit()
    return stats
