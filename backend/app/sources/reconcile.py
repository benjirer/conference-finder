"""Cross-source reconciliation: detect date disagreements across aggregators.

For each (acronym, year) with two or more SourceRecord rows, compare
`submission_deadline` (the action-driving field). If any two sources differ
by more than `TOLERANCE_DAYS`, set the canonical Conference row's
`diverged=True` and store per-source values in `diverged_detail` as JSON.

The conferences row itself is NOT modified beyond the flag — the canonical
values still come from the highest-priority writer (ccfddl/seed/user_added),
which already wrote during the earlier refresh steps.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Conference, SourceRecord

TOLERANCE_DAYS = 1


def _norm(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d") if dt else None


def reconcile() -> dict[str, int]:
    flagged = 0
    cleared = 0
    examined = 0

    with SessionLocal() as db:
        keys = db.execute(
            select(SourceRecord.acronym, SourceRecord.year).distinct()
        ).all()
        for acronym, year in keys:
            examined += 1
            records = (
                db.query(SourceRecord)
                .filter_by(acronym=acronym, year=year)
                .all()
            )
            with_deadlines = [r for r in records if r.submission_deadline is not None]
            disagreements: list[dict] = []
            if len(with_deadlines) >= 2:
                # Pairwise check: any two differ by > TOLERANCE_DAYS?
                base = with_deadlines[0]
                for other in with_deadlines[1:]:
                    delta = abs(
                        (base.submission_deadline - other.submission_deadline).days
                    )
                    if delta > TOLERANCE_DAYS:
                        disagreements.append({
                            "field": "submission_deadline",
                            "sources": [
                                {"source": base.source, "value": _norm(base.submission_deadline)},
                                {"source": other.source, "value": _norm(other.submission_deadline)},
                            ],
                            "diff_days": delta,
                        })
            conf = (
                db.query(Conference)
                .filter_by(acronym=acronym, year=year)
                .one_or_none()
            )
            if conf is None:
                continue
            if disagreements:
                # Augment with the full per-source snapshot for the tooltip.
                per_source = [
                    {
                        "source": r.source,
                        "submission_deadline": _norm(r.submission_deadline),
                        "abstract_deadline": _norm(r.abstract_deadline),
                        "notification_date": _norm(r.notification_date),
                        "link": r.link,
                    }
                    for r in records
                ]
                if not conf.diverged:
                    flagged += 1
                conf.diverged = True
                conf.diverged_detail = json.dumps({
                    "disagreements": disagreements,
                    "per_source": per_source,
                })
            else:
                if conf.diverged_detail or conf.diverged:
                    cleared += 1
                # Only clear if there are 2+ sources and they agree — single-source
                # rows shouldn't be auto-cleared since `diverged` might have been
                # set by the user-add flow.
                if len(with_deadlines) >= 2:
                    conf.diverged = False
                    conf.diverged_detail = None
        db.commit()
    return {"examined": examined, "flagged": flagged, "cleared": cleared}
