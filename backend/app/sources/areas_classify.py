"""Heuristic areas classification.

Many venues come out of confsearch / klb2 / aideadlines with empty `areas`,
because those sources either don't categorize at all or use codes we couldn't
map cleanly. This step runs after the stats overlay and applies a name+full-name
keyword classifier to every row whose `areas` list is empty.

Rules are conservative — we only assign an area when there's a strong signal.
Falsely empty is better than falsely classified.
"""
from __future__ import annotations

import json
import re

from ..db import SessionLocal
from ..models import Conference

# Each tuple: (area, list of regex patterns). Patterns are matched against the
# lowercased acronym + name + parent_venue + notes blob.
_RULES: list[tuple[str, list[str]]] = [
    ("robotics", [
        r"\brobot", r"\bdrone", r"\bmanipulation\b", r"\bautonomous\b",
        r"\bgrasp", r"\blocomotion\b", r"\bhumanoid", r"\bicra\b", r"\biros\b",
        r"\bhri\b", r"\bcorl\b", r"\brss\b",
    ]),
    ("control", [
        r"\bcontrol\b", r"\bdecision\b", r"\badaptive\b", r"\bcybernetic",
        r"\bdynamic system", r"\boptimization\b", r"\bobserver\b", r"\bestimat",
        r"\bcdc\b", r"\bacc\b", r"\becc\b", r"\bl4dc\b", r"\bhscc\b",
    ]),
    ("ml", [
        r"\bneural\b", r"\blearning\b", r"\bmachine learning\b", r"\bdeep\b",
        r"\breinforcement\b", r"\binference\b", r"\bartificial intelligen",
        r"\bgenerative\b", r"\bllm\b", r"\bfoundation model", r"\bdata mining\b",
        r"\bcomputer vision\b", r"\bnatural language\b", r"\bnlp\b", r"\bcv\b",
        r"\bneurips\b", r"\bicml\b", r"\biclr\b", r"\baaai\b", r"\bijcai\b",
        r"\bcvpr\b", r"\biccv\b", r"\beccv\b", r"\bacl\b", r"\bemnlp\b",
    ]),
    ("networking", [
        r"\bnetwork", r"\bcommunicat", r"\bprotocol", r"\bwireless\b",
        r"\bcellular\b", r"\b5g\b", r"\b6g\b", r"\bsensor network",
        r"\binternet\b", r"\binfocom\b", r"\bsigcomm\b", r"\bnsdi\b",
        r"\bconext\b", r"\bimc\b", r"\bmobicom\b", r"\bmobisys\b",
        r"\bsensys\b", r"\bipsn\b", r"\bicnp\b", r"\bvtc\b", r"\bglobecom\b",
        r"\bwcnc\b", r"\bpimrc\b",
    ]),
    ("systems", [
        r"\boperating system", r"\bdistributed system", r"\bdatabase\b",
        r"\bstorage\b", r"\bcompiler\b", r"\bvirtualization\b", r"\bcloud\b",
        r"\bedge computing\b", r"\bcontainer\b", r"\bdebugging\b",
        r"\bsosp\b", r"\bosdi\b", r"\beurosys\b", r"\batc\b", r"\bmlsys\b",
        r"\bhpdc\b", r"\bppopp\b", r"\basplos\b", r"\bisca\b", r"\bmicro\b",
        r"\bhpca\b", r"\bsigmod\b", r"\bvldb\b", r"\bicde\b",
    ]),
    ("multimedia", [
        r"\bmultimedia\b", r"\baudio\b", r"\bvideo\b", r"\bimage processing\b",
        r"\bsignal processing\b", r"\bspeech\b", r"\bacm mm\b", r"\bicme\b",
        r"\bmmsys\b", r"\bicassp\b", r"\beusipco\b", r"\binterspeech\b",
    ]),
]


def _classify_blob(blob: str) -> list[str]:
    out: set[str] = set()
    for area, patterns in _RULES:
        for p in patterns:
            if re.search(p, blob):
                out.add(area)
                break
    return sorted(out)


def classify_missing_areas() -> dict[str, int]:
    """For each row with empty areas, infer them from name+acronym+context."""
    updated = 0
    skipped = 0
    with SessionLocal() as db:
        for row in db.query(Conference).all():
            existing = json.loads(row.areas or "[]")
            if existing:
                continue
            blob = " ".join(filter(None, [
                row.acronym or "",
                row.name or "",
                row.parent_venue or "",
                row.location or "",
                row.notes or "",
            ])).lower()
            inferred = _classify_blob(blob)
            if not inferred:
                skipped += 1
                continue
            row.areas = json.dumps(inferred)
            updated += 1
        db.commit()
    return {"updated": updated, "skipped_no_signal": skipped}
