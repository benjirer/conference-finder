"""Small paid extraction accuracy check. Does not update venues or create PRs."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sources.llm_extract import _fetch_page, extract_full_venue_page
from app.sources.official import _validated
from app.sources._common import parse_iso_date

cases = [
 ('ECC', 2027, 'https://ecc27.euca-ecc.org/call-for-papers/',
  {'submission_deadline': '2026-10-31', 'notification_date': '2027-03-05', 'camera_ready': '2027-04-05'}),
 ('CGO', 2027, 'https://2027.cgo.org/track/cgo-2027-papers',
  {'submission_deadline': '2026-06-11', 'conference_start': '2027-03-20', 'conference_end': '2027-03-24'}),
 ('GCCE', 2026, 'https://www.ieee-gcce.org/2026/documents/GCCE2026_v01.pdf',
  {'submission_deadline': '2026-05-11', 'conference_start': '2026-10-26', 'conference_end': '2026-10-29'}),
]
if len(sys.argv) > 1:
 cases = [case for case in cases if case[0] == sys.argv[1]]
results = []
for acronym, year, url, expected in cases:
 try:
  page = _fetch_page(url)
  if not page:
   raise ValueError('Page fetch failed')
  result = extract_full_venue_page(page, [f'{acronym} {year}'])
  dates = _validated(result, acronym, year)
  first = next(item for item in dates if item['round'] == 1)
  comparisons = {field: {'expected': value, 'actual': first.get(field), 'match': first.get(field) == value} for field, value in expected.items()}
  if acronym == 'CGO':
   second = next(item for item in dates if item['round'] == 2)
   comparisons['round_2_submission'] = {'expected': '2026-09-10', 'actual': second.get('submission_deadline'), 'match': second.get('submission_deadline') == '2026-09-10'}
  results.append({'venue': acronym, 'url': url, 'comparisons': comparisons, 'rounds': dates, 'diverged': result.get('_diverged'), 'passed': all(item['match'] for item in comparisons.values())})
 except Exception as exc:
  results.append({'venue': acronym, 'url': url, 'error': str(exc)[:500], 'passed': False})
print(json.dumps(results, indent=2))
