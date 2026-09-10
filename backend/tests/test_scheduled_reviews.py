"""Exercise repeated checks, replacement, and PR lifecycle through a fake GitHub."""
import base64
import copy
import json

import httpx
import pytest

from app import review_updates


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'test-token')
    monkeypatch.setenv('CONFERENCE_FINDER_GITHUB_REPO', 'owner/repo')
    state = {'branches': {'main': {}}, 'pulls': [], 'writes': []}

    def handler(request):
        path = request.url.path.removeprefix('/repos/owner/repo').strip('/')
        body = json.loads(request.content) if request.content else {}
        if request.method != 'GET':
            state['writes'].append((request.method, path))
        if path == '' and request.method == 'GET':
            return httpx.Response(200, json={'default_branch': 'main'})
        if path == 'pulls' and request.method == 'GET':
            return httpx.Response(200, json=[p for p in state['pulls'] if p['state'] == 'open'])
        if path == 'git/ref/heads/main':
            return httpx.Response(200, json={'object': {'sha': 'main-sha'}})
        if path == 'git/refs' and request.method == 'POST':
            branch = body['ref'].removeprefix('refs/heads/')
            assert branch not in state['branches']
            assert body['sha'] == 'main-sha'
            state['branches'][branch] = copy.deepcopy(state['branches']['main'])
            return httpx.Response(201, json={})
        if path.startswith('contents/'):
            filename = path.removeprefix('contents/')
            branch = body.get('branch') or request.url.params['ref']
            files = state['branches'][branch]
            if request.method == 'GET':
                return httpx.Response(200, json=files[filename]) if filename in files else httpx.Response(404)
            assert request.method == 'PUT'
            if filename in files:
                assert body['sha'] == files[filename]['sha']
            files[filename] = {'content': body['content'], 'sha': str(len(state['writes']))}
            return httpx.Response(200, json={})
        if path == 'pulls' and request.method == 'POST':
            number = len(state['pulls']) + 1
            pr = {'number': number, 'html_url': f'https://github.com/owner/repo/pull/{number}',
                  'head': {'ref': body['head'], 'repo': {'full_name': 'owner/repo'}},
                  'state': 'open', 'body': body['body']}
            state['pulls'].append(pr)
            return httpx.Response(201, json=pr)
        if path.startswith('pulls/') and request.method == 'PATCH':
            pr = state['pulls'][int(path.split('/')[-1]) - 1]
            assert pr['state'] == 'open'
            pr.update(body)
            return httpx.Response(200, json=pr)
        raise AssertionError(f'{request.method} {request.url}')

    real_client = httpx.Client
    monkeypatch.setattr(review_updates.httpx, 'Client', lambda **kwargs:
        real_client(transport=httpx.MockTransport(handler), **kwargs))
    return state


def propose(acronym, date='2027-02-01'):
    return review_updates.propose('official', f'{acronym} 2027', {
        'acronym': acronym, 'year': 2027, 'url': 'https://example.com/cfp',
        'verified_at': '2026-09-10T12:00:00', 'content_hash': date,
        'dates': [{'round': 1, 'submission_deadline': date}],
        'previous_dates': [{'round': 1, 'submission_deadline': '2027-01-01'}]})


def files_for(github, pr):
    return {name: base64.b64decode(file['content']).decode()
        for name, file in github['branches'][pr['head']['ref']].items()}


def test_multiple_checks_and_retries_share_one_pr(github):
    first = propose('AAA')
    assert propose('BBB') == first
    assert propose('AAA', '2027-03-01') == first
    assert propose('AAA', '2027-03-01') == first
    assert len(github['pulls']) == 1
    pr = github['pulls'][0]
    files = files_for(github, pr)
    snapshots = [json.loads(v) for k, v in files.items() if k.endswith('.json')]
    assert len(snapshots) == 2
    aaa = next(s for s in snapshots if s['payload']['acronym'] == 'AAA')
    assert aaa['payload']['dates'][0]['submission_deadline'] == '2027-03-01'
    assert pr['body'].count('| AAA 2027') == 1
    assert '| BBB 2027' in pr['body']
    assert '2027-01-01 → 2027-03-01' in pr['body']
    assert '[CFP](<https://example.com/cfp>)' in pr['body']
    assert '2027-02-01' not in next(line for line in pr['body'].splitlines() if '| AAA' in line)
    assert not github['branches']['main']  # No canonical writes or merges.


@pytest.mark.parametrize('merged', [False, True])
def test_closed_pr_starts_fresh_branch_and_summary(github, merged):
    first = propose('AAA')
    pr = github['pulls'][0]
    pr['state'] = 'closed'
    if merged:
        github['branches']['main'] = copy.deepcopy(github['branches'][pr['head']['ref']])
    second = propose('BBB')
    assert second != first
    new_pr = github['pulls'][1]
    assert new_pr['head']['ref'] != pr['head']['ref']
    assert '| AAA 2027' not in new_pr['body']
    assert '| BBB 2027' in new_pr['body']
    assert '| BBB 2027' not in pr['body']
    files = files_for(github, new_pr)
    assert len([name for name in files if name.endswith('.json')]) == (2 if merged else 1)


def test_large_summary_stays_available_without_exceeding_pr_limit(github):
    propose('AAA')
    pr = github['pulls'][0]
    files = github['branches'][pr['head']['ref']]
    full = review_updates.SUMMARY_HEADER + '| Example | Some date change | CFP |\n' * 2500
    files[review_updates.SUMMARY_PATH]['content'] = base64.b64encode(full.encode()).decode()
    propose('BBB')
    assert len(pr['body']) < 65536
    assert 'Full review summary' in pr['body']
    assert 'Additional venues' in pr['body']
    assert '| BBB 2027' in files_for(github, pr)[review_updates.SUMMARY_PATH]
