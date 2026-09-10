"""Create one real ECC update PR and verify repeat submissions reuse it."""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import httpx
from app import review_updates
from app.sources import llm_extract
from app.sources.official import _validated


def main():
    token = os.environ.get('CONFERENCE_FINDER_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise RuntimeError('GitHub token is not configured')
    repo = os.environ.get('CONFERENCE_FINDER_GITHUB_REPO', 'benjirer/conference-finder')
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    response = httpx.get(f'https://api.github.com/repos/{repo}', headers=headers, timeout=30)
    response.raise_for_status()
    print(json.dumps({'repository': response.json()['full_name'], 'access': 'verified'}), flush=True)
    url = 'https://ecc27.euca-ecc.org/call-for-papers/'
    page = llm_extract._fetch_page(url)
    if not page:
        raise RuntimeError('CFP could not be read')
    venue = llm_extract.extract_full_venue_page(page, ['ECC 2027'])
    dates = _validated(venue, 'ECC', 2027)
    first = next(entry for entry in dates if entry['round'] == 1)
    expected = {'submission_deadline': '2026-10-31', 'notification_date': '2027-03-05', 'camera_ready': '2027-04-05'}
    if any(first.get(field) != value for field, value in expected.items()):
        raise RuntimeError('Extraction did not match previously verified dates; no PR created')
    payload = {'venue': venue, 'url': url, 'diverged': venue.pop('_diverged', []),
               'submitted_at': datetime.now(timezone.utc).isoformat()}
    pr_url = review_updates.propose('user', 'ECC 2027', payload)
    repeated_url = review_updates.propose('user', 'ECC 2027', payload)
    number = pr_url.rsplit('/', 1)[-1]
    response = httpx.get(f'https://api.github.com/repos/{repo}/pulls/{number}', headers=headers, timeout=30)
    response.raise_for_status()
    pr = response.json()
    files_response = httpx.get(f'https://api.github.com/repos/{repo}/pulls/{number}/files', headers=headers, timeout=30)
    files_response.raise_for_status()
    files = [item['filename'] for item in files_response.json()]
    if len(files) != 1 or not files[0].startswith('backend/data/reviewed_updates/'):
        raise RuntimeError('Unexpected PR files; manual review required')
    print(json.dumps({'pr_url': pr_url, 'state': pr['state'], 'merged': pr['merged'],
        'repeated_submission_reused_pr': pr_url == repeated_url, 'files': files}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(json.dumps({'error': 'GitHub request failed', 'status': exc.response.status_code,
                          'message': exc.response.json().get('message', 'Request rejected')}))
        sys.exit(1)
