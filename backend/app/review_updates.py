"""Propose immutable, content-addressed data updates through GitHub PRs.

Each changed venue snapshot gets its own file, avoiding conflicting edits to
one shared YAML file. No auto-merge. Repeated submissions reuse the same PR.
"""
import base64
import hashlib
import json
import os
from pathlib import Path

import httpx


def enabled():
    return bool(os.environ.get('CONFERENCE_FINDER_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN'))


def propose(kind, identity, payload):
    token = os.environ.get('CONFERENCE_FINDER_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise RuntimeError('GitHub token is missing; update has not been saved to GitHub')
    repo = os.environ.get('CONFERENCE_FINDER_GITHUB_REPO', 'benjirer/conference-finder')
    content = json.dumps({'kind': kind, 'identity': identity, 'payload': payload}, sort_keys=True, indent=2)
    stable = {**payload}
    stable.pop('verified_at', None)
    stable.pop('submitted_at', None)
    digest = hashlib.sha256(json.dumps({'kind': kind, 'identity': identity, 'payload': stable}, sort_keys=True).encode()).hexdigest()[:20]
    branch = f'codex/venue-update-{digest}'
    path = f'backend/data/reviewed_updates/{digest}.json'
    with httpx.Client(base_url=f'https://api.github.com/repos/{repo}/', timeout=30,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}) as client:
        def request(method, endpoint, **kwargs):
            target = endpoint if endpoint else str(client.base_url).rstrip('/')
            response = client.request(method, target, **kwargs)
            response.raise_for_status()
            return response.json()
        metadata = request('GET', '')
        base = metadata['default_branch']
        pulls = request('GET', 'pulls', params={'head': f'{repo.split("/")[0]}:{branch}', 'state': 'all'})
        if pulls:
            return pulls[0]['html_url']
        ref = client.get(f'git/ref/heads/{branch}')
        if ref.status_code == 404:
            sha = request('GET', f'git/ref/heads/{base}')['object']['sha']
            request('POST', 'git/refs', json={'ref': f'refs/heads/{branch}', 'sha': sha})
        else:
            ref.raise_for_status()
        existing = client.get(f'contents/{path}', params={'ref': branch})
        if existing.status_code == 404:
            request('PUT', f'contents/{path}', json={'message': f'Propose {identity} data update',
                'content': base64.b64encode(content.encode()).decode(), 'branch': branch})
        else:
            existing.raise_for_status()
        pr = request('POST', 'pulls', json={'title': f'Venue update: {identity}', 'head': branch,
            'base': base, 'body': 'Review the venue snapshot and source URL in the changed JSON file. Merge to approve it for the next deployment. No automatic merge is performed.'})
        return pr['html_url']


def approved_updates():
    from .db import BUNDLED_DATA_DIR
    for path in sorted((BUNDLED_DATA_DIR / 'reviewed_updates').glob('*.json')):
        try:
            yield json.loads(path.read_text())
        except (ValueError, OSError):
            continue


def import_approved():
    from .db import SessionLocal
    from .models import OfficialCheck, Conference
    from .sources import user_venues, _common
    count = 0
    updates = sorted(approved_updates(), key=lambda item: item.get('payload', {}).get('verified_at') or item.get('payload', {}).get('submitted_at', ''))
    for item in updates:
        payload = item.get('payload', {})
        if item.get('kind') == 'user':
            user_venues.append_and_upsert(payload['venue'], payload['url'], payload.get('diverged', []))
            count += 1
        elif item.get('kind') == 'official':
            with SessionLocal() as db:
                check = db.query(OfficialCheck).filter_by(acronym=payload['acronym'], year=payload['year'], url=payload['url']).first()
                if check is None:
                    check = OfficialCheck(acronym=payload['acronym'], year=payload['year'], url=payload['url'])
                    db.add(check)
                verified = _common.parse_iso_date(payload['verified_at'])
                if check.verified_at is None or check.verified_at < verified:
                    check.payload = json.dumps(payload['dates'])
                    check.verified_at = verified
                    check.content_hash = payload['content_hash']
                    check.error = None
                    for row in db.query(Conference).filter_by(acronym=payload['acronym'], year=payload['year']):
                        row.cfp_url = payload['url']
                db.commit()
                count += 1
    return {'imported': count}
