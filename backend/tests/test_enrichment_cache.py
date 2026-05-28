"""EnrichmentCache behaviour — load/save/freshness/atomic-rename."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import yaml

from app._enrich_common import EnrichmentCache


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_loads_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app._enrich_common.DATA_DIR", tmp_path)
    c = EnrichmentCache("missing.yaml", ttl_days=30)
    assert c.entries_by_key == {}


def test_persists_via_put(tmp_path, monkeypatch):
    monkeypatch.setattr("app._enrich_common.DATA_DIR", tmp_path)
    c = EnrichmentCache("c.yaml", ttl_days=30)
    c.put("SIGCOMM", 2026, {"page_limit": 14})
    assert c.is_fresh("SIGCOMM", 2026)
    # New instance should see the persisted entry.
    c2 = EnrichmentCache("c.yaml", ttl_days=30)
    assert c2.get("SIGCOMM", 2026)["page_limit"] == 14


def test_freshness_decays_after_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr("app._enrich_common.DATA_DIR", tmp_path)
    c = EnrichmentCache("c.yaml", ttl_days=30)
    c.put("X", 2026, {"foo": 1})
    # Backdate the entry beyond TTL.
    c.entries_by_key["X|2026"]["extracted_at"] = (
        _utc_now() - timedelta(days=60)
    ).isoformat(timespec="seconds")
    assert not c.is_fresh("X", 2026)


def test_handles_corrupt_yaml_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr("app._enrich_common.DATA_DIR", tmp_path)
    (tmp_path / "broken.yaml").write_text("::: not valid yaml :::\n")
    c = EnrichmentCache("broken.yaml", ttl_days=30)
    assert c.entries_by_key == {}
    # Should still write valid YAML on save.
    c.put("Y", 2026, {})
    raw = yaml.safe_load((tmp_path / "broken.yaml").read_text())
    assert raw["entries"][0]["acronym"] == "Y"


def test_sibling_pc_urls_grouped_by_acronym(tmp_path, monkeypatch):
    monkeypatch.setattr("app._enrich_common.DATA_DIR", tmp_path)
    c = EnrichmentCache("c.yaml", ttl_days=30)
    c.put("X", 2025, {"pc_url": "https://x/2025/pc"})
    c.put("X", 2026, {"pc_url": "https://x/2026/pc"})
    c.put("Y", 2026, {"pc_url": "https://y/2026/pc"})
    c.put("Z", 2026, {})  # no pc_url — skipped
    grouped = c.all_pc_urls_by_acronym()
    assert sorted(grouped["X"]) == ["https://x/2025/pc", "https://x/2026/pc"]
    assert grouped["Y"] == ["https://y/2026/pc"]
    assert "Z" not in grouped
