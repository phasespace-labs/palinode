"""`PROPOSE_CONTRADICTS` across the propose→dispose seam, on a real store.

`tests/test_typed_links.py` hand-feeds the op to `apply_operations`, which proves
the executor arm works and nothing about whether a *pass* can ever produce one.
Everything between a model's JSON and the link on disk — parse, the `allowed_ops`
filter, the executor, the status log, the git commit — is what these tests drive,
with the LLM replaced by an injected `llm_fn` returning canned op-JSON.

Real files, real SQLite, real git; no database mocks.
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import pytest

from palinode.consolidation import runner
from palinode.core.config import config

# Two facts that cannot both be true: same subject, incompatible port. Dated,
# so nothing about the pair invites a merge or an age-based archive.
FACT_A = "- [2026-08-01] The API listens on port 6340. <!-- fact:port-a -->"
FACT_B = "- [2026-08-20] The API listens on port 6341. <!-- fact:port-b -->"

CONTRADICTS_REF = "decisions/api-port"


def _git(memory_dir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=memory_dir, check=True, capture_output=True)


@pytest.fixture
def seeded_store(tmp_path, monkeypatch) -> Path:
    """A store whose project doc holds two conflicting facts, plus a decision."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)

    for sub in ("projects", "decisions", "daily", "specs/prompts"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    # The real prompt, so the shipped text is what drives the pass.
    repo_prompt = Path(__file__).resolve().parents[1] / "specs" / "prompts" / "compaction.md"
    (tmp_path / "specs" / "prompts" / "compaction.md").write_text(
        repo_prompt.read_text(encoding="utf-8"), encoding="utf-8"
    )

    (tmp_path / "projects" / "gateway.md").write_text(
        "---\nid: projects-gateway\ncategory: project\n---\n\n"
        "# Gateway\n\n## Current Work\n"
        f"{FACT_A}\n{FACT_B}\n",
        encoding="utf-8",
    )
    (tmp_path / "decisions" / "api-port.md").write_text(
        "---\nid: decisions-api-port\nname: api-port\n"
        "entities:\n  - project/gateway\n---\n\n"
        "The API listens on port 6340.\n",
        encoding="utf-8",
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    (tmp_path / "daily" / f"{today}.md").write_text(
        f"---\nid: daily-{today}\ncategory: daily\n---\n\n"
        "Looked at project/gateway; the port in the notes disagrees with the "
        "decision and nobody has said which one is current.\n",
        encoding="utf-8",
    )

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


def _llm_returning(ops: list[dict]):
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return json.dumps(ops), "fake-model"

    return _fn


PROPOSAL = [
    {
        "op": "PROPOSE_CONTRADICTS",
        "id": "port-b",
        "contradicts": [CONTRADICTS_REF],
        "rationale": "port-b says 6341, the decision says 6340, no reversal recorded",
    }
]


def test_proposed_contradiction_lands_as_a_typed_link(seeded_store: Path) -> None:
    """The whole point of the fix: a proposal reaches the file as a link."""
    target = seeded_store / "projects" / "gateway.md"

    result = runner.run_consolidation(dry_run=False, llm_fn=_llm_returning(PROPOSAL))

    assert result["projects_compacted"] == 1, result
    meta = frontmatter.load(target).metadata
    assert meta.get("contradicts") == [CONTRADICTS_REF]


def test_nothing_is_retired_by_the_proposal(seeded_store: Path) -> None:
    """No winner is picked: both facts stay live and no history is spawned."""
    target = seeded_store / "projects" / "gateway.md"

    runner.run_consolidation(dry_run=False, llm_fn=_llm_returning(PROPOSAL))

    body = frontmatter.load(target).content
    assert FACT_A in body and FACT_B in body
    assert "~~" not in body, "a contradiction proposal tombstoned a fact"
    assert not (seeded_store / "projects" / "gateway-history.md").exists()


def test_the_conflict_is_auditable_in_the_consolidation_log(seeded_store: Path) -> None:
    """The rationale the model gave is recorded where the pass is reviewed.

    The executor writes the link and drops the rationale; the runner's status
    log is where it survives, so a human reading the file can see *why* the two
    memories were linked without re-running the pass.
    """
    target = seeded_store / "projects" / "gateway.md"

    runner.run_consolidation(dry_run=False, llm_fn=_llm_returning(PROPOSAL))

    body = target.read_text(encoding="utf-8")
    assert "PROPOSE_CONTRADICTS" in body
    assert "no reversal recorded" in body


def test_allowed_ops_filter_is_the_second_gate(seeded_store: Path, monkeypatch) -> None:
    """Naming the op in the prompt is not enough — the pass filters proposals.

    Pinned because this is the half of the defect that leaves no trace: with the
    op missing from `allowed_ops` the proposal is dropped before the executor
    ever sees it, and the run reports a clean pass with nothing applied.
    """
    monkeypatch.setattr(
        config.consolidation, "allowed_ops", ["KEEP", "UPDATE", "MERGE", "SUPERSEDE"]
    )
    target = seeded_store / "projects" / "gateway.md"

    runner.run_consolidation(dry_run=False, llm_fn=_llm_returning(PROPOSAL))

    assert "contradicts" not in frontmatter.load(target).metadata


def test_malformed_ref_records_nothing(seeded_store: Path) -> None:
    """A fact id where a `category/slug` ref belongs is rejected, not written."""
    target = seeded_store / "projects" / "gateway.md"
    bad = [dict(PROPOSAL[0], contradicts=["../escape"])]

    runner.run_consolidation(dry_run=False, llm_fn=_llm_returning(bad))

    assert "contradicts" not in frontmatter.load(target).metadata


def test_active_decisions_carry_the_ref_the_op_requires(seeded_store: Path) -> None:
    """A conflict the model can see but cannot name is one it cannot record.

    `PROPOSE_CONTRADICTS` takes `category/slug` refs; the decision context used
    to render titles only, and the frontmatter `id` (`decisions-api-port`) is
    not a ref. Without a citable ref in the prompt the op stays unreachable in
    practice however clearly the prompt describes it.
    """
    seen: dict[str, str] = {}

    def _capture(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        seen["user"] = user_prompt
        seen["system"] = system_prompt
        return "[]", "fake-model"

    runner._consolidate_project("gateway", [], llm_fn=_capture)

    assert CONTRADICTS_REF in seen["user"], "no citable ref in ACTIVE_DECISIONS"
    assert "PROPOSE_CONTRADICTS" in seen["system"], "the shipped prompt lost the op"


def test_prompt_frontmatter_never_reaches_the_model(seeded_store: Path) -> None:
    """`version:`/`active:` is metadata about the prompt, not an instruction.

    The loader used to send the file whole, so the moment a prompt gained a
    frontmatter block — nightly already had one — the model received a YAML
    document ahead of its instructions. The block has to stay on disk (the
    versioning API and `palinode doctor` read it) and out of the system prompt.
    """
    seen: dict[str, str] = {}

    def _capture(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        seen["system"] = system_prompt
        return "[]", "fake-model"

    runner._consolidate_project("gateway", [], llm_fn=_capture)

    assert not seen["system"].startswith("---"), seen["system"][:120]
    assert "version:" not in seen["system"]
    assert seen["system"].lstrip().startswith("# Compaction Prompt")
    # …and the block is still in the file the doctor check reads.
    on_disk = (seeded_store / "specs" / "prompts" / "compaction.md").read_text()
    assert on_disk.startswith("---"), "frontmatter was stripped from disk, not the prompt"


def test_the_commit_message_does_not_read_as_nothing_happened(seeded_store: Path) -> None:
    """A pass whose only outcome is a link still committed `0u 0m 0s 0a`.

    Self-misreporting is its own defect: the counter that moved has to appear in
    the message, or the store's own history says the pass did nothing.
    """
    runner.run_consolidation(dry_run=False, llm_fn=_llm_returning(PROPOSAL))

    subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=seeded_store, capture_output=True, text=True, check=True,
    ).stdout
    assert "compaction" in subject
    assert "1c" in subject, subject
