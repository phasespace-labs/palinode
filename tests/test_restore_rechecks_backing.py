"""Restore re-checks its own ``backed_by`` sources.

``backed_by`` propagation deliberately skips archived dependents (they assert
nothing in recall), so a source retired *while* a dependent was archived
leaves no ``stale_backing`` flag on it. ``restore_memory`` closes that gap
from the other side: on the way back into recall, each of the restored
memory's own sources is checked against its current state and every one no
longer active is flagged under ``op: restore-check`` — same entry shape, same
writer, same idempotency-per-ref rule, in the restore's own commit.

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
from palinode.consolidation.archive import archive_memory, restore_memory
from palinode.core.config import config

_FAKE_VECTOR = [0.05] * 1024
_PINNED_NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)


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
    for d in ("insights", "decisions", "research"):
        os.makedirs(os.path.join(str(tmp_path), d), exist_ok=True)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_fake_embed):
        yield str(tmp_path)


def _write(base: str, rel: str, body: str, **meta) -> str:
    import yaml

    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{fm}---\n\n{body}\n")
    return path


def _seed(base: str) -> dict[str, str]:
    """A source, a dependent citing it, a dependent also citing a missing ref."""
    paths = {
        "source": _write(base, "insights/alpha.md", "Alpha holds.",
                         id="insights-alpha", category="insights", type="Insight",
                         status="active"),
        "dep": _write(base, "insights/beta.md", "Beta rests on alpha.",
                      id="insights-beta", category="insights", type="Insight",
                      status="active", created_at="2026-01-01T00:00:00+00:00",
                      backed_by=["insights/alpha"]),
        "dep2": _write(base, "decisions/gamma.md", "Gamma rests on alpha and a paper.",
                       id="decisions-gamma", category="decisions", type="Decision",
                       status="active", backed_by=["insights/alpha.md", "research/paper"]),
    }
    subprocess.run(["git", "-C", base, "add", "-A"], check=True)
    subprocess.run(["git", "-C", base, "commit", "-q", "-m", "seed"], check=True)
    return paths


def _meta(path: str) -> dict:
    return frontmatter.load(path).metadata


def _stale(path: str) -> list[dict]:
    return propagate.parse_stale_backing(_meta(path))


def _git_log(base: str) -> str:
    return subprocess.run(
        ["git", "-C", base, "log", "--format=%s"], capture_output=True, text=True, check=True
    ).stdout


def _git_head_files(base: str) -> list[str]:
    return subprocess.run(
        ["git", "-C", base, "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.split()


def _git_dirty(base: str) -> str:
    """Uncommitted memory files (the SQLite store under tmp_path is not one)."""
    return subprocess.run(
        ["git", "-C", base, "status", "--porcelain", "--", "*.md", "*/*.md"],
        capture_output=True, text=True, check=True,
    ).stdout


# ── the gap, closed ───────────────────────────────────────────────────────────


def test_source_superseded_while_dependent_archived_is_flagged_on_restore(store_env):
    _write(store_env, "insights/alpha-v2.md", "The new alpha.", id="insights-alpha-v2",
           category="insights", type="Insight", status="active")
    p = _seed(store_env)

    assert archive_memory("insights/beta.md", reason="parked")["status"] == "archived"
    retired = archive_memory("insights/alpha.md", superseded_by="insights/alpha-v2.md")
    # Propagation skipped the archived dependent — the gap this closes.
    assert retired["review_flagged"] == ["decisions/gamma.md"]
    assert _stale(p["dep"]) == []

    out = restore_memory("insights/beta.md", reason="needed again")

    assert out["status"] == "active"
    assert out["stale_backing"] == ["insights/alpha"]
    assert out["committed"] is True
    assert _stale(p["dep"]) == [{
        "ref": "insights/alpha",
        "op": "restore-check",
        "at": "2026-09-06T12:00:00+00:00",
        "reason": "insights/alpha is archived, superseded by insights/alpha-v2.md",
    }]

    # Restore semantics are untouched: status, provenance, the carried-over
    # frontmatter and the body are exactly what a restore without the flag gives.
    meta = _meta(p["dep"])
    assert meta["status"] == "active"
    assert "superseded_by" not in meta
    assert meta["restored_from"] == "archived"
    assert meta["created_at"] == "2026-01-01T00:00:00+00:00"
    assert meta["backed_by"] == ["insights/alpha"]
    assert frontmatter.load(p["dep"]).content.strip() == "Beta rests on alpha."

    # In the restore's own commit — the only `backed_by review` commit is the
    # supersede's (for gamma); the restore adds no second provenance commit
    # and leaves nothing dirty.
    log = _git_log(store_env)
    assert log.splitlines()[0] == f"{config.git.commit_prefix} restore: insights/beta.md"
    assert log.count("backed_by review") == 1
    assert log.index("restore:") < log.index("backed_by review")  # newest first
    assert sorted(_git_head_files(store_env)) == ["insights/beta-history.md", "insights/beta.md"]
    assert _git_dirty(store_env) == ""

    history = open(os.path.join(store_env, out["history_file"]), encoding="utf-8").read()
    assert ("Restored: insights/beta.md (was archived) (reason: needed again); "
            "stale backing: insights/alpha") in history


def test_lint_reports_the_restore_flag(store_env):
    from palinode.core.lint import run_lint_pass

    p = _seed(store_env)
    archive_memory("insights/beta.md")
    archive_memory("insights/alpha.md", reason="obsolete")
    assert [f["file"] for f in run_lint_pass()["stale_backing"]] == ["decisions/gamma.md"]

    restore_memory("insights/beta.md")

    findings = {f["file"]: f["stale_backing"] for f in run_lint_pass()["stale_backing"]}
    assert sorted(findings) == ["decisions/gamma.md", "insights/beta.md"]
    assert findings["insights/beta.md"] == _stale(p["dep"])
    assert findings["insights/beta.md"][0]["op"] == "restore-check"
    assert findings["insights/beta.md"][0]["reason"] == "insights/alpha is archived"


def test_flag_reaches_search_metadata(store_env):
    from palinode.core import store
    from palinode.indexer.index_file import index_file

    p = _seed(store_env)
    index_file(p["dep"])
    archive_memory("insights/beta.md")
    archive_memory("insights/alpha.md")

    restore_memory("insights/beta.md")

    hits = store.search(_FAKE_VECTOR, top_k=10, threshold=0.0)
    beta = [h for h in hits if h["file_path"].endswith("insights/beta.md")]
    assert beta, hits
    assert beta[0]["metadata"]["status"] == "active"
    assert beta[0]["metadata"]["stale_backing"][0]["ref"] == "insights/alpha"
    assert beta[0]["metadata"]["stale_backing"][0]["op"] == "restore-check"


# ── idempotency ───────────────────────────────────────────────────────────────


def test_second_restore_cycle_does_not_double_flag(store_env):
    p = _seed(store_env)
    archive_memory("insights/beta.md")
    archive_memory("insights/alpha.md")
    first = restore_memory("insights/beta.md")
    assert first["stale_backing"] == ["insights/alpha"]

    # A restore of a live memory is the reported no-op it always was.
    again = restore_memory("insights/beta.md")
    assert again["status"] == "not_archived"
    assert "stale_backing" not in again
    assert len(_stale(p["dep"])) == 1

    # A full archive -> restore cycle finds the ref already flagged.
    archive_memory("insights/beta.md")
    third = restore_memory("insights/beta.md")
    assert third["status"] == "active"
    assert third["stale_backing"] == []
    assert len(_stale(p["dep"])) == 1
    assert _stale(p["dep"])[0]["op"] == "restore-check"


def test_already_flagged_by_propagation_is_not_flagged_again(store_env):
    """Retired before archival: propagation already flagged it, restore adds nothing."""
    p = _seed(store_env)
    archive_memory("insights/alpha.md", reason="first")
    assert _stale(p["dep"])[0]["op"] == "archive"
    archive_memory("insights/beta.md")

    out = restore_memory("insights/beta.md")

    assert out["stale_backing"] == []
    entries = _stale(p["dep"])
    assert len(entries) == 1 and entries[0]["op"] == "archive"


# ── no flag when nothing changed ──────────────────────────────────────────────


def test_restore_with_live_sources_writes_no_flag(store_env):
    p = _seed(store_env)
    archive_memory("insights/beta.md")

    out = restore_memory("insights/beta.md")

    assert out["stale_backing"] == []
    assert "stale_backing" not in _meta(p["dep"])
    history = open(os.path.join(store_env, out["history_file"]), encoding="utf-8").read()
    assert "stale backing" not in history


def test_memory_without_backed_by_is_untouched(store_env):
    _seed(store_env)
    archive_memory("insights/alpha.md")
    out = restore_memory("insights/alpha.md")
    assert out["stale_backing"] == []
    assert "stale_backing" not in _meta(os.path.join(store_env, "insights/alpha.md"))


# ── which sources count as no longer active ───────────────────────────────────


def test_missing_source_is_flagged_and_live_source_is_not(store_env):
    p = _seed(store_env)
    archive_memory("decisions/gamma.md")

    out = restore_memory("decisions/gamma.md")

    assert out["stale_backing"] == ["research/paper"]
    entries = _stale(p["dep2"])
    assert [e["ref"] for e in entries] == ["research/paper"]
    assert entries[0]["reason"] == "research/paper is missing"


def test_status_layer_answers_for_its_base_ref(store_env):
    """``insights/alpha`` with only ``alpha-status.md`` on disk is present, not missing."""
    p = _seed(store_env)
    os.remove(p["source"])
    _write(store_env, "insights/alpha-status.md", "Alpha, status layer.", id="insights-alpha",
           category="insights", type="Insight", status="active")
    archive_memory("insights/beta.md")
    assert restore_memory("insights/beta.md")["stale_backing"] == []

    archive_memory("insights/beta.md")
    archive_memory("insights/alpha-status.md")
    assert restore_memory("insights/beta.md")["stale_backing"] == ["insights/alpha"]


def test_escaping_or_unreadable_refs_are_not_retirement_signals(store_env):
    _seed(store_env)
    _write(store_env, "insights/zeta.md", "Zeta cites oddities.", id="insights-zeta",
           category="insights", type="Insight", status="active",
           backed_by=["../outside", "insights/broken"])
    with open(os.path.join(store_env, "insights/broken.md"), "w", encoding="utf-8") as f:
        f.write("---\n: [unclosed\n---\nbroken frontmatter\n")
    archive_memory("insights/zeta.md")

    out = restore_memory("insights/zeta.md")

    assert out["stale_backing"] == []
    assert out["status"] == "active"


def test_entry_builder_accepts_the_restore_op_alone():
    entry = propagate.build_entry("insights/alpha.md", ops=["restore-check"], reason="r")
    assert entry["ref"] == "insights/alpha" and entry["op"] == "restore-check"
    with pytest.raises(ValueError):
        propagate.build_entry("insights/alpha", ops=["undo"])


# ── renderers ─────────────────────────────────────────────────────────────────


def test_cli_text_names_the_flagged_sources():
    import importlib

    from click.testing import CliRunner

    # The package's `restore` attribute is the click Command, not the module.
    mod = importlib.import_module("palinode.cli.restore")

    payload = {"file": "insights/x.md", "status": "active", "restored_from": "archived",
               "history_file": "insights/x-history.md", "chunks_updated": 1,
               "committed": True, "stale_backing": ["insights/a", "insights/b"]}

    class _FakeAPI:
        def restore(self, file_path, reason=None):
            return payload

    with patch.object(mod, "api_client", _FakeAPI()):
        res = CliRunner().invoke(mod.restore, ["insights/x.md", "--format", "text"])
    assert res.exit_code == 0, res.output
    assert "stale backing flagged (source no longer active): insights/a, insights/b" in res.output


@pytest.mark.asyncio
async def test_mcp_text_names_the_flagged_sources(monkeypatch):
    import palinode.mcp as mcp

    class _Resp:
        status_code = 200

        def json(self):
            return {"file": "insights/x.md", "status": "active", "restored_from": "archived",
                    "history_file": "insights/x-history.md", "chunks_updated": 1,
                    "stale_backing": ["insights/a"]}

    async def _fake_post(p, json=None, timeout=30.0):
        return _Resp()

    monkeypatch.setattr(mcp, "_post", _fake_post)
    result = await mcp._dispatch_tool("palinode_restore", {"file_path": "insights/x.md"})
    assert "Stale backing flagged (source no longer active): insights/a" in result[0].text
