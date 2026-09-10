"""Read-only live retrieval smoke test; writes no venue data or PRs."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sources.llm_extract import _fetch_page
from app.sources.pages import edition_candidates

CASES = {
 'CDC 2026 HTML': 'https://cdc2026.ieeecss.org/',
 'ECC 2027 HTML': 'https://ecc27.euca-ecc.org/call-for-papers/',
 'ECCE 2026 PDF': 'https://www.ieee-ecce.org/2026/wp-content/uploads/sites/24/2025/09/ECCE2026_CallForPapersFINAL-1-.pdf',
 'GCCE 2026 PDF': 'https://www.ieee-gcce.org/2026/documents/GCCE2026_v01.pdf',
 'L4DC 2026 HTML': 'https://sites.google.com/usc.edu/l4dc2026',
}
results = []
for name, url in CASES.items():
 try:
  page = _fetch_page(url)
  results.append({'case': name, 'url': url, 'characters': len(page or ''),
                  'contains_year': '2026' in (page or '') or '2027' in (page or ''),
                  'contains_deadline_word': any(w in (page or '').lower() for w in ['deadline','submission','digest']),
                  'status': 'readable' if page else 'failed'})
 except Exception as exc:
  results.append({'case': name, 'url': url, 'status': 'failed', 'error': str(exc)[:300]})
print(json.dumps(results, indent=2))
