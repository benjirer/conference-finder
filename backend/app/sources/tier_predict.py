"""Fill in missing `tier` values using h5_index / acceptance_rate heuristics.

These predictions are marked `tier_predicted=True` so the UI can show them in
italic / with a dotted underline. Anything we can't reasonably guess is left
null. Existing (non-predicted) tiers are never overwritten.

Thresholds are deliberately conservative — they're meant to give a rough
indicator, not a precise CORE-rank substitute.
"""
from __future__ import annotations

from ..db import SessionLocal
from ..models import Conference


def _tier_from_h5(h5: int | None) -> str | None:
    if h5 is None:
        return None
    if h5 >= 100: return "A*"
    if h5 >= 50:  return "A"
    if h5 >= 20:  return "B"
    if h5 >= 5:   return "C"
    return None


def _tier_from_accept(rate: float | None) -> str | None:
    if rate is None:
        return None
    # Heuristic: lower acceptance rates correlate with higher prestige, but
    # only when the venue is selective at all. Skip when >0.6 (open conferences).
    if rate < 0.18: return "A*"
    if rate < 0.28: return "A"
    if rate < 0.45: return "B"
    if rate < 0.60: return "C"
    return None


def predict_tiers() -> dict[str, int]:
    filled = 0
    skipped = 0
    with SessionLocal() as db:
        rows = db.query(Conference).filter(Conference.tier.is_(None)).all()
        for r in rows:
            t = _tier_from_h5(r.h5_index) or _tier_from_accept(r.acceptance_rate)
            if t is None:
                skipped += 1
                continue
            r.tier = t
            r.tier_predicted = True
            filled += 1
        db.commit()
    return {"filled": filled, "skipped_no_signal": skipped}
