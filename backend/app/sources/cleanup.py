"""Drop stale rows whose year is below the current cutoff.

Aggregators ship historical years (aideadlines / ds-deadlines / klb2 all keep
~5 years of history). On ingest we already skip old years going forward, but
existing rows in the DB from prior runs need to be pruned too. Runs after all
ingesters so nothing it deletes will be re-added in the same pass.
"""
from __future__ import annotations

from ..db import SessionLocal
from ..models import Conference, SourceRecord
from . import _common


def cleanup_old_years() -> dict[str, int]:
    cutoff = _common.min_year()
    with SessionLocal() as db:
        conf_deleted = (
            db.query(Conference).filter(Conference.year < cutoff).delete(synchronize_session=False)
        )
        sr_deleted = (
            db.query(SourceRecord).filter(SourceRecord.year < cutoff).delete(synchronize_session=False)
        )
        db.commit()
    return {"cutoff_year": cutoff, "conferences_deleted": conf_deleted, "source_records_deleted": sr_deleted}
