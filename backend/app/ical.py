"""Generate an ICS (RFC 5545) calendar feed from the DB.

For every deadline (abstract / paper / notification / camera-ready) we emit:
  1. An all-day event on the deadline's UTC date
  2. A 1-hour event ending at the exact deadline time (UTC, Z-suffix)

iCalendar clients render the timed event in the user's local TZ automatically
because we use the `Z` suffix (UTC instant). The all-day event uses a floating
DATE value, so it appears on the same calendar-day everywhere — the deadline
day is unambiguous in any TZ.

Predicted entries are marked with `[Predicted]` in the SUMMARY and a
prominent note in DESCRIPTION so subscribers can tell at a glance which
events are extrapolated from prior years.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import Conference


def _esc(s: str | None) -> str:
    if s is None:
        return ""
    return (
        s.replace("\\", "\\\\")
         .replace(";", "\\;")
         .replace(",", "\\,")
         .replace("\n", "\\n")
    )


def _dt_utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _date(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _round_suffix(c: Conference) -> str:
    if c.rounds_total and c.rounds_total > 1:
        return f" R{c.round}/{c.rounds_total}"
    if c.round and c.round > 1:
        return f" R{c.round}"
    return ""


def _summary(c: Conference, label: str) -> str:
    prefix = "[Predicted] " if c.predicted else ""
    return f"{prefix}{label}: {c.acronym} {c.year}{_round_suffix(c)}"


def _description(c: Conference, when: datetime, label: str) -> str:
    parts: list[str] = []
    if c.predicted:
        parts.append("⚠ PREDICTED — dates extrapolated from a prior year, not from an official CFP.")
    parts.append(c.name)
    if c.cfp_url:
        parts.append(f"CFP: {c.cfp_url}")
    parts.append(f"{label} (UTC): {when.strftime('%Y-%m-%d %H:%M')}")
    if c.timezone:
        parts.append(f"Original deadline TZ: {c.timezone}")
    if c.page_limit:
        parts.append(f"Pages: {c.page_limit}")
    if c.acceptance_rate is not None:
        parts.append(f"Accept rate: {c.acceptance_rate * 100:.0f}%")
    if c.tier:
        parts.append(f"Tier: {c.tier}")
    if c.rounds_total and c.rounds_total > 1:
        parts.append(f"Round {c.round} of {c.rounds_total}")
    return "\n".join(parts)


def _deadline_events(c: Conference, when: datetime, label: str) -> list[str]:
    """Emit BOTH an all-day event on the deadline date AND a 1-hour timed event
    ending exactly at the deadline. Two events per deadline."""
    uid_base = f"{c.acronym}-{c.year}-r{c.round}-{label.lower().replace(' ', '_').replace('-', '_')}"
    stamp = _dt_utc(datetime.utcnow())
    summary = _summary(c, label)
    desc = _description(c, when, label)

    # 1. All-day event on the (UTC) deadline date.
    all_day_uid = f"{uid_base}-allday@conference-finder"
    next_day = when + timedelta(days=1)
    all_day = [
        "BEGIN:VEVENT",
        f"UID:{all_day_uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{_date(when)}",
        f"DTEND;VALUE=DATE:{_date(next_day)}",
        f"SUMMARY:{_esc(summary)} (all-day)",
        f"DESCRIPTION:{_esc(desc)}",
        "TRANSP:TRANSPARENT",  # don't show as busy
        "END:VEVENT",
    ]

    # 2. One-hour timed event ending AT the deadline (so the slot leading up to
    #    it appears in calendar). DTSTART/DTEND in UTC (Z), clients render local.
    end = when
    start = when - timedelta(hours=1)
    timed_uid = f"{uid_base}-timed@conference-finder"
    timed = [
        "BEGIN:VEVENT",
        f"UID:{timed_uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_dt_utc(start)}",
        f"DTEND:{_dt_utc(end)}",
        f"SUMMARY:{_esc(summary)} (final hour)",
        f"DESCRIPTION:{_esc(desc)}",
        "END:VEVENT",
    ]
    return all_day + timed


def _conference_event(c: Conference) -> list[str]:
    if not c.conference_start:
        return []
    end = (c.conference_end or c.conference_start) + timedelta(days=1)
    uid = f"{c.acronym}-{c.year}-r{c.round}-conf@conference-finder"
    summary = ("[Predicted] " if c.predicted else "") + f"{c.acronym} {c.year}{_round_suffix(c)}"
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_dt_utc(datetime.utcnow())}",
        f"DTSTART;VALUE=DATE:{_date(c.conference_start)}",
        f"DTEND;VALUE=DATE:{_date(end)}",
        f"SUMMARY:{_esc(summary)}",
    ]
    if c.location:
        lines.append(f"LOCATION:{_esc(c.location)}")
    desc = c.name
    if c.predicted:
        desc = "⚠ PREDICTED — dates extrapolated from a prior year.\n" + desc
    if c.cfp_url:
        desc += f"\n{c.cfp_url}"
    lines.append(f"DESCRIPTION:{_esc(desc)}")
    lines.append("END:VEVENT")
    return lines


def build_ics(conferences: list[Conference]) -> str:
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//conference-finder//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Conferences",
    ]
    for c in conferences:
        if c.abstract_deadline:
            out.extend(_deadline_events(c, c.abstract_deadline, "Abstract deadline"))
        if c.submission_deadline:
            out.extend(_deadline_events(c, c.submission_deadline, "Paper deadline"))
        if c.notification_date:
            out.extend(_deadline_events(c, c.notification_date, "Notification"))
        if c.camera_ready:
            out.extend(_deadline_events(c, c.camera_ready, "Camera-ready"))
        out.extend(_conference_event(c))
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"
