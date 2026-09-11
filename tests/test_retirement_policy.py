"""Document-relative retirement policy (ADR-020).

Three surfaces, one rule:

- :mod:`palinode.consolidation.retirement` classifies a document as
  ``age-eligible`` (episodic — daily/insights/research/status docs) or
  ``superseded-only`` (identity/profile — ``people/``, a project's profile doc,
  a living doc, a ``core: true`` doc), with a frontmatter override in both
  directions;
- the TTL sweep leaves a superseded-only document with a lapsed ``expires_at``
  in place and still archives an episodic one;
- the executor refuses an age-argued ARCHIVE against a superseded-only
  document, applies one that names ``superseded_by``, and is a strict no-op for
  an episodic document.

Real files under ``tmp_path``, real SQLite for the sweep — no mocks.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from palinode.consolidation import retirement, ttl
from palinode.consolidation.executor import apply_operations
from palinode.consolidation.retirement import (
    AGE_ELIGIBLE,
    SUPERSEDED_ONLY,
    classify,
    is_superseded_only,
    retirement_policy,
)
from palinode.core.config import config


@pytest.fixture(autouse=True)
def _memory_dir(tmp_path, monkeypatch):
    # write_memory_file (the executor's write primitive) validates its target
    # resolves inside config.memory_dir, and the classifier reads paths
    # relative to it. The sweep touches the real index (status push + trigger
    # expiry), so point the DB at this tmp_path too — never the developer's.
    from palinode.core import store

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", os.path.join(str(tmp_path), ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


def _write(tmp_path, rel: str, frontmatter: str, body: str = "") -> str:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter.strip()}\n---\n\n{body}", encoding="utf-8")
    return str(path)


# ───────────────────────────── the classifier ────────────────────────────────


@pytest.mark.parametrize(
    "rel,frontmatter,expected",
    [
        # identity / profile → superseded-only
        ("people/alice.md", {"id": "person-alice", "category": "person"}, SUPERSEDED_ONLY),
        ("people/bob.md", {"id": "person-bob"}, SUPERSEDED_ONLY),
        ("projects/alpha.md", {"id": "project-alpha", "category": "project"}, SUPERSEDED_ONLY),
        ("insights/x.md", {"type": "PersonMemory"}, SUPERSEDED_ONLY),
        ("inbox/uptime.md", {"update_policy": "replace"}, SUPERSEDED_ONLY),
        ("insights/pinned.md", {"core": True}, SUPERSEDED_ONLY),
        ("insights/pinned-str.md", {"core": "true"}, SUPERSEDED_ONLY),
        # episodic → age-eligible
        ("daily/2026-01-01.md", {"category": "daily"}, AGE_ELIGIBLE),
        ("insights/lesson.md", {"category": "insight"}, AGE_ELIGIBLE),
        ("research/2026-01-01-paper.md", {"category": "research"}, AGE_ELIGIBLE),
        ("projects/alpha-status.md", {"category": "project"}, AGE_ELIGIBLE),
        ("decisions/pick-a-db.md", {"category": "decision"}, AGE_ELIGIBLE),
        ("inbox/scratch.md", {"category": "inbox", "core": False}, AGE_ELIGIBLE),
        ("loose.md", {}, AGE_ELIGIBLE),
    ],
)
def test_classifier_by_class(rel, frontmatter, expected):
    assert retirement_policy(rel, frontmatter) == expected
    assert is_superseded_only(rel, frontmatter) is (expected == SUPERSEDED_ONLY)


def test_override_protects_an_episodic_document():
    policy, signal = classify(
        "insights/house-rules.md",
        {"category": "insight", "retirement_policy": "superseded-only"},
    )
    assert policy == SUPERSEDED_ONLY
    assert signal == "declared:retirement_policy"


def test_override_exposes_an_identity_document_to_age():
    policy, signal = classify(
        "people/former-contractor.md",
        {"category": "person", "core": True, "retirement_policy": "age-eligible"},
    )
    assert policy == AGE_ELIGIBLE
    assert signal == "declared:retirement_policy"


def test_unknown_override_value_falls_back_to_inference(caplog):
    with caplog.at_level(logging.WARNING, logger="palinode.consolidation.retirement"):
        policy = retirement_policy("people/alice.md", {"retirement_policy": "whenever"})
    assert policy == SUPERSEDED_ONLY  # the path still decides
    assert "unknown retirement_policy value" in caplog.text


def test_classifier_reads_absolute_paths_under_the_memory_dir(tmp_path):
    abs_path = os.path.join(str(tmp_path), "people", "alice.md")
    assert retirement_policy(abs_path, {}) == SUPERSEDED_ONLY
    assert retirement_policy(os.path.join(str(tmp_path), "insights", "x.md"), {}) == AGE_ELIGIBLE


def test_classifier_is_deterministic():
    fm = {"category": "person"}
    results = {classify("people/alice.md", fm) for _ in range(10)}
    assert results == {(SUPERSEDED_ONLY, "category:person")}


def test_classifier_tolerates_missing_frontmatter():
    assert retirement_policy("people/alice.md", None) == SUPERSEDED_ONLY
    assert retirement_policy("insights/x.md", None) == AGE_ELIGIBLE
    # A non-mapping (a garbled parse) is treated as no frontmatter, not raised.
    assert retirement_policy("insights/x.md", ["not", "a", "dict"]) == AGE_ELIGIBLE


# ───────────────────────────── the TTL sweep ─────────────────────────────────


def _past() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()


def test_ttl_sweep_skips_superseded_only_documents(tmp_path, caplog):
    person = _write(
        tmp_path, "people/alice.md",
        f"id: person-alice\ncategory: person\nexpires_at: {_past()}",
        "# Alice\n\nStill a person.\n",
    )
    core_doc = _write(
        tmp_path, "insights/standing-rule.md",
        f"id: insight-standing\ncategory: insight\ncore: true\nexpires_at: {_past()}",
        "The rule.\n",
    )
    episodic = _write(
        tmp_path, "inbox/probe.md",
        f"id: inbox-probe\ncategory: inbox\nexpires_at: {_past()}",
        "Ephemeral.\n",
    )

    with caplog.at_level(logging.INFO, logger="palinode.ttl"):
        result = ttl.archive_expired()

    assert result["count"] == 1
    assert result["archived"] == ["inbox/probe.md"]
    assert result["skipped_superseded_only"] == 2
    # One line for the whole sweep, carrying the count and a sample path.
    skip_lines = [r for r in caplog.records if "superseded-only" in r.getMessage()]
    assert len(skip_lines) == 1
    assert "skipped 2 expired document(s)" in skip_lines[0].getMessage()

    for protected in (person, core_doc):
        assert "status: archived" not in open(protected, encoding="utf-8").read()
    assert "status: archived" in open(episodic, encoding="utf-8").read()


def test_ttl_sweep_honours_an_age_eligible_override(tmp_path):
    _write(
        tmp_path, "people/former.md",
        f"id: person-former\ncategory: person\nretirement_policy: age-eligible\n"
        f"expires_at: {_past()}",
        "# Former\n",
    )
    result = ttl.archive_expired()
    assert result["archived"] == ["people/former.md"]
    assert result["skipped_superseded_only"] == 0


def test_ttl_sweep_reports_no_skips_when_nothing_is_protected(tmp_path, caplog):
    _write(
        tmp_path, "inbox/probe.md",
        f"id: inbox-probe\ncategory: inbox\nexpires_at: {_past()}", "Ephemeral.\n",
    )
    with caplog.at_level(logging.INFO, logger="palinode.ttl"):
        result = ttl.archive_expired()
    assert result["skipped_superseded_only"] == 0
    assert "superseded-only" not in caplog.text


# ───────────────────────────── the executor guard ────────────────────────────


_PROFILE_BODY = "# Alice\n\n- [2024-01-01] Alice's dog is named Rex <!-- fact:f1 -->\n"
_AGE_OP = {"op": "ARCHIVE", "id": "f1", "rationale": "stale: >60 days, never referenced"}


def _history_path(path: str) -> str:
    return path.replace(".md", "-history.md")


def test_executor_rejects_age_archive_on_a_profile_document(tmp_path, caplog):
    path = _write(tmp_path, "people/alice.md", "id: person-alice\ncategory: person", _PROFILE_BODY)
    before = open(path, encoding="utf-8").read()

    with caplog.at_level(logging.WARNING, logger="palinode.consolidation.executor"):
        stats = apply_operations(path, [dict(_AGE_OP)])

    assert stats["archived"] == 0
    assert stats["protected_rejected"] == 1
    assert open(path, encoding="utf-8").read() == before
    assert not os.path.exists(_history_path(path))
    # The rejection says why, and names the signal that protected the doc.
    assert "retirement_policy=superseded-only" in caplog.text
    assert "(category:person)" in caplog.text
    assert "age/staleness is not a retirement reason" in caplog.text


def test_executor_accepts_archive_naming_a_successor(tmp_path):
    path = _write(tmp_path, "people/alice.md", "id: person-alice\ncategory: person", _PROFILE_BODY)

    stats = apply_operations(
        path,
        [{"op": "ARCHIVE", "id": "f1", "reason": "Rex died; replaced by the new dog fact",
          "superseded_by": "f7"}],
    )

    assert stats["archived"] == 1
    assert stats["protected_rejected"] == 0
    assert "fact:f1" not in open(path, encoding="utf-8").read()
    assert os.path.exists(_history_path(path))


def test_executor_applies_the_same_age_archive_to_an_episodic_document(tmp_path):
    path = _write(
        tmp_path, "projects/alpha-status.md", "id: project-alpha\ncategory: project",
        "# Alpha status\n\n- [2024-01-01] Milestone one shipped <!-- fact:f1 -->\n",
    )

    stats = apply_operations(path, [dict(_AGE_OP)])

    assert stats["archived"] == 1
    assert stats["protected_rejected"] == 0
    assert "fact:f1" not in open(path, encoding="utf-8").read()


def test_executor_leaves_supersede_and_retract_alone_on_a_profile_document(tmp_path):
    path = _write(
        tmp_path, "people/alice.md", "id: person-alice\ncategory: person",
        "# Alice\n\n- [2024-01-01] Dog is Rex <!-- fact:f1 -->\n"
        "- [2024-01-02] Lives in Berlin <!-- fact:f2 -->\n",
    )

    stats = apply_operations(path, [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "Dog is Fido", "reason": "the dog changed"},
        {"op": "RETRACT", "id": "f2", "reason": "never true — that was her sister"},
    ])

    assert stats["superseded"] == 1
    assert stats["retracted"] == 1
    assert stats["protected_rejected"] == 0


def test_executor_guard_applies_to_a_project_profile_but_not_its_status_layer(tmp_path):
    profile = _write(
        tmp_path, "projects/alpha.md", "id: project-alpha\ncategory: project",
        "# Alpha\n\n- [2024-01-01] Alpha is the checkout rewrite <!-- fact:f1 -->\n",
    )
    status = _write(
        tmp_path, "projects/alpha-status.md", "id: project-alpha-status\ncategory: project",
        "# Alpha status\n\n- [2024-01-01] Sprint 1 closed <!-- fact:f1 -->\n",
    )

    assert apply_operations(profile, [dict(_AGE_OP)])["protected_rejected"] == 1
    assert apply_operations(status, [dict(_AGE_OP)])["archived"] == 1


def test_executor_guard_honours_the_age_eligible_override(tmp_path):
    path = _write(
        tmp_path, "people/former.md",
        "id: person-former\ncategory: person\nretirement_policy: age-eligible",
        "# Former\n\n- [2024-01-01] Contract fact <!-- fact:f1 -->\n",
    )
    stats = apply_operations(path, [dict(_AGE_OP)])
    assert stats["archived"] == 1 and stats["protected_rejected"] == 0


def test_executor_guard_honours_the_superseded_only_override_on_an_episodic_doc(tmp_path):
    path = _write(
        tmp_path, "insights/house-rules.md",
        "id: insight-house-rules\ncategory: insight\nretirement_policy: superseded-only",
        "# House rules\n\n- [2024-01-01] Rule one <!-- fact:f1 -->\n",
    )
    stats = apply_operations(path, [dict(_AGE_OP)])
    assert stats["archived"] == 0 and stats["protected_rejected"] == 1


def test_executor_guard_is_deterministic_across_repeat_runs(tmp_path):
    path = _write(tmp_path, "people/alice.md", "id: person-alice\ncategory: person", _PROFILE_BODY)
    before = open(path, encoding="utf-8").read()

    runs = [apply_operations(path, [dict(_AGE_OP)]) for _ in range(3)]

    assert all(s["protected_rejected"] == 1 and s["archived"] == 0 for s in runs)
    assert open(path, encoding="utf-8").read() == before


def test_executor_guard_falls_open_on_garbled_frontmatter(tmp_path):
    # Mirrors the replace-guard's fail-open: an unparseable document must never
    # block consolidation. The path here is episodic, so nothing protects it.
    path = tmp_path / "insights" / "garbled.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n: : not: yaml: [\n---\n\n- [2024-01-01] A fact <!-- fact:f1 -->\n",
        encoding="utf-8",
    )
    stats = apply_operations(str(path), [dict(_AGE_OP)])
    assert stats["archived"] == 1 and stats["protected_rejected"] == 0


def test_retirement_module_exports_the_policy_vocabulary():
    # The one home for the rule: other consolidation paths (lint-derived
    # proposals, future evidence-gated archive) consume these, not a local copy.
    assert retirement.VALID_POLICIES == (AGE_ELIGIBLE, SUPERSEDED_ONLY)
    assert retirement.POLICY_FIELD == "retirement_policy"
