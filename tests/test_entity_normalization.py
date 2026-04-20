"""Tests for entity normalization on the save path (M0)."""
from palinode.api.server import _normalize_entities


def test_bare_string_gets_category_prefix():
    assert _normalize_entities(["alice"], "people") == ["person/alice"]


def test_bare_string_project_category():
    assert _normalize_entities(["palinode"], "projects") == ["project/palinode"]


def test_already_prefixed_unchanged():
    assert _normalize_entities(["person/alice"], "people") == ["person/alice"]
    assert _normalize_entities(["project/palinode"], "insights") == ["project/palinode"]


def test_mixed_bare_and_prefixed():
    result = _normalize_entities(["alice", "project/palinode"], "people")
    assert result == ["person/alice", "project/palinode"]


def test_unknown_category_defaults_to_project():
    assert _normalize_entities(["foo"], "unknown_cat") == ["project/foo"]


def test_empty_list():
    assert _normalize_entities([], "people") == []


def test_all_categories():
    cases = [
        ("people", "person"),
        ("decisions", "decision"),
        ("projects", "project"),
        ("insights", "insight"),
        ("research", "research"),
        ("inbox", "action"),
    ]
    for category, expected_prefix in cases:
        result = _normalize_entities(["test"], category)
        assert result == [f"{expected_prefix}/test"], f"Failed for {category}"
