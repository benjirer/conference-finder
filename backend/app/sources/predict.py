"""Generate next-year predicted entries for venues that don't yet have a CFP for it.

Heuristic: take the most recent year's row for each acronym, clone it, advance
the year by one, and shift each date field forward by ~365 days. Mark
`predicted=True` and `source="predicted"`. Skip if a row already exists for the
target year (whatever its source).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Conference

_DATE_FIELDS = (
    "abstract_deadline", "submission_deadline", "notification_date",
    "camera_ready", "conference_start", "conference_end",
)


def predict_next_year() -> dict[str, int]:
    """Synthesize one predicted entry per acronym for `current_year + 1`.

    Only runs if the acronym has at least one non-predicted row with at least
    one real date — otherwise we'd be inventing dates with nothing to base them
    on, which is worse than no entry.
    """
    added = 0
    skipped = 0
    skipped_nodates = 0
    now = datetime.utcnow()
    target_year = now.year + 1
    with SessionLocal() as db:
        acronyms = [r[0] for r in db.execute(select(Conference.acronym).distinct()).all()]
        for acronym in acronyms:
            rows = (
                db.query(Conference)
                .filter(Conference.acronym == acronym)
                .order_by(Conference.year.desc())
                .all()
            )
            if not rows:
                continue
            # Skip if any row already exists for target_year (real or predicted).
            if any(r.year == target_year for r in rows):
                skipped += 1
                continue
            # Template = most recent non-predicted row WITH at least one date.
            template = next(
                (r for r in rows
                 if not r.predicted
                 and (r.submission_deadline or r.conference_start
                      or r.abstract_deadline or r.notification_date)),
                None,
            )
            if template is None:
                skipped_nodates += 1
                continue
            offset_days = 365 * (target_year - template.year)

            new = Conference(
                acronym=acronym,
                year=target_year,
                name=template.name,
                areas=template.areas,
                topics=template.topics,
                is_workshop=template.is_workshop,
                parent_venue=template.parent_venue,
                timezone=template.timezone,
                page_limit=template.page_limit,
                format_notes=template.format_notes,
                h5_index=template.h5_index,
                acceptance_rate=template.acceptance_rate,
                tier=template.tier,
                location=None,  # location changes each edition — don't carry over
                website=template.website,
                cfp_url=template.cfp_url,
                source="predicted",
                last_verified=now,
                predicted=True,
                notes=(template.notes or "") + "\nDates predicted by shifting "
                      f"{template.year} dates forward {offset_days} days."
            )
            for f in _DATE_FIELDS:
                v = getattr(template, f)
                if v is not None:
                    setattr(new, f, v + timedelta(days=offset_days))
            db.add(new)
            added += 1
        db.commit()
    return {"added": added, "skipped_existing": skipped, "skipped_no_dates": skipped_nodates}
