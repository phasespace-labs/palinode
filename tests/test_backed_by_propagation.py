"""``backed_by`` propagation — retiring a source flags its dependents for review.

The correctness condition: for every ``backed_by`` edge (an extension edge), an
update to the source triggers evaluation of the dependent. Here "evaluation"
is a deterministic, idempotent ``stale_backing`` flag written by the executor
and the on-demand archive/retract paths — never a rewrite, never an
auto-retract — cleared by re-saving the dependent, and surfaced by ``lint``,
the review pass, the quality UI and the MCP search renderer.

Real git + real SQLite under ``tmp_path``; only the embedder is faked.
"""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

import frontmatter
import pytest

from palinode.consolidation import propagate
from palinode.consolidation.executor import apply_operations
from palinode.core.config import config

_FAKE_VECTOR = [0.05] * 1024
_PINNED_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    return list(_FAKE_VECTOR)


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """Git-backed tmp memory_dir with real SQLite, fake embedder, pinned clock."""
    from palinode.core import store

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", os.path.join(str(tmp_path), ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(propagate, "_utc_now", lambda: _PINNED_NOW)
    for d in ("insights", "projects", "decisions", "research"):
        os.makedirs(os.path.join(str(tmp_path), d), exist_ok=True)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_fake_embed):
        yield str(tmp_path)


_SOURCE_BODY = """# Alpha

- [2024-01-01] Throughput is 900 units per hour <!-- fact:f1 -->
- [2024-01-01] Latency is 20 ms <!-- fact:f2 -->
- [2024-01-01] Error rate is 0.1% <!-- fact:f3 -->
"""


def _write(base: str, rel: str, body: str, **meta) -> str:
    """Write a memory file with the given frontmatter; return its abs path."""
    import yaml

    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{fm}---\n\n{body}\n")
    return path


def _seed(base: str) -> dict[str, str]:
    """A source, two dependents citing it, one bystander citing something else."""
    paths = {
        "source": _write(base, "insights/alpha.md", _SOURCE_BODY,
                         id="insights-alpha", category="insights", type="Insight",
                         status="active"),
        "dep1": _write(base, "insights/beta.md", "Beta rests on alpha.",
                       id="insights-beta", category="insights", type="Insight",
                       status="active", backed_by=["insights/alpha"]),
        "dep2": _write(base, "decisions/gamma.md", "Gamma rests on alpha.",
                       id="decisions-gamma", category="decisions", type="Decision",
                       status="active", backed_by=["insights/alpha.md", "research/paper"]),
        "bystander": _write(base, "insights/delta.md", "Delta rests on something else.",
                            id="insights-delta", category="insights", type="Insight",
                            status="active", backed_by=["research/paper"]),
        "unlinked": _write(base, "insights/epsilon.md", "Epsilon cites nothing.",
                           id="insights-epsilon", category="insights", type="Insight",
                           status="active"),
    }
    subprocess.run(["git", "-C", base, "add", "-A"], check=True)
    subprocess.run(["git", "-C", base, "commit", "-q", "-m", "seed"], check=True)
    return paths


def _meta(path: str) -> dict:
    return frontmatter.load(path).metadata


def _body(path: str) -> str:
    return frontmatter.load(path).content


def _stale(path: str) -> list[dict]:
    return propagate.parse_stale_backing(_meta(path))


def _git_log(base: str) -> str:
    return subprocess.run(
        ["git", "-C", base, "log", "--format=%s"], capture_output=True, text=True, check=True
    ).stdout


def _git_dirty(base: str) -> str:
    return subprocess.run(
        ["git", "-C", base, "status", "--porcelain", "--", "*.md", "*/*.md"],
        capture_output=True, text=True, check=True,
    ).stdout


# ── executor: SUPERSEDE ───────────────────────────────────────────────────────


def test_supersede_flags_each_dependent_exactly_once(store_env):
    p = _seed(store_env)
    stats = apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "Throughput is 1200 units per hour",
         "reason": "remeasured"},
    ])
    assert stats["superseded"] == 1
    assert stats["review_flagged"] == 2

    for dep in ("dep1", "dep2"):
        entries = _stale(p[dep])
        assert len(entries) == 1, dep
        assert entries[0] == {
            "ref": "insights/alpha",
            "op": "supersede",
            "at": "2026-09-05T12:00:00+00:00",
            "facts": ["f1"],
            "reason": "remeasured",
        }
    # Flag only: body, status and the backed_by list itself are untouched.
    assert _body(p["dep1"]) == "Beta rests on alpha."
    assert _meta(p["dep1"])["status"] == "active"
    assert _meta(p["dep2"])["backed_by"] == ["insights/alpha.md", "research/paper"]


def test_unlinked_and_bystander_memories_are_untouched(store_env):
    p = _seed(store_env)
    before = {k: open(p[k], encoding="utf-8").read() for k in ("bystander", "unlinked")}
    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    for k, content in before.items():
        assert open(p[k], encoding="utf-8").read() == content, k
        assert _stale(p[k]) == []


def test_second_run_does_not_double_flag(store_env):
    p = _seed(store_env)
    op = [{"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"}]
    first = apply_operations(p["source"], op)
    assert first["review_flagged"] == 2
    dep_after_first = open(p["dep1"], encoding="utf-8").read()

    # SUPERSEDE is not a pure no-op on re-run (the tombstoned line still
    # matches), so the retirement fires again — and the flag must not.
    second = apply_operations(p["source"], op)
    assert second["superseded"] == 1
    assert second["review_flagged"] == 0
    assert open(p["dep1"], encoding="utf-8").read() == dep_after_first
    assert len(_stale(p["dep1"])) == 1

    # A different retirement of the same source is also absorbed: one entry
    # per source ref, however many times it is retired.
    third = apply_operations(p["source"], [{"op": "RETRACT", "id": "f2", "reason": "wrong"}])
    assert third["retracted"] == 1
    assert third["review_flagged"] == 0
    assert len(_stale(p["dep1"])) == 1


# ── executor: the other retiring ops, and the non-retiring ones ───────────────


def test_retract_archive_and_merge_all_propagate(store_env):
    cases = [
        ("retract", [{"op": "RETRACT", "id": "f1", "reason": "wrong"}], ["f1"]),
        ("archive", [{"op": "ARCHIVE", "id": "f2", "reason": "stale"}], ["f2"]),
        ("merge", [{"op": "MERGE", "ids": ["f1", "f3"], "new_text": "combined",
                    "rationale": "dup"}], ["f1", "f3"]),
    ]
    for kind, ops, facts in cases:
        base = os.path.join(store_env, f"run-{kind}")
        os.makedirs(base)
        subprocess.run(["git", "init", "-q", base], check=True)
        subprocess.run(["git", "-C", base, "config", "user.email", "t@t.test"], check=True)
        subprocess.run(["git", "-C", base, "config", "user.name", "test"], check=True)
        config.memory_dir = base
        p = _seed(base)
        stats = apply_operations(p["source"], ops)
        assert stats["review_flagged"] == 2, kind
        entry = _stale(p["dep1"])[0]
        assert entry["op"] == kind, kind
        assert entry["facts"] == facts, kind
        assert entry["ref"] == "insights/alpha"


def test_keep_and_update_do_not_propagate(store_env):
    p = _seed(store_env)
    stats = apply_operations(p["source"], [
        {"op": "KEEP", "id": "f1"},
        {"op": "UPDATE", "id": "f2", "new_text": "Latency is 25 ms"},
    ])
    assert stats["updated"] == 1
    assert stats["review_flagged"] == 0
    assert _stale(p["dep1"]) == [] and _stale(p["dep2"]) == []


def test_rejected_or_unmatched_retirement_does_not_propagate(store_env):
    p = _seed(store_env)
    stats = apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "no-such-fact", "new_text": "x", "reason": "r"},
        {"op": "ARCHIVE"},  # missing id
    ])
    assert stats["unmatched"] == 2
    assert stats["review_flagged"] == 0
    assert _stale(p["dep1"]) == []


def test_multiple_retirements_in_one_call_write_one_entry(store_env):
    p = _seed(store_env)
    stats = apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "n1", "reason": "first"},
        {"op": "RETRACT", "id": "f2", "reason": "second"},
        {"op": "ARCHIVE", "id": "f3", "reason": "first"},
    ])
    assert stats["review_flagged"] == 2
    entry = _stale(p["dep1"])[0]
    assert entry["op"] == "supersede, archive, retract"
    assert entry["facts"] == ["f1", "f2", "f3"]
    assert entry["reason"] == "first; second"


# ── determinism, provenance, index ────────────────────────────────────────────


def test_same_ops_same_output(store_env):
    ops = [{"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"}]
    outputs = []
    for run in ("a", "b"):
        base = os.path.join(store_env, run)
        os.makedirs(base)
        subprocess.run(["git", "init", "-q", base], check=True)
        subprocess.run(["git", "-C", base, "config", "user.email", "t@t.test"], check=True)
        subprocess.run(["git", "-C", base, "config", "user.name", "test"], check=True)
        config.memory_dir = base
        p = _seed(base)
        apply_operations(p["source"], ops)
        outputs.append((
            open(p["dep1"], encoding="utf-8").read(),
            open(p["dep2"], encoding="utf-8").read(),
        ))
    assert outputs[0] == outputs[1]


def test_flag_is_committed_with_provenance(store_env):
    p = _seed(store_env)
    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    log = _git_log(store_env)
    assert f"{config.git.commit_prefix} backed_by review: insights/alpha supersede -> 2 dependent(s)" in log
    # The dependents are clean in the working tree — the flag landed in git,
    # not just on disk. (The source itself is the runner's to commit.)
    dirty = _git_dirty(store_env)
    assert "beta.md" not in dirty and "gamma.md" not in dirty


def test_flag_reaches_search_metadata(store_env):
    from palinode.core import store
    from palinode.indexer.index_file import index_file

    p = _seed(store_env)
    index_file(p["dep1"])
    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    hits = store.search(_FAKE_VECTOR, top_k=10, threshold=0.0)
    beta = [h for h in hits if h["file_path"].endswith("insights/beta.md")]
    assert beta, hits
    assert beta[0]["metadata"]["stale_backing"][0]["ref"] == "insights/alpha"


# ── the on-demand paths ───────────────────────────────────────────────────────


def test_archive_memory_propagates(store_env):
    from palinode.consolidation.archive import archive_memory

    p = _seed(store_env)
    res = archive_memory("insights/alpha.md", reason="obsolete")
    assert res["status"] == "archived"
    assert sorted(res["review_flagged"]) == ["decisions/gamma.md", "insights/beta.md"]
    entry = _stale(p["dep1"])[0]
    assert entry["op"] == "archive"
    assert entry["reason"] == "Archived: insights/alpha.md (reason: obsolete)"
    assert "facts" not in entry  # a whole-file retirement names no fact ids

    # already_archived is a no-op end to end, propagation included.
    again = archive_memory("insights/alpha.md", reason="obsolete")
    assert again["status"] == "already_archived"
    assert "review_flagged" not in again
    assert len(_stale(p["dep1"])) == 1


def test_archive_memory_supersede_records_the_successor(store_env):
    from palinode.consolidation.archive import archive_memory

    p = _seed(store_env)
    _write(store_env, "insights/alpha-v2.md", "The new alpha.", id="insights-alpha-v2",
           category="insights", type="Insight", status="active")
    res = archive_memory("insights/alpha.md", superseded_by="insights/alpha-v2.md")
    assert len(res["review_flagged"]) == 2
    entry = _stale(p["dep2"])[0]
    assert entry["op"] == "supersede"
    assert entry["reason"] == "Superseded by insights/alpha-v2.md"


def test_retract_mentions_propagates_with_opaque_reason(store_env):
    from palinode.consolidation.retract import retract_mentions

    p = _seed(store_env)
    with open(p["source"], "w", encoding="utf-8") as f:
        f.write(
            "---\nid: insights-alpha\ncategory: insights\ntype: Insight\nstatus: active\n---\n\n"
            "Alpha prefers Vim for editing. The team uses tabs.\n"
        )
    res = retract_mentions("insights/alpha.md", "Alpha prefers Vim")
    assert res["status"] == "retracted", res
    assert sorted(res["review_flagged"]) == ["decisions/gamma.md", "insights/beta.md"]
    entry = _stale(p["dep1"])[0]
    assert entry["op"] == "retract"
    # The pref text must never appear in the flag — same rule as the marker.
    assert "Vim" not in entry["reason"]
    assert entry["reason"] == f"retracted 1 mention(s) r:{res['retraction_id']}"


# ── clearing, lookup edge cases ───────────────────────────────────────────────


def test_re_save_of_dependent_clears_the_flag(store_env):
    from palinode.core.save import save_memory

    p = _seed(store_env)
    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    assert len(_stale(p["dep1"])) == 1

    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        out = save_memory(
            content="Beta rests on alpha, re-verified against the new figure.",
            type="Insight", slug="beta", backed_by=["insights/alpha"],
        )
    assert out["file_path"].endswith("insights/beta.md")
    meta = _meta(p["dep1"])
    assert "stale_backing" not in meta
    assert meta["backed_by"] == ["insights/alpha"]

    # And the next retirement of the source flags it afresh: the re-save was a
    # re-verification against the source as it stood then, not a permanent
    # exemption. Only beta is newly written — gamma still carries its first
    # flag (one entry per source), so it is absorbed.
    stats = apply_operations(p["source"], [{"op": "RETRACT", "id": "f2", "reason": "w"}])
    assert stats["review_flagged"] == 1
    assert _stale(p["dep1"])[0]["op"] == "retract"
    assert _stale(p["dep2"])[0]["op"] == "supersede"


def test_archived_dependents_and_history_siblings_are_skipped(store_env):
    p = _seed(store_env)
    _write(store_env, "insights/old.md", "Old, archived, cites alpha.", id="insights-old",
           category="insights", type="Insight", status="archived", backed_by=["insights/alpha"])
    _write(store_env, "insights/alpha-history.md", "- history", category="history",
           status="archived", backed_by=["insights/alpha"])
    stats = apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    assert stats["review_flagged"] == 2
    assert _stale(os.path.join(store_env, "insights/old.md")) == []
    assert _stale(os.path.join(store_env, "insights/alpha-history.md")) == []


def test_status_layer_matches_its_base_ref(store_env):
    p = _seed(store_env)
    status_layer = _write(store_env, "insights/alpha-status.md", _SOURCE_BODY,
                          id="insights-alpha", category="insights", type="Insight")
    stats = apply_operations(status_layer, [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    assert stats["review_flagged"] == 2
    assert _stale(p["dep1"])[0]["ref"] == "insights/alpha"


def test_one_hop_only(store_env):
    """C depends on B depends on A: retiring A flags B, not C."""
    p = _seed(store_env)
    c = _write(store_env, "insights/zeta.md", "Zeta rests on beta.", id="insights-zeta",
               category="insights", type="Insight", status="active", backed_by=["insights/beta"])
    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    assert len(_stale(p["dep1"])) == 1
    assert _stale(c) == []


def test_source_outside_memory_dir_has_no_dependents(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path / "mem"))
    assert propagate.source_refs_for(str(tmp_path / "elsewhere" / "a.md")) == []


def test_malformed_stale_backing_is_read_soft(store_env):
    p = _seed(store_env)
    with open(p["dep1"], "w", encoding="utf-8") as f:
        f.write("---\nid: insights-beta\nbacked_by: [insights/alpha]\n"
                "stale_backing: not-a-list\n---\n\nBeta.\n")
    assert _stale(p["dep1"]) == []
    # A malformed field is replaced by a well-formed one, not appended to.
    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    assert len(_stale(p["dep1"])) == 1


# ── surfaces: lint, review, MCP search ────────────────────────────────────────


def test_lint_reports_pending_reviews_until_cleared(store_env):
    from palinode.core.lint import run_lint_pass

    p = _seed(store_env)
    assert run_lint_pass()["stale_backing"] == []

    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    findings = run_lint_pass()["stale_backing"]
    assert sorted(f["file"] for f in findings) == ["decisions/gamma.md", "insights/beta.md"]
    assert findings[0]["stale_backing"][0]["ref"] == "insights/alpha"

    # An archived dependent that still carries an old flag is not reported.
    from palinode.consolidation.archive import archive_memory
    archive_memory("insights/beta.md", reason="done")
    assert [f["file"] for f in run_lint_pass()["stale_backing"]] == ["decisions/gamma.md"]


def test_lint_cli_text_lists_stale_backing(store_env, monkeypatch):
    from click.testing import CliRunner

    from palinode.cli.lint import api_client, lint as lint_command
    from palinode.core.lint import run_lint_pass

    p = _seed(store_env)
    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    # The client takes the propose/apply options; this stub ignores them.
    monkeypatch.setattr(api_client, "lint", lambda **_kwargs: run_lint_pass())
    result = CliRunner().invoke(lint_command, ["--format", "text"])
    assert result.exit_code == 0, result.output
    assert "Stale Backing (2)" in result.output, result.output
    assert "insights/beta.md backed by: insights/alpha (supersede)" in result.output


def test_review_proposes_an_update_for_stale_backing(store_env):
    from palinode.core.review import run_review

    p = _seed(store_env)
    apply_operations(p["source"], [
        {"op": "SUPERSEDE", "id": "f1", "new_text": "new", "reason": "r"},
    ])
    out = run_review(None)
    files = sorted(f["file"] for f in out["findings"]["stale_backing"])
    assert files == ["decisions/gamma.md", "insights/beta.md"]
    ops = [o for o in out["proposed_ops"] if "Backing withdrawn" in o["reason"]]
    assert {o["file"] for o in ops} == set(files)
    assert all(o["op"] == "PROPOSE_UPDATE" for o in ops)
    assert "insights/alpha (supersede)" in ops[0]["reason"]


def test_mcp_search_renders_stale_backing():
    from palinode.mcp import _format_results

    result = {
        "file_path": "/store/insights/beta.md", "score": 0.9,
        "snippet": "Beta rests on alpha.",
        "metadata": {
            "backed_by": ["insights/alpha"],
            "stale_backing": [{"ref": "insights/alpha", "op": "supersede"}],
        },
    }
    out = _format_results([result])
    assert "stale backing: insights/alpha" in out
    # Malformed values never break rendering.
    for bad in ("insights/alpha", None, 42, [{"op": "x"}], ["str"]):
        result["metadata"]["stale_backing"] = bad
        assert "Beta rests on alpha" in _format_results([result])
