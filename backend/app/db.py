from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
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
    # column_name: DDL fragment used in ALTER TABLE ... ADD COLUMN ...
    "predicted": "BOOLEAN DEFAULT 0",
    "diverged_detail": "TEXT",
    "tier_predicted": "BOOLEAN DEFAULT 0",
    "round": "INTEGER NOT NULL DEFAULT 1",
    "rounds_total": "INTEGER",
    "latitude": "REAL",
    "longitude": "REAL",
}


def _apply_migrations():
    """Idempotent column adds for SQLite — keeps existing data on schema upgrades.

    For unique-constraint changes (which SQLite can't ALTER in place), we drop
    and recreate the conferences table: the DB is fully reproducible from
    `app.refresh` so this is cheap.
    """
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(conferences)")).fetchall()
        existing = {r[1] for r in rows}
        if not existing:
            return  # Table doesn't exist yet — create_all() will handle it.
        for col, ddl in _REQUIRED_COLUMNS.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE conferences ADD COLUMN {col} {ddl}"))

        # Detect the old unique constraint (`uq_acronym_year`) — if present, drop
        # & recreate so the new (acronym, year, round) constraint takes effect.
        indices = conn.execute(text("PRAGMA index_list(conferences)")).fetchall()
        has_old = any(r[1] == "uq_acronym_year" for r in indices)
        has_new = any(r[1] == "uq_acronym_year_round" for r in indices)
        if has_old and not has_new:
            conn.execute(text("DROP TABLE conferences"))
            conn.execute(text("DROP TABLE IF EXISTS source_records"))
        conn.commit()


def init_db():
    from . import models  # noqa: F401
    _apply_migrations()
    Base.metadata.create_all(engine)
