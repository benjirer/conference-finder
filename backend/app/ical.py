"""Generate an ICS (RFC 5545) calendar feed from the DB."""
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


def _deadline_event(c: Conference, when: datetime, label: str) -> list[str]:
    desc_parts = [c.name]
    if c.cfp_url:
        desc_parts.append(f"CFP: {c.cfp_url}")
    if c.page_limit:
        desc_parts.append(f"Pages: {c.page_limit}")
    if c.acceptance_rate is not None:
        desc_parts.append(f"Accept rate: {c.acceptance_rate * 100:.0f}%")
    if c.tier:
        desc_parts.append(f"Tier: {c.tier}")
    if c.timezone:
        desc_parts.append(f"Original TZ: {c.timezone}")

    uid = f"{c.acronym}-{c.year}-{label.lower().replace(' ', '_')}@conference-finder"
    end = when + timedelta(minutes=30)
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_dt_utc(datetime.utcnow())}",
        f"DTSTART:{_dt_utc(when)}",
        f"DTEND:{_dt_utc(end)}",
        f"SUMMARY:{_esc(f'{label}: {c.acronym} {c.year}')}",
        f"DESCRIPTION:{_esc(chr(10).join(desc_parts))}",
        "END:VEVENT",
    ]


def _conference_event(c: Conference) -> list[str]:
    if not c.conference_start:
        return []
    end = (c.conference_end or c.conference_start) + timedelta(days=1)
    uid = f"{c.acronym}-{c.year}-conf@conference-finder"
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_dt_utc(datetime.utcnow())}",
        f"DTSTART;VALUE=DATE:{_date(c.conference_start)}",
        f"DTEND;VALUE=DATE:{_date(end)}",
        f"SUMMARY:{_esc(f'{c.acronym} {c.year}')}",
    ]
    if c.location:
        lines.append(f"LOCATION:{_esc(c.location)}")
    desc = c.name
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
            out.extend(_deadline_event(c, c.abstract_deadline, "Abstract deadline"))
        if c.submission_deadline:
            out.extend(_deadline_event(c, c.submission_deadline, "Paper deadline"))
        if c.notification_date:
            out.extend(_deadline_event(c, c.notification_date, "Notification"))
        if c.camera_ready:
            out.extend(_deadline_event(c, c.camera_ready, "Camera-ready"))
        out.extend(_conference_event(c))
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"
