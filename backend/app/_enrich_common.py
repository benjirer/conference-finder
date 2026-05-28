"""Shared helpers for the LLM-enrichment scripts (enrich_extras + enrich_pc).

Both scripts keep an idempotent YAML cache of LLM results so subsequent runs can
skip fresh entries and so the result ships to Render via git instead of via
live API calls. This module centralises that pattern.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .sources import _common

log = logging.getLogger("conference_finder")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class EnrichmentCache:
    """A YAML-backed `{key: entry}` cache with TTL-based freshness.

    Entries look like:
        {acronym, year, extracted_at, <fields the script wants to remember>}

    Keys are "ACRONYM|YEAR" strings. Writes happen incrementally — every `put`
    rewrites the YAML so a long-running script can be Ctrl-C'd without losing
    progress.
    """

    def __init__(self, filename: str, ttl_days: int) -> None:
        self.path: Path = DATA_DIR / filename
        self.ttl_days = ttl_days
        raw = _common.safe_yaml_load(self.path, {"entries": []})
        if not isinstance(raw, dict):
            raw = {"entries": []}
        raw.setdefault("entries", [])
        self.raw: dict = raw
        self.entries_by_key: dict[str, dict] = {}
        for e in raw["entries"]:
            if not isinstance(e, dict):
                continue
            try:
                k = self.key(e["acronym"], e["year"])
            except KeyError:
                continue
            self.entries_by_key[k] = e

    @staticmethod
    def key(acronym: str, year: int) -> str:
        return f"{acronym}|{year}"

    def get(self, acronym: str, year: int) -> dict | None:
        return self.entries_by_key.get(self.key(acronym, year))

    def is_fresh(self, acronym: str, year: int) -> bool:
        e = self.get(acronym, year)
        if not e:
            return False
        ts = e.get("extracted_at")
        if not ts:
            return False
        try:
            when = datetime.fromisoformat(str(ts))
        except ValueError:
            return False
        return (_common.utc_now() - when) < timedelta(days=self.ttl_days)

    def put(self, acronym: str, year: int, entry: dict) -> None:
        """Insert/replace an entry and persist to disk immediately."""
        entry = {**entry, "acronym": acronym, "year": year}
        entry.setdefault("extracted_at", _common.utc_now().isoformat(timespec="seconds"))
        self.entries_by_key[self.key(acronym, year)] = entry
        self._save()

    def all_pc_urls_by_acronym(self) -> dict[str, list[str]]:
        """Group known pc_urls across all cached entries by acronym — used as
        sibling-year hints for new extractions."""
        out: dict[str, list[str]] = {}
        for e in self.entries_by_key.values():
            if e.get("pc_url"):
                out.setdefault(e["acronym"], []).append(e["pc_url"])
        return out

    def _save(self) -> None:
        self.raw["entries"] = list(self.entries_by_key.values())
        # Atomic-ish write: temp file then rename. Avoids leaving a half-written
        # YAML on disk if the process is killed mid-flush.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(self.raw, sort_keys=False, allow_unicode=True))
        tmp.replace(self.path)
