from datetime import datetime
from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


class Conference(Base):
    __tablename__ = "conferences"
    __table_args__ = (UniqueConstraint("acronym", "year", "round", name="uq_acronym_year_round"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    acronym: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(512))
    year: Mapped[int] = mapped_column(Integer, index=True)
    round: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rounds_total: Mapped[int | None] = mapped_column(Integer, nullable=True)  # e.g. 3 when a venue has 3 review cycles

    # JSON-encoded string arrays — kept as TEXT for SQLite simplicity.
    areas: Mapped[str | None] = mapped_column(Text, default="[]")  # control/networking/ml/systems
    topics: Mapped[str | None] = mapped_column(Text, default="[]")

    is_workshop: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_venue: Mapped[str | None] = mapped_column(String(64), nullable=True)

    abstract_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submission_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notification_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    camera_ready: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conference_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conference_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    page_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    h5_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acceptance_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[str | None] = mapped_column(String(8), nullable=True)  # A*, A, B, C

    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cfp_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pc_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    source: Mapped[str] = mapped_column(String(32), default="seed")  # ccfddl|seed|llm_extract|user|predicted|aideadlines|ds-deadlines|klb2|noise-lab|confsearch
    last_verified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    diverged: Mapped[bool] = mapped_column(Boolean, default=False)
    diverged_detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of {source, field, value} when sources disagree
    predicted: Mapped[bool] = mapped_column(Boolean, default=False)
    tier_predicted: Mapped[bool] = mapped_column(Boolean, default=False)  # tier was inferred from h5_index/acceptance_rate, not directly known
    date_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceRecord(Base):
    """One row per (acronym, year, source) — the raw data each ingester saw.

    The `conferences` table holds the merged/canonical view; SourceRecord
    preserves the per-source values so we can detect when aggregators disagree.
    """
    __tablename__ = "source_records"
    __table_args__ = (UniqueConstraint("acronym", "year", "source", "round", name="uq_acronym_year_source_round"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    acronym: Mapped[str] = mapped_column(String(64), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    round: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)

    abstract_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submission_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notification_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conference_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conference_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class PCMember(Base):
    """A program-committee member of one conference (acronym, year, round=1).

    `normalized_name` is the case-folded / accent-stripped / middle-initial-dropped
    form used for cross-venue intersection matching. Affiliation is kept for
    disambiguation (two different "Y. Wang"s with different affiliations are
    distinct people; the comparison UI shows both per-venue affiliations so the
    user can sanity-check).
    """
    __tablename__ = "pc_members"
    __table_args__ = (UniqueConstraint("conference_id", "normalized_name", name="uq_conference_normalized_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conference_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(256))
    normalized_name: Mapped[str] = mapped_column(String(256), index=True)
    affiliation: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Role taxonomy: member | area-chair | track-chair | program-chair | general-chair
    role: Mapped[str] = mapped_column(String(32), default="member")


class OfficialCheck(Base):
    """Durable check state and last accepted extraction for an official edition."""
    __tablename__ = "official_checks"
    __table_args__ = (UniqueConstraint("acronym", "year", "url", name="uq_official_edition_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    acronym: Mapped[str] = mapped_column(String(64))
    year: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PendingReview(Base):
    __tablename__ = 'pending_reviews'
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    review_url: Mapped[str] = mapped_column(Text)
