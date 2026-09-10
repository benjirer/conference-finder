import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import httpx
repo = os.environ.get('CONFERENCE_FINDER_GITHUB_REPO', 'benjirer/conference-finder')
token = os.environ.get('CONFERENCE_FINDER_GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN')
with httpx.Client(headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}, timeout=30) as client:
 for suffix in ['', '/', '/pulls?state=open&per_page=1']:
  r = client.get(f'https://api.github.com/repos/{repo}{suffix}')
  data = r.json()
  print(json.dumps({'endpoint': suffix or 'repository', 'status': r.status_code,
   'message': data.get('message') if isinstance(data, dict) else None,
   'permissions': data.get('permissions') if isinstance(data, dict) else None,
   'default_branch': data.get('default_branch') if isinstance(data, dict) else None}))
