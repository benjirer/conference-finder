"""Review official updates in one rolling PR; user additions get individual PRs."""
import base64
import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

SCHEDULED_BRANCH_PREFIX = 'codex/scheduled-venue-updates-'
SUMMARY_PATH = 'backend/data/scheduled_review.md'
SUMMARY_HEADER = ('Scheduled website checks update this PR until it is merged or closed. '
    'Merge to approve the snapshots for the next deployment. '
    'Previous dates may be estimated or unverified.\n\n'
    '| Venue | Date changes (previous → proposed) | Source |\n'
    '| --- | --- | --- |\n')


def _cell(value):
    return html.escape(str(value)).replace('|', '&#124;').replace('\n', ' ').replace('\r', ' ')


def _summary_row(identity, payload, key):
    previous = {entry['round']: entry for entry in payload.get('previous_dates', [])}
    changes = []
    for entry in payload['dates']:
        old = previous.get(entry['round'], {})
        for field, value in entry.items():
            if field == 'round':
                continue
            before = old.get(field)
            if value == before:
                continue
            changes.append(f"R{entry['round']} {_cell(field.replace('_', ' '))}: "
                f"{_cell(before or 'unknown')} → {_cell(value if value is not None else 'withdrawn')}")
    details = '<br>'.join(changes) or 'Confirm existing dates'
    if payload.get('warnings'):
        details += '<br>Extraction warnings: ' + _cell(', '.join(payload['warnings']))
    source = quote(payload['url'], safe=':/?#=&%+-._~')
    return f'| {_cell(identity)} <!-- {key} --> | {details} | [CFP](<{source}>) |\n'


def _propose_scheduled(client, request, repo, base, identity, payload, content):
    # Discover the open PR through GitHub, so this survives cache loss and restarts.
    # Workflow concurrency serializes scheduled writers.
    current = None
    page = 1
    while True:
        pulls = request('GET', 'pulls', params={'state': 'open', 'base': base,
            'per_page': 100, 'page': page})
        current = next((pr for pr in pulls
            if pr['head']['ref'].startswith(SCHEDULED_BRANCH_PREFIX)
            and (pr['head'].get('repo') or {}).get('full_name') == repo), None)
        if current or len(pulls) < 100:
            break
        page += 1
    branch = current['head']['ref'] if current else SCHEDULED_BRANCH_PREFIX + uuid4().hex[:12]
    if current is None:
        sha = request('GET', f'git/ref/heads/{base}')['object']['sha']
        request('POST', 'git/refs', json={'ref': f'refs/heads/{branch}', 'sha': sha})

    def read_file(path):
        response = client.get(f'contents/{path}', params={'ref': branch})
        if response.status_code == 404:
            return None, None
        response.raise_for_status()
        data = response.json()
        return base64.b64decode(data['content']).decode(), data['sha']

    def write_file(path, text, previous, sha):
        if text == previous:
            return
        body = {'message': f'Update scheduled review: {identity}', 'branch': branch,
            'content': base64.b64encode(text.encode()).decode()}
        if sha:
            body['sha'] = sha
        request('PUT', f'contents/{path}', json=body)

    # One file per edition: another finding supersedes its pending snapshot.
    key = hashlib.sha256(json.dumps([payload['acronym'], payload['year']]).encode()).hexdigest()[:20]
    path = f'backend/data/reviewed_updates/scheduled-{key}.json'
    old, sha = read_file(path)
    write_file(path, content, old, sha)
    old_summary, summary_sha = read_file(SUMMARY_PATH)
    summary = old_summary if current and old_summary else SUMMARY_HEADER
    marker = f'<!-- {key} -->'
    rows = [line for line in summary.splitlines(keepends=True) if marker not in line]
    summary = ''.join(rows) + _summary_row(identity, payload, key)
    write_file(SUMMARY_PATH, summary, old_summary, summary_sha)
    # Keep the full table in a reviewable file even for catalogues that exceed
    # GitHub's PR body limit. Never truncate a table row midway.
    link = f'https://github.com/{repo}/blob/{branch}/{SUMMARY_PATH}'
    footer = f'\n[Full review summary]({link}). Each venue snapshot is in Files changed.\n'
    body = ''
    for line in summary.splitlines(keepends=True):
        if len(body) + len(line) > 55000:
            body += '\nAdditional venues are listed in the full review summary.\n'
            break
        body += line
    body += footer
    if current:
        request('PATCH', f"pulls/{current['number']}", json={'body': body})
        return current['html_url']
    pr = request('POST', 'pulls', json={'title': 'Review scheduled venue updates',
        'head': branch, 'base': base, 'body': body})
    return pr['html_url']


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
        if kind == 'official':
            return _propose_scheduled(client, request, repo, base, identity, payload, content)
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
