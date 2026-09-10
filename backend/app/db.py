"""Database setup + idempotent migrations.

The DB is fully reproducible from `app.refresh` (which rebuilds it from public
aggregators + the committed YAML caches), so we treat the on-disk SQLite file
as cache rather than source-of-truth. Migrations therefore can afford to be
lossy when the table shape changes — but we still want them loud and opt-in
for unique-constraint changes that would require a table drop.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

log = logging.getLogger("conference_finder")

BUNDLED_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR = Path(os.environ.get("CONFERENCE_FINDER_DATA_DIR", str(BUNDLED_DATA_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "conferences.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


_REQUIRED_COLUMNS = {
    "date_metadata": "TEXT",
    # column_name: DDL fragment used in ALTER TABLE ... ADD COLUMN ...
    "predicted": "BOOLEAN DEFAULT 0",
    "diverged_detail": "TEXT",
    "tier_predicted": "BOOLEAN DEFAULT 0",
    "round": "INTEGER NOT NULL DEFAULT 1",
    "rounds_total": "INTEGER",
    "latitude": "REAL",
    "longitude": "REAL",
    "pc_url": "TEXT",
}


def _destructive_migrations_allowed() -> bool:
    """Drop-and-recreate is OK on Render (DB is ephemeral, rebuilt on every
    cold start) and on local-dev (data is reproducible from YAML + sources).
    A user can set CONFERENCE_FINDER_NO_DESTRUCTIVE=1 if they're running this
    in some other environment where they care about preserving the DB."""
    return os.environ.get("CONFERENCE_FINDER_NO_DESTRUCTIVE", "0") != "1"


def _apply_migrations():
    """Idempotent column adds + opt-in destructive table-drop for constraint changes."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(conferences)")).fetchall()
        existing = {r[1] for r in rows}
        if not existing:
            return  # Table doesn't exist yet — create_all() will handle it.

        # Non-destructive: add any missing columns.
        for col, ddl in _REQUIRED_COLUMNS.items():
            if col not in existing:
                log.info("migration: ADD COLUMN conferences.%s", col)
                conn.execute(text(f"ALTER TABLE conferences ADD COLUMN {col} {ddl}"))

        # Destructive: detect the old `uq_acronym_year` unique constraint.
        # SQLite can't ALTER a unique constraint in place, so we drop and let
        # `Base.metadata.create_all()` rebuild with the new key.
        indices = conn.execute(text("PRAGMA index_list(conferences)")).fetchall()
        has_old = any(r[1] == "uq_acronym_year" for r in indices)
        has_new = any(r[1] == "uq_acronym_year_round" for r in indices)
        if has_old and not has_new:
            if not _destructive_migrations_allowed():
                log.error(
                    "migration: detected obsolete uq_acronym_year constraint, "
                    "but CONFERENCE_FINDER_NO_DESTRUCTIVE=1 is set. Skipping. "
                    "The new unique key won't take effect until you allow the "
                    "drop or recreate the DB manually."
                )
            else:
                log.warning(
                    "migration: DROPPING conferences + source_records tables to "
                    "replace obsolete uq_acronym_year constraint with the new "
                    "(acronym, year, round) key. Data will be rebuilt by app.refresh."
                )
                conn.execute(text("DROP TABLE conferences"))
                conn.execute(text("DROP TABLE IF EXISTS source_records"))
        conn.commit()


def init_db():
    from . import models  # noqa: F401
    _apply_migrations()
    Base.metadata.create_all(engine)
