"""Inspect deployment prerequisites without printing credentials."""
import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import httpx
repo = os.environ.get('CONFERENCE_FINDER_GITHUB_REPO', 'benjirer/conference-finder')
token = os.environ.get('CONFERENCE_FINDER_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN')
with httpx.Client(base_url=f'https://api.github.com/repos/{repo}/', headers={
    'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}, timeout=30) as client:
 for path in ['actions/permissions/workflow', 'actions/secrets/public-key', 'commits/main/status', 'deployments?per_page=3', 'actions/workflows']:
  response=client.get(path)
  data=response.json()
  if path=='actions/secrets/public-key' and response.is_success:
   data={'available': True}
  if path=='commits/main/status' and response.is_success:
   data={'state':data['state'], 'statuses':[{k:s.get(k) for k in ['context','state','target_url','description']} for s in data['statuses']]}
  if path.startswith('deployments?') and response.is_success:
   data=[{'id':item['id'],'sha':item['sha'],'environment':item['environment'], 'statuses':[ {k:status.get(k) for k in ['state','environment_url','target_url','description']} for status in client.get(f"deployments/{item['id']}/statuses").json()]} for item in data[:1]]
  print(json.dumps({'endpoint':path,'status':response.status_code,'result':data}),flush=True)
print('Render credential configured:',bool(os.environ.get('RENDER_API_KEY')))
