"""A malformed ``layer_hint`` must cost one file's hint, not the whole sweep.

``split_all_core_files`` mutates files in a loop, so an exception raised partway
through leaves some files rewritten and others untouched with no boundary marking
where it stopped. The trigger used to be a typo: YAML parses a bare ``layer_hint:``
as ``None``, and ``None.lower()`` raised.

The hint is an *optimization over a heuristic* that classifies correctly without it,
so the correct degradation is to drop the hint for that one file and carry on. The
second half of these tests pins the other direction — that carrying on happens
*audibly*, because a control the author believes is in effect and silently is not is
the same defect class the hint was added to resolve.
"""
from __future__ import annotations

import logging
import os

import pytest

from palinode.consolidation import layer_split
from palinode.core.config import config


@pytest.fixture(autouse=True)
def _memory_dir(tmp_path, monkeypatch):
    # write_memory_file (layer_split's write primitive) validates its target
    # resolves inside config.memory_dir — every fixture/test here writes
    # under tmp_path, so point memory_dir there by default. Individual tests
    # that already set it explicitly just re-set the same value.
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))


# Two sections straddling the identity and status keyword lists, so heuristic
# classification demonstrably routes them to *different* layers — which is how a
# test can tell "the hint was ignored" apart from "the hint was applied".
BODY = """## Architecture

The scheduler leases partitions for a fixed term and renews them out of band, so a
stalled worker loses its lease without the coordinator having to detect the stall.

## Current Status

Rolled out to the eu-west shard on 2026-03-11. Remaining shards are queued.
"""

IDENTITY_MARKER = "leases partitions"
STATUS_MARKER = "eu-west shard"


def _write_source(directory, name: str, hint_line: str | None) -> str:
    """Write a ``core: true`` memory file, optionally carrying a raw hint line."""
    meta = f"id: projects-{name}\ncategory: projects\ncore: true\n"
    if hint_line is not None:
        meta += f"{hint_line}\n"
    path = os.path.join(str(directory), f"{name}.md")
    with open(path, "w") as f:
        f.write(f"---\n{meta}---\n\n{BODY}")
    return path


def _body_of(path: str) -> str:
    content = open(path).read()
    if content.startswith("---"):
        return content.split("---", 2)[2].strip()
    return content.strip()


def test_fixture_preconditions():
    """The fixture must actually split across two layers, or nothing below bites."""
    assert BODY.count("\n## ") + BODY.startswith("## ") == 2
    assert BODY.count(IDENTITY_MARKER) == 1
    assert BODY.count(STATUS_MARKER) == 1


# --------------------------------------------------------------------------
# Degradation: a bad hint must not raise
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hint_line,label",
    [
        ("layer_hint:", "bare key parses as None"),
        ("layer_hint: ''", "explicitly empty"),
        ("layer_hint: 123", "non-string scalar"),
        ("layer_hint: histroy", "typo of a real value"),
        ("layer_hint: archive", "plausible but unsupported"),
        ("layer_hint:\n  - history", "a list, not a scalar"),
    ],
)
def test_malformed_hint_does_not_raise(tmp_path, hint_line, label):
    """Every unusable hint shape degrades to heuristic classification."""
    src = _write_source(tmp_path, "sched", hint_line)

    results = layer_split.split_file(src)

    # Heuristic classification ran: the two sections landed in different layers.
    assert IDENTITY_MARKER in _body_of(results["identity"]), label
    assert "status" in results, f"{label}: status layer should have been written"
    assert STATUS_MARKER in _body_of(results["status"]), label


def test_non_mapping_frontmatter_does_not_raise(tmp_path):
    """Frontmatter that parses to a scalar is ignored, not fatal.

    Same failure shape as the malformed hint — every ``metadata.get`` below the
    parse would raise on a non-dict — so it is pinned in the same place.
    """
    path = os.path.join(str(tmp_path), "scalar-fm.md")
    with open(path, "w") as f:
        f.write(f"---\njust a bare string, not a mapping\n---\n\n{BODY}")

    results = layer_split.split_file(path)

    assert IDENTITY_MARKER in _body_of(results["identity"])


def test_one_bad_file_does_not_abort_the_sweep(tmp_path, monkeypatch):
    """The regression that matters: partial application across a batch mutation.

    Three core files, one carrying a bare ``layer_hint:``. Previously the sweep
    raised on the bad one, leaving however many files it had already rewritten in
    the new shape and the rest in the old, with nothing recording the boundary.
    """
    projects = tmp_path / "projects"
    projects.mkdir()
    for name, hint in [("alpha", None), ("bravo", "layer_hint:"), ("charlie", None)]:
        _write_source(projects, name, hint)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    # Return no vector so trigger auto-registration is skipped without touching
    # the store or the network — the sweep's completion is what is under test.
    monkeypatch.setattr("palinode.core.embedder.embed", lambda *a, **k: None)

    stats = layer_split.split_all_core_files()

    assert stats["files_split"] == 3, "the sweep must not stop at the malformed file"
    for name in ("alpha", "bravo", "charlie"):
        assert os.path.exists(projects / f"{name}-status.md"), f"{name} was not split"


def _layer_split_records(caplog) -> list[logging.LogRecord]:
    """Only this module's logger — the tmp_path here is not a git repo, so the
    git_tools choke point now logs its own ERROR for the split's
    auto-commit, which is not the warning under test."""
    return [r for r in caplog.records if r.name == "palinode.consolidation.layer_split"]


# --------------------------------------------------------------------------
# Audibility: degrading must not be silent
# --------------------------------------------------------------------------

def test_unrecognized_hint_warns_with_file_and_accepted_set(tmp_path, caplog):
    """The warning must name the file, the offending value, and what is accepted."""
    src = _write_source(tmp_path, "sched", "layer_hint: histroy")

    with caplog.at_level(logging.WARNING, logger="palinode.consolidation.layer_split"):
        layer_split.split_file(src)

    records = _layer_split_records(caplog)
    assert len(records) == 1, "expected exactly one warning"
    message = records[0].getMessage()
    assert "sched.md" in message, "warning must name the file"
    assert "histroy" in message, "warning must quote the offending value"
    for accepted in layer_split.LAYER_HINTS:
        assert accepted in message, f"warning must list {accepted!r} as accepted"


def test_ignored_hint_is_reported_in_the_result(tmp_path):
    """A caller reading only the return value still learns the hint did not apply."""
    src = _write_source(tmp_path, "sched", "layer_hint: archive")

    results = layer_split.split_file(src)

    assert results["layer_hint_ignored"] == "archive"


def test_bare_hint_is_reported_even_though_its_value_is_none(tmp_path):
    """``None`` is a legitimate ignored value, so presence cannot be inferred from it."""
    src = _write_source(tmp_path, "sched", "layer_hint:")

    results = layer_split.split_file(src)

    assert "layer_hint_ignored" in results
    assert results["layer_hint_ignored"] is None


def test_trigger_failure_is_logged_not_printed(tmp_path, monkeypatch, caplog, capsys):
    """A failed auto-trigger must go through the logger, not stdout.

    The sweep runs inside the API server process, so a ``print`` here never
    reaches the caller: not the HTTP response, and not the CLI, which talks to
    this code over HTTP. It also skipped the JSONL operations log and the
    secret redaction both handlers apply.
    """
    projects = tmp_path / "projects"
    projects.mkdir()
    _write_source(projects, "alpha", None)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))

    def _boom(*a, **k):
        raise RuntimeError("embedder unreachable")

    monkeypatch.setattr("palinode.core.embedder.embed", _boom)

    with caplog.at_level(logging.WARNING, logger="palinode.consolidation.layer_split"):
        stats = layer_split.split_all_core_files()

    records = [r for r in _layer_split_records(caplog) if "auto-register" in r.getMessage()]
    assert len(records) == 1, "expected exactly one warning for the failed trigger"
    record = records[0]
    assert record.levelno == logging.WARNING, "the record must carry a level"
    message = record.getMessage()
    assert "alpha.md" in message, "warning must name the file"
    assert "embedder unreachable" in message, "warning must carry the failure"

    assert "Failed to auto-register" not in capsys.readouterr().out

    # The trigger is optional enrichment: the sweep still completes.
    assert stats["files_split"] == 1
    assert stats["triggers_registered"] == 0


def test_sweep_counts_ignored_hints(tmp_path, monkeypatch):
    """A sweep that fell back to heuristics must not report a clean run."""
    projects = tmp_path / "projects"
    projects.mkdir()
    _write_source(projects, "alpha", None)
    _write_source(projects, "bravo", "layer_hint:")
    _write_source(projects, "charlie", "layer_hint: histroy")

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr("palinode.core.embedder.embed", lambda *a, **k: None)

    stats = layer_split.split_all_core_files()

    assert stats["hints_ignored"] == 2
    reported = {e["file"]: e["value"] for e in stats["files_with_ignored_hints"]}
    assert len(reported) == 2
    assert any(f.endswith("bravo.md") for f in reported)
    assert any(f.endswith("charlie.md") for f in reported)
    assert "histroy" in reported.values()


# --------------------------------------------------------------------------
# No false positives: a valid hint must still work, silently
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hint", ["identity", "status", "history"])
def test_valid_hint_still_routes_and_reports_nothing(tmp_path, hint, caplog):
    """Hardening must not make a working hint noisy or mark it ignored."""
    src = _write_source(tmp_path, "sched", f"layer_hint: {hint}")

    with caplog.at_level(logging.WARNING, logger="palinode.consolidation.layer_split"):
        results = layer_split.split_file(src)

    assert "layer_hint_ignored" not in results
    assert _layer_split_records(caplog) == []
    # The whole body went to the named layer rather than being split across two.
    target = results[hint]
    assert IDENTITY_MARKER in _body_of(target)
    assert STATUS_MARKER in _body_of(target)


@pytest.mark.parametrize("hint", ["History", " status ", "IDENTITY"])
def test_valid_hint_tolerates_case_and_surrounding_space(tmp_path, hint):
    """Normalisation is part of the contract, not an accident of ``.lower()``."""
    src = _write_source(tmp_path, "sched", f"layer_hint: '{hint}'")

    results = layer_split.split_file(src)

    assert "layer_hint_ignored" not in results
    assert IDENTITY_MARKER in _body_of(results[hint.strip().lower()])


def test_absent_hint_is_not_reported_as_ignored(tmp_path, caplog):
    """A file with no hint at all is the common case and must stay quiet."""
    src = _write_source(tmp_path, "sched", None)

    with caplog.at_level(logging.WARNING, logger="palinode.consolidation.layer_split"):
        results = layer_split.split_file(src)

    assert "layer_hint_ignored" not in results
    assert _layer_split_records(caplog) == []
