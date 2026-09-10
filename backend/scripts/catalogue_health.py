"""Inspect source discovery and the local rows used for deployment verification."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.sources import _common
response = _common.http_get('https://api.github.com/repos/ccfddl/ccf-deadlines/git/trees/main?recursive=1')
if response is not None:
 paths=[item.get('path','') for item in response.json().get('tree',[])]
 print('Categories:', sorted({path.split('/')[1] for path in paths if path.startswith('conference/') and len(path.split('/'))==3}))
 print('CGO paths:',[path for path in paths if 'cgo' in path.lower()])
