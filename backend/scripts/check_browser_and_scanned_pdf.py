"""Exercise a real browser and document API using synthetic, non-venue fixtures."""
import io
import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import httpx
from app.sources import pages, llm_extract
from app import db
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject, EncodedStreamObject, DecodedStreamObject
from playwright.sync_api import sync_playwright

if '--app' in sys.argv:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get('CONFERENCE_FINDER_BROWSER_EXECUTABLE') or None)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.goto('http://127.0.0.1:8000/', wait_until='networkidle')
            page.get_by_role('button', name='Data status', exact=True).click()
            page.wait_for_function("document.querySelector('#data-status-summary').textContent.includes('tracked editions')")
            page.get_by_label('Filter venue checks').fill('CGO')
            page.wait_for_timeout(100)
            assert page.locator('#data-status-list').inner_text().count('CGO') >= 1
            page.get_by_role('button', name='Recheck this venue').first.click()
            assert page.locator('#add-url').input_value().startswith('http')
            print(json.dumps({'data_status_dialog': True, 'venue_filter': True, 'recheck_opens_prefilled_form': True}))
        finally:
            browser.close()
    sys.exit(0)

fixture = '''<html><body><div id="app"></div><script>
setTimeout(() => {document.getElementById('app').innerHTML =
'<h1>TESTCONF 2027</h1><h2>Paper submission: October 18, 2026</h2><p>Conference: March 4–6, 2027</p>';}, 100);
</script></body></html>'''
original_get = llm_extract._public_get
llm_extract._public_get = lambda url: httpx.Response(200, text=fixture,
    headers={'content-type':'text/html'}, request=httpx.Request('GET',url))
try:
    rendered = pages.rendered_html('https://fixture.example/cfp')
finally:
    llm_extract._public_get = original_get
browser_ok = '<h2>Paper submission: October 18, 2026</h2>' in rendered
print(json.dumps({'real_browser_rendering':browser_ok}),flush=True)
if not browser_ok:
    raise RuntimeError('JavaScript fixture failed to render')
with sync_playwright() as playwright:
    browser=playwright.chromium.launch(headless=True, executable_path=os.environ.get('CONFERENCE_FINDER_BROWSER_EXECUTABLE') or None)
    try:
        page=browser.new_page(viewport={'width':1000,'height':600}, device_scale_factor=1)
        page.set_content(rendered)
        jpeg=page.screenshot(type='jpeg',quality=95)
    finally:
        browser.close()
writer=PdfWriter()
page=writer.add_blank_page(width=750,height=450)
image=EncodedStreamObject()
image._data=jpeg
image.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Image'),
    NameObject('/Width'):NumberObject(1000),NameObject('/Height'):NumberObject(600),
    NameObject('/ColorSpace'):NameObject('/DeviceRGB'),NameObject('/BitsPerComponent'):NumberObject(8),
    NameObject('/Filter'):NameObject('/DCTDecode')})
page[NameObject('/Resources')]=DictionaryObject({NameObject('/XObject'):DictionaryObject({NameObject('/Im0'):writer._add_object(image)})})
stream=DecodedStreamObject();stream.set_data(b'q 750 0 0 450 0 0 cm /Im0 Do Q')
page[NameObject('/Contents')]=writer._add_object(stream)
out=io.BytesIO();writer.write(out)
assert not pages.pdf_text(out.getvalue()).strip()
with tempfile.TemporaryDirectory() as folder:
    db.DATA_DIR=Path(folder)
    transcript=pages.scanned_pdf_text(out.getvalue())
    repeated=pages.scanned_pdf_text(out.getvalue())
    ok='October 18, 2026' in transcript and '2027' in transcript
    print(json.dumps({'scanned_pdf_transcription':ok,'cached_repeat_identical':transcript==repeated}))
    if not ok:
        raise RuntimeError('Scanned PDF transcription did not preserve the fixture dates')
