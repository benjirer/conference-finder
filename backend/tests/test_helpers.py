"""Tests for the pure-function helpers — no DB, no network."""
from app.sources._common import (
    canonical_acronym,
    normalize_person_name,
    normalize_tier,
    safe_yaml_load,
    safe_yaml_load_text,
    min_year,
)


def test_canonical_acronym_known_aliases():
    assert canonical_acronym("USENIX NSDI") == "NSDI"
    assert canonical_acronym("usenix nsdi") == "NSDI"
    assert canonical_acronym("ACM/IEEE ICSE") == "ICSE"
    assert canonical_acronym("IEEE ICDCS") == "ICDCS"


def test_canonical_acronym_unknown_passthrough():
    assert canonical_acronym("L4DC") == "L4DC"
    assert canonical_acronym("RandomVenue 2026") == "RandomVenue 2026"


def test_canonical_acronym_falsy():
    assert canonical_acronym(None) is None
    assert canonical_acronym("") == ""


def test_normalize_person_name_strips_accents_and_case():
    assert normalize_person_name("Jürgen Schmidhuber") == "jurgen schmidhuber"
    assert normalize_person_name("John A. Smith") == "john smith"
    assert normalize_person_name("Yi-Han Wang") == "yi han wang"


def test_normalize_person_name_keeps_initial_when_only_one_real_token():
    # "J. Smith" must NOT collapse to "smith" — would false-match every Smith.
    assert normalize_person_name("J. Smith") == "j smith"
    assert normalize_person_name("Smith") == "smith"


def test_normalize_tier_variants():
    assert normalize_tier("A*") == "A*"
    assert normalize_tier("A1") == "A*"
    assert normalize_tier("a") == "A"
    assert normalize_tier("b1") == "B"
    assert normalize_tier(None) is None
    assert normalize_tier("N/A") is None
    # Dict and list shapes (ccfddl/ds-deadlines).
    assert normalize_tier({"core": "A*", "ccf": "A"}) == "A*"
    assert normalize_tier(["A", "A1"]) == "A"


def test_safe_yaml_load_missing_file(tmp_path):
    p = tmp_path / "nope.yaml"
    assert safe_yaml_load(p, {"default": 1}) == {"default": 1}


def test_safe_yaml_load_corrupt(tmp_path):
    p = tmp_path / "corrupt.yaml"
    p.write_text(":\n  - this:\n: : not: valid yaml:::\n")
    assert safe_yaml_load(p, {"fallback": True}) == {"fallback": True}


def test_safe_yaml_load_valid(tmp_path):
    p = tmp_path / "good.yaml"
    p.write_text("key: value\n")
    assert safe_yaml_load(p, {}) == {"key": "value"}


def test_safe_yaml_load_text_corrupt():
    # Truly invalid YAML: unclosed flow mapping with bad indent.
    assert safe_yaml_load_text("{key: value, more:\n: : : invalid\n", []) == []


def test_min_year_recent():
    from datetime import datetime, timezone
    assert min_year() == datetime.now(timezone.utc).year - 1
