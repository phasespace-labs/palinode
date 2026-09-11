"""The lint→consolidation loop: deterministic findings become proposed ops.

Covers the mapping (which finding becomes which op, and with what rationale),
the document classes age-based ARCHIVE must never touch, the dry-run default,
the ``--apply`` path through the existing deterministic writers with a ``lint``
actor on the history line and the commit, idempotency, and the three surfaces.

Real SQLite and real git under ``tmp_path``; only the embedder and the security
scanner are patched (repo rule: never mock the database).
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import frontmatter
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from palinode.consolidation import propose_from_lint as propose_mod
from palinode.core.config import config
from palinode.core.lint import run_lint_pass

EMBED_DIM = 1024
_FAKE_VECTOR = [0.05] * EMBED_DIM


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _write(base, rel, *, days_old=200, status="active", extra=None, body="A fact."):
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": rel.replace("/", "-").replace(".md", ""),
        "type": "Insight",
        "category": path.parent.name,
        "description": "a description",
        "entities": ["project/alpha"],
        "created_at": _iso(days_old),
        "last_updated": _iso(days_old),
        "status": status,
    }
    meta.update(extra or {})
    post = frontmatter.Post(body, **meta)
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def memdir(tmp_path, monkeypatch):
    """Git-backed tmp memory dir with a real (empty) SQLite store."""
    from palinode.core import store

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", os.path.join(str(tmp_path), ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=lambda *a, **k: list(_FAKE_VECTOR)):
        yield tmp_path


def _proposals(memdir, **kwargs):
    return propose_mod.propose_from_lint(run_lint_pass(), **kwargs)


def _for(result, check):
    return [p for p in result["proposals"] if p["finding"]["check"] == check]


# ── the mapping ──────────────────────────────────────────────────────────────


def test_stale_insight_becomes_an_archive_op_carrying_its_finding(memdir):
    _write(memdir, "insights/rotting.md", days_old=400)

    result = _proposals(memdir)
    archives = _for(result, "stale_files")

    assert len(archives) == 1
    op = archives[0]
    assert op["op"] == "ARCHIVE"
    assert op["file"] == "insights/rotting.md"
    assert op["scope"] == "document"
    assert op["applicable"] is True
    assert op["source"] == "lint"
    # The finding travels with the op, not just the verdict.
    assert op["finding"]["check"] == "stale_files"
    assert op["finding"]["detail"]["days_old"] >= 400
    assert "400" in op["rationale"] or str(op["finding"]["detail"]["days_old"]) in op["rationale"]


def test_stale_backing_becomes_an_advisory_propose_update(memdir):
    _write(
        memdir,
        "insights/dependent.md",
        days_old=1,
        extra={"stale_backing": [{"ref": "decisions/old", "op": "archive"}]},
    )

    op = _for(_proposals(memdir), "stale_backing")[0]
    assert op["op"] == "PROPOSE_UPDATE"
    assert op["applicable"] is False
    assert op["blocked_by"] == "advisory"
    assert "decisions/old (archive)" in op["rationale"]


def test_orphan_becomes_an_advisory_propose_update_not_an_update(memdir):
    _write(memdir, "insights/lonely.md", days_old=1, extra={"entities": []})

    ops = _for(_proposals(memdir), "orphaned_files")
    assert [o["op"] for o in ops] == ["PROPOSE_UPDATE"]
    assert ops[0]["applicable"] is False
    # The refusal is the contract: which entity it belongs to is judgement.
    assert "judgement" in ops[0]["rationale"]


def test_resolvable_relative_date_becomes_an_update_naming_phrase_and_anchor(memdir):
    _write(
        memdir,
        "insights/drifting.md",
        days_old=0,
        body="- Yesterday we chose SQLite. <!-- fact:f1 -->",
    )
    anchor = datetime.now(timezone.utc).date()
    expected = (anchor - timedelta(days=1)).isoformat()

    ops = _for(_proposals(memdir), "relative_dates")

    assert len(ops) == 1
    op = ops[0]
    assert op["op"] == "UPDATE"
    assert op["file"] == "insights/drifting.md"
    assert op["id"] == "f1"
    # The rewrite is the arithmetic, not a reworded fact.
    assert op["new_text"] == f"On {expected} we chose SQLite."
    assert op["applicable"] is True
    assert "'Yesterday'" in op["rationale"]
    assert anchor.isoformat() in op["rationale"]
    assert op["finding"]["detail"]["resolved"] == [expected]


def test_vague_relative_date_is_skipped_with_its_reason_not_proposed(memdir):
    _write(
        memdir,
        "insights/vague.md",
        days_old=1,
        body="- We shipped it last week. <!-- fact:f1 -->",
    )

    result = _proposals(memdir)
    assert _for(result, "relative_dates") == []
    skipped = [s for s in result["skipped"] if s["check"] == "relative_dates"]
    assert skipped and "'last week'" in skipped[0]["reason"]
    assert "interval" in skipped[0]["reason"]


def test_relative_date_outside_a_fact_line_is_skipped_not_guessed_at(memdir):
    # UPDATE addresses a fact by its marker; prose has nothing to aim at.
    _write(memdir, "insights/prose.md", days_old=0, body="Yesterday we chose SQLite.")

    result = _proposals(memdir)
    assert _for(result, "relative_dates") == []
    skipped = [s for s in result["skipped"] if s["check"] == "relative_dates"]
    assert skipped and "no <!-- fact:" in skipped[0]["reason"]


def test_relative_date_inside_quoted_text_is_reported_but_never_rewritten(memdir):
    _write(
        memdir,
        "insights/quoted.md",
        days_old=0,
        body='- She said "yesterday it broke". <!-- fact:f1 -->',
    )

    result = _proposals(memdir)
    assert _for(result, "relative_dates") == []
    skipped = [s for s in result["skipped"] if s["check"] == "relative_dates"]
    assert skipped and "quoted text or code" in skipped[0]["reason"]


def test_update_honours_the_consolidation_allowed_ops_restriction(memdir, monkeypatch):
    monkeypatch.setattr(
        config.consolidation, "allowed_ops", ["KEEP", "ARCHIVE"]
    )
    _write(
        memdir,
        "insights/drifting.md",
        days_old=0,
        body="- Yesterday we chose SQLite. <!-- fact:f1 -->",
    )

    op = _for(_proposals(memdir), "relative_dates")[0]
    assert op["applicable"] is False
    assert op["blocked_by"] == "consolidation.allowed_ops"


def test_deep_contradiction_becomes_propose_contradicts_on_both_sides(memdir):
    _write(memdir, "decisions/a.md", days_old=1)
    _write(memdir, "decisions/b.md", days_old=1)
    deep = {
        "contradictions": [
            {
                "file_a": "decisions/a.md",
                "file_b": "decisions/b.md",
                "similarity": 0.91,
                "llm_explanation": "one says yes, the other no",
            }
        ]
    }

    ops = _for(_proposals(memdir, deep_contradictions=deep), "deep_contradictions")

    assert {o["file"] for o in ops} == {"decisions/a.md", "decisions/b.md"}
    assert all(o["op"] == "PROPOSE_CONTRADICTS" for o in ops)
    assert all(o["applicable"] for o in ops)
    by_file = {o["file"]: o for o in ops}
    assert by_file["decisions/a.md"]["contradicts"] == ["decisions/b"]
    assert by_file["decisions/b.md"]["contradicts"] == ["decisions/a"]
    assert "one says yes, the other no" in by_file["decisions/a.md"]["rationale"]


def test_deep_contradiction_already_recorded_is_not_reproposed(memdir):
    _write(memdir, "decisions/a.md", days_old=1, extra={"contradicts": ["decisions/b"]})
    _write(memdir, "decisions/b.md", days_old=1, extra={"contradicts": ["decisions/a"]})
    deep = {"contradictions": [{"file_a": "decisions/a.md", "file_b": "decisions/b.md",
                               "similarity": 0.91, "llm_explanation": ""}]}

    assert _for(_proposals(memdir, deep_contradictions=deep), "deep_contradictions") == []


# ── the exclusions (ADR-020) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel, extra, reason",
    [
        # The ADR-020 invariant, straight from retirement.classify() — the
        # recorded reason names the signal that classified the document, so a
        # skip says *why* it is protected, not merely that it is.
        ("people/ada.md", {}, "retirement_policy: superseded-only (path:people/)"),
        (
            "projects/alpha.md",
            {},
            "retirement_policy: superseded-only (path:projects/ profile document)",
        ),
        (
            "insights/identity.md",
            {"type": "PersonMemory"},
            "retirement_policy: superseded-only (type:PersonMemory)",
        ),
        (
            "insights/declared.md",
            {"retirement_policy": "superseded-only"},
            "retirement_policy: superseded-only (declared:retirement_policy)",
        ),
        (
            "insights/living.md",
            {"update_policy": "replace"},
            "retirement_policy: superseded-only (update_policy:replace)",
        ),
        (
            "insights/promoted.md",
            {"core": True},
            "retirement_policy: superseded-only (core:true)",
        ),
        # Proposal-side conservatism, not the invariant: ADR-020 calls the
        # decision regime "conservative", not forbidden, so the executor would
        # apply this ARCHIVE — the proposer simply declines to nominate it.
        (
            "decisions/keep-it.md",
            {},
            "proposer: conservative class (decisions/)",
        ),
    ],
)
def test_excluded_document_classes_are_never_age_archived(memdir, rel, extra, reason):
    _write(memdir, rel, days_old=900, extra=extra)

    result = _proposals(memdir)

    assert _for(result, "stale_files") == []
    skipped = [s for s in result["skipped"] if s["file"] == rel]
    assert skipped, f"{rel} was neither proposed nor recorded as skipped"
    assert "no ARCHIVE proposed" in skipped[0]["reason"]
    # Which layer excluded it, not just that something did.
    assert reason in skipped[0]["reason"]


def test_a_stale_status_document_is_proposed_for_archive(memdir):
    """``projects/<slug>-status.md`` ages — ADR-020's one explicitly allowed regime."""
    _write(memdir, "projects/alpha-status.md", days_old=400)

    archives = _for(_proposals(memdir), "stale_files")

    assert [op["file"] for op in archives] == ["projects/alpha-status.md"]
    assert archives[0]["op"] == "ARCHIVE"
    assert archives[0]["applicable"] is True


def test_a_declared_age_eligible_person_document_becomes_proposable(memdir):
    """The frontmatter override runs end to end, in the direction that opens up."""
    _write(memdir, "people/ada.md", days_old=400, extra={"retirement_policy": "age-eligible"})

    result = _proposals(memdir)

    assert [op["file"] for op in _for(result, "stale_files")] == ["people/ada.md"]
    assert [s for s in result["skipped"] if s["file"] == "people/ada.md"] == []


def test_no_archive_is_ever_proposed_against_a_superseded_only_document(memdir):
    """The invariant, over a mixed store: propose ARCHIVE ⇒ not superseded-only.

    The proposer may be *stricter* than the classifier (``decisions/`` is), and
    that is a proposer choice. It may never be looser — a proposal the executor
    is obliged to reject is a bug in the proposer, not a policy difference.
    """
    from palinode.consolidation.retirement import is_superseded_only

    docs = [
        ("people/ada.md", {}),
        ("people/grace.md", {"retirement_policy": "age-eligible"}),
        ("projects/alpha.md", {}),
        ("projects/alpha-status.md", {}),
        ("projects/beta-status.md", {"retirement_policy": "superseded-only"}),
        ("decisions/keep-it.md", {}),
        ("insights/rotting.md", {}),
        ("insights/identity.md", {"type": "PersonMemory"}),
        ("insights/personal.md", {"category": "person"}),
        ("insights/living.md", {"update_policy": "replace"}),
        ("insights/promoted.md", {"core": True}),
        ("insights/declared.md", {"retirement_policy": "superseded-only"}),
        ("daily/2024-01-01.md", {}),
        ("research/old-notes.md", {}),
    ]
    for rel, extra in docs:
        _write(memdir, rel, days_old=400, extra=extra)

    result = _proposals(memdir)
    archived = {op["file"] for op in _for(result, "stale_files")}

    assert archived, "the fixture must produce at least one ARCHIVE to be meaningful"
    for rel in archived:
        meta = frontmatter.load(memdir / rel).metadata
        assert not is_superseded_only(str(memdir / rel), meta), (
            f"{rel} was proposed for age-based ARCHIVE but the executor's own "
            f"retirement guard would reject it"
        )
    # Every document is accounted for: proposed or skipped with a reason.
    assert archived | {s["file"] for s in result["skipped"]} == {rel for rel, _ in docs}


def test_archive_is_blocked_when_the_operator_removed_it_from_allowed_ops(
    memdir, monkeypatch
):
    monkeypatch.setattr(config.consolidation, "allowed_ops", ["KEEP", "UPDATE"])
    _write(memdir, "insights/rotting.md", days_old=400)

    op = _for(_proposals(memdir), "stale_files")[0]
    assert op["applicable"] is False
    assert op["blocked_by"] == "consolidation.allowed_ops"


# ── dry run vs apply ─────────────────────────────────────────────────────────


def _head(memdir):
    return subprocess.run(
        ["git", "-C", str(memdir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()


def _commit_all(memdir, message="seed"):
    subprocess.run(["git", "-C", str(memdir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(memdir), "commit", "-q", "-m", message], check=True)


def test_dry_run_writes_nothing_and_commits_nothing(memdir):
    path = _write(memdir, "insights/rotting.md", days_old=400)
    _commit_all(memdir)
    before_head, before_text = _head(memdir), path.read_text(encoding="utf-8")

    out = propose_mod.attach_proposals(run_lint_pass())

    assert out["proposals"]["dry_run"] is True
    assert "applied" not in out["proposals"]
    assert path.read_text(encoding="utf-8") == before_text
    assert _head(memdir) == before_head
    assert not (memdir / "insights" / "rotting-history.md").exists()


def test_apply_archives_through_the_executor_path_with_a_lint_actor(memdir):
    path = _write(memdir, "insights/rotting.md", days_old=400)
    _commit_all(memdir)

    out = propose_mod.attach_proposals(run_lint_pass(), apply=True)

    assert out["proposals"]["dry_run"] is False
    applied = out["proposals"]["applied"]
    assert applied["source"] == "lint"
    assert [a["op"] for a in applied["applied"]] == ["ARCHIVE"]
    assert applied["failed"] == []

    # The document is retired the sanctioned way…
    assert frontmatter.loads(path.read_text(encoding="utf-8")).get("status") == "archived"
    # …with the audit sibling naming the actor…
    history = (memdir / "insights" / "rotting-history.md").read_text(encoding="utf-8")
    assert "[actor: lint]" in history
    assert "lint: status is active" in history
    # …and the commit saying who proposed it.
    subject = subprocess.run(
        ["git", "-C", str(memdir), "log", "-1", "--format=%s"],
        capture_output=True, text=True,
    ).stdout
    assert "(actor: lint)" in subject


def test_apply_records_contradicts_through_the_executor(memdir):
    _write(memdir, "decisions/a.md", days_old=1)
    _write(memdir, "decisions/b.md", days_old=1)
    _commit_all(memdir)
    deep = {"contradictions": [{"file_a": "decisions/a.md", "file_b": "decisions/b.md",
                               "similarity": 0.9, "llm_explanation": "disagree"}]}

    out = propose_mod.attach_proposals(
        run_lint_pass(), apply=True, deep_contradictions=deep
    )

    assert out["proposals"]["applied"]["stats"]["contradicts_proposed"] == 2
    meta_a = frontmatter.loads((memdir / "decisions" / "a.md").read_text(encoding="utf-8"))
    assert "decisions/b" in meta_a.get("contradicts", [])
    subjects = subprocess.run(
        ["git", "-C", str(memdir), "log", "--format=%s"],
        capture_output=True, text=True,
    ).stdout
    assert "lint-proposed ops: PROPOSE_CONTRADICTS" in subjects


def test_apply_rewrites_a_relative_date_through_the_executor(memdir):
    path = _write(
        memdir,
        "insights/drifting.md",
        days_old=0,
        body="- Yesterday we chose SQLite. <!-- fact:f1 -->",
    )
    _commit_all(memdir)
    expected = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    out = propose_mod.attach_proposals(run_lint_pass(), apply=True)

    applied = out["proposals"]["applied"]
    assert [a["op"] for a in applied["applied"]] == ["UPDATE"]
    assert applied["failed"] == []
    # The fact keeps its id — an UPDATE rewrites text, it does not re-key.
    body = path.read_text(encoding="utf-8")
    assert f"- On {expected} we chose SQLite. <!-- fact:f1 -->" in body
    subjects = subprocess.run(
        ["git", "-C", str(memdir), "log", "--format=%s"],
        capture_output=True, text=True,
    ).stdout
    assert "lint-proposed ops: UPDATE" in subjects


def test_apply_is_idempotent(memdir):
    _write(memdir, "insights/rotting.md", days_old=400)
    _write(memdir, "decisions/a.md", days_old=1)
    _write(memdir, "decisions/b.md", days_old=1)
    _commit_all(memdir)
    deep = {"contradictions": [{"file_a": "decisions/a.md", "file_b": "decisions/b.md",
                               "similarity": 0.9, "llm_explanation": ""}]}

    first = propose_mod.attach_proposals(
        run_lint_pass(), apply=True, deep_contradictions=deep
    )
    assert first["proposals"]["summary"]["applicable"] == 3

    second = propose_mod.attach_proposals(
        run_lint_pass(), apply=True, deep_contradictions=deep
    )
    # Nothing appliable is left: the archived file is no longer `status: active`
    # and both contradicts links are already recorded.
    assert second["proposals"]["summary"]["applicable"] == 0
    assert second["proposals"]["applied"]["applied"] == []


def test_apply_reports_a_failure_without_stopping_the_pass(memdir):
    _write(memdir, "insights/rotting.md", days_old=400)
    _write(memdir, "insights/also-rotting.md", days_old=400)
    _commit_all(memdir)

    with patch(
        "palinode.consolidation.archive.archive_memory",
        side_effect=[RuntimeError("disk on fire"), {"file": "x", "status": "archived"}],
    ):
        out = propose_mod.attach_proposals(run_lint_pass(), apply=True)

    applied = out["proposals"]["applied"]
    assert len(applied["failed"]) == 1
    assert "disk on fire" in applied["failed"][0]["error"]
    assert len(applied["applied"]) == 1


# ── surfaces ─────────────────────────────────────────────────────────────────


def test_cli_propose_is_a_dry_run(memdir):
    from palinode.cli.lint import lint as lint_cmd
    from palinode.cli._api import RequestError

    path = _write(memdir, "insights/rotting.md", days_old=400)
    _commit_all(memdir)
    before = path.read_text(encoding="utf-8")

    with patch("palinode.cli._api.api_client.lint", side_effect=RequestError("down")):
        result = CliRunner().invoke(lint_cmd, ["--propose", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["proposals"]["dry_run"] is True
    assert payload["proposals"]["summary"]["applicable"] == 1
    assert path.read_text(encoding="utf-8") == before


def test_cli_apply_runs_the_ops(memdir):
    from palinode.cli.lint import lint as lint_cmd
    from palinode.cli._api import RequestError

    path = _write(memdir, "insights/rotting.md", days_old=400)
    _commit_all(memdir)

    with patch("palinode.cli._api.api_client.lint", side_effect=RequestError("down")):
        result = CliRunner().invoke(lint_cmd, ["--apply"])

    assert result.exit_code == 0, result.output
    assert "actor 'lint'" in result.output
    assert frontmatter.loads(path.read_text(encoding="utf-8")).get("status") == "archived"


def test_cli_without_propose_is_unchanged(memdir):
    from palinode.cli.lint import lint as lint_cmd
    from palinode.cli._api import RequestError

    _write(memdir, "insights/rotting.md", days_old=400)

    with patch("palinode.cli._api.api_client.lint", side_effect=RequestError("down")):
        result = CliRunner().invoke(lint_cmd, ["--format", "json"])

    assert result.exit_code == 0, result.output
    assert "proposals" not in json.loads(result.output)


def test_api_lint_propose_query_param(memdir, monkeypatch):
    for key in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)
    _write(memdir, "insights/rotting.md", days_old=400)
    _commit_all(memdir)

    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=list(_FAKE_VECTOR)),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as client:
            plain = client.post("/lint").json()
            proposed = client.post("/lint", params={"propose": "true"}).json()
    srv._rate_counters.clear()

    assert "proposals" not in plain
    assert proposed["proposals"]["dry_run"] is True
    assert proposed["proposals"]["summary"]["applicable"] == 1
    # The report itself is untouched — the quality queues keep reading it.
    assert proposed["stale_files"] == plain["stale_files"]


@pytest.mark.asyncio
async def test_mcp_lint_tool_exposes_propose_and_stays_read_only(monkeypatch):
    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    from palinode.mcp import list_tools

    tool = {t.name: t for t in await list_tools()}["palinode_lint"]
    assert "propose" in tool.input_schema["properties"]
    # `apply` is deliberately absent: the tool's readOnlyHint must stay true.
    assert "apply" not in tool.input_schema["properties"]
    assert tool.annotations.read_only_hint is True


def test_parity_registers_propose_on_every_surface():
    from palinode.core import parity

    op = parity.by_name("lint")
    assert [p.name for p in op.canonical_params] == ["propose"]
    assert op.cli_command == "lint"
    assert op.mcp_tool == "palinode_lint"
    assert op.api_endpoint == ("POST", "/lint")
    # Promoted out of the registration backlog on all three surfaces.
    assert "lint" not in parity.INVENTORY_BACKLOG["cli"]
    assert "palinode_lint" not in parity.INVENTORY_BACKLOG["mcp"]
    assert "POST /lint" not in parity.INVENTORY_BACKLOG["api"]
