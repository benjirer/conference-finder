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


def canonicalize_existing():
    """Merge known aliases into existing stable venue IDs, preserving PC data."""
    from ..models import PCMember, OfficialCheck
    merged = 0
    with SessionLocal() as db:
        for row in db.query(Conference).all():
            canonical = _common.canonical_acronym(row.acronym)
            if canonical == row.acronym:
                continue
            target = db.query(Conference).filter_by(acronym=canonical, year=row.year, round=row.round).first()
            if target is None:
                row.acronym = canonical
                db.flush()
                continue
            authoritative = (target.predicted and not row.predicted) or (row.source == 'ccfddl' and target.source != 'ccfddl')
            for column in Conference.__table__.columns:
                if column.name in {'id', 'acronym'}:
                    continue
                value = getattr(row, column.name)
                if authoritative or getattr(target, column.name) is None:
                    setattr(target, column.name, value)
            for member in db.query(PCMember).filter_by(conference_id=row.id):
                duplicate = db.query(PCMember).filter_by(conference_id=target.id, normalized_name=member.normalized_name).first()
                if duplicate:
                    db.delete(member)
                else:
                    member.conference_id = target.id
                db.flush()
            db.delete(row)
            db.flush()
            merged += 1
        for row in db.query(SourceRecord).all():
            canonical = _common.canonical_acronym(row.acronym)
            if canonical == row.acronym:
                continue
            target = db.query(SourceRecord).filter_by(acronym=canonical, year=row.year, round=row.round, source=row.source).first()
            if target is None:
                row.acronym = canonical
            else:
                if row.fetched_at > target.fetched_at:
                    for column in SourceRecord.__table__.columns:
                        if column.name not in {'id', 'acronym'}:
                            setattr(target, column.name, getattr(row, column.name))
                db.delete(row)
            db.flush()
        for row in db.query(OfficialCheck).all():
            canonical = _common.canonical_acronym(row.acronym)
            if canonical == row.acronym:
                continue
            target = db.query(OfficialCheck).filter_by(acronym=canonical, year=row.year, url=row.url).first()
            if target is None:
                row.acronym = canonical
            else:
                if row.verified_at and (not target.verified_at or row.verified_at > target.verified_at):
                    target.verified_at, target.payload, target.content_hash = row.verified_at, row.payload, row.content_hash
                db.delete(row)
            db.flush()
        db.commit()
    return {'merged': merged}
