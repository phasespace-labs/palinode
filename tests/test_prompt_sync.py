"""`palinode prompt sync` — refresh stale prompts, never a tuned one.

The hard part is not copying files; it is telling the two apart. Consolidation
reads the *store's* prompts and operators are told to edit them, so "differs
from the packaged copy" means either "you are a release behind" or "you tuned
this", and the two need opposite treatment. The tiebreaker is
``palinode/prompts/shipped-hashes.json``: every sha256 palinode has ever
released. A store copy whose hash is in that list is provably pristine.

Every case here uses real files under ``tmp_path`` and the real manifest.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from palinode import __version__ as palinode_version
from palinode.cli import main
from palinode.cli.prompt import sync_plan
from palinode.core.config import config
from palinode.prompts import packaged_prompt_names, packaged_prompts_dir, shipped_hashes

#: What a store provisioned by an older release holds. Synthesised rather than
#: dug out of git history: the rule under test is "the hash is in the manifest",
#: and reconstructing a real past revision would tie the test to a git log the
#: released repo does not have. That the *real* manifest carries the real
#: history is asserted separately, below and in
#: tests/test_packaged_prompts_match_source.py.
_EARLIER_RELEASE = b"# Compaction Prompt\n\nAs shipped by some earlier release.\n"


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    memory_dir = tmp_path / "store"
    (memory_dir / "specs" / "prompts").mkdir(parents=True)
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    return memory_dir


def _prompts(store: Path) -> Path:
    return store / "specs" / "prompts"


def _sync(*extra: str):
    return CliRunner().invoke(main, ["prompt", "sync", *extra])


def _actions(store: Path) -> dict[str, str]:
    return {e["prompt"]: e["action"] for e in sync_plan(_prompts(store))}


# ---------------------------------------------------------------------------
# The four verdicts
# ---------------------------------------------------------------------------


def test_missing_prompts_are_added(store: Path) -> None:
    assert set(_actions(store).values()) == {"added"}

    result = _sync()

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in _prompts(store).glob("*.md")) == packaged_prompt_names()


def test_a_current_prompt_is_unchanged(store: Path) -> None:
    for name in packaged_prompt_names():
        (_prompts(store) / name).write_bytes((packaged_prompts_dir() / name).read_bytes())

    assert set(_actions(store).values()) == {"unchanged"}


def test_an_edited_prompt_is_kept(store: Path) -> None:
    edited = _prompts(store) / "compaction.md"
    edited.write_text("MY OWN COMPACTION PROMPT\n", encoding="utf-8")

    assert _actions(store)["compaction.md"] == "kept-edited"

    result = _sync("--format", "text")

    assert edited.read_text(encoding="utf-8") == "MY OWN COMPACTION PROMPT\n"
    assert "kept" in result.output
    assert "locally edited" in result.output


def test_a_previous_release_copy_is_refreshed(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the whole command exists for: a store a release behind."""
    from palinode.prompts import content_hash

    monkeypatch.setattr(
        "palinode.prompts.shipped_hashes",
        lambda: {"compaction.md": [content_hash(_EARLIER_RELEASE)]},
    )
    stale = _prompts(store) / "compaction.md"
    stale.write_bytes(_EARLIER_RELEASE)

    assert _actions(store)["compaction.md"] == "refreshed"

    result = _sync()

    assert result.exit_code == 0, result.output
    assert stale.read_bytes() == (packaged_prompts_dir() / "compaction.md").read_bytes()


def test_the_real_manifest_keeps_more_than_the_current_hash(store: Path) -> None:
    """History is the point: dropping old hashes strands every older store.

    ``compaction.md`` has been revised several times, so its list must carry
    more than one entry — otherwise a store on the previous release reads as
    edited and `sync` would never touch it again.
    """
    assert len(shipped_hashes()["compaction.md"]) > 1


# ---------------------------------------------------------------------------
# Behaviour around the verdicts
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(store: Path) -> None:
    result = _sync("--dry-run", "--format", "text")

    assert result.exit_code == 0, result.output
    assert list(_prompts(store).glob("*.md")) == []
    assert "would add" in result.output


def test_force_overwrites_an_edited_prompt(store: Path) -> None:
    edited = _prompts(store) / "compaction.md"
    edited.write_text("MY OWN COMPACTION PROMPT\n", encoding="utf-8")

    result = _sync("--force")

    assert result.exit_code == 0, result.output
    assert edited.read_bytes() == (packaged_prompts_dir() / "compaction.md").read_bytes()


def test_sync_touches_only_the_prompt_it_must(store: Path) -> None:
    """One edited prompt does not stop the other eight being provisioned."""
    (_prompts(store) / "compaction.md").write_text("MINE\n", encoding="utf-8")

    _sync()

    provisioned = sorted(p.name for p in _prompts(store).glob("*.md"))
    assert provisioned == packaged_prompt_names()
    assert (_prompts(store) / "compaction.md").read_text(encoding="utf-8") == "MINE\n"


def test_json_output_reports_every_verdict(store: Path) -> None:
    (_prompts(store) / "compaction.md").write_text("MINE\n", encoding="utf-8")

    result = _sync("--dry-run", "--format", "json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["counts"]["kept-edited"] == 1
    assert payload["counts"]["added"] == len(packaged_prompt_names()) - 1
    assert {e["prompt"] for e in payload["prompts"]} == set(packaged_prompt_names())


def test_an_unreadable_manifest_refuses_to_overwrite(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: with no shipped-hash history, nothing is provably stale.

    The only irreversible thing this command does is replace a file, so a
    manifest it cannot read must make it *more* conservative, not less.
    """
    monkeypatch.setattr("palinode.prompts.shipped_hashes", lambda: {})
    (_prompts(store) / "compaction.md").write_text("SOMETHING ELSE\n", encoding="utf-8")

    assert _actions(store)["compaction.md"] == "kept-edited"


def test_sync_after_init_reports_everything_current(
    store: Path, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    CliRunner().invoke(main, ["init", "--dir", str(project)])

    assert set(_actions(store).values()) == {"unchanged"}


# ---------------------------------------------------------------------------
# Provenance: the refresh is a commit, or it is reported as not being one
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def git_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store that is a real git repo, which is what a provisioned store is.

    Nothing about git is mocked below: what is under test is whether a commit
    exists afterwards, and a fake would assert the call, not the commit.
    """
    memory_dir = tmp_path / "store"
    (memory_dir / "specs" / "prompts").mkdir(parents=True)
    _git(memory_dir, "init", "-q")
    _git(memory_dir, "config", "user.email", "sync-test@example.invalid")
    _git(memory_dir, "config", "user.name", "Prompt Sync Test")
    (memory_dir / "README.md").write_text("store\n", encoding="utf-8")
    _git(memory_dir, "add", "README.md")
    _git(memory_dir, "commit", "-qm", "initial")
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    return memory_dir


def _subject(store: Path) -> str:
    return _git(store, "log", "-1", "--pretty=%s").strip()


def _commit_count(store: Path) -> int:
    return int(_git(store, "rev-list", "--count", "HEAD").strip())


def _packaged_version(name: str) -> str:
    text = (packaged_prompts_dir() / name).read_text(encoding="utf-8")
    return str(yaml.safe_load(text.split("---")[1])["version"])


def test_sync_commits_what_it_wrote(git_store: Path) -> None:
    """The reported bug: refreshed prompts left dirty in the working tree."""
    before = _commit_count(git_store)

    result = _sync()

    assert result.exit_code == 0, result.output
    assert _git(git_store, "status", "--porcelain") == ""
    assert _commit_count(git_store) == before + 1

    committed = _git(
        git_store, "show", "--name-only", "--pretty=format:", "HEAD"
    ).split()
    assert sorted(committed) == sorted(
        f"specs/prompts/{name}" for name in packaged_prompt_names()
    )


def test_the_commit_message_names_the_prompts_and_their_versions(
    git_store: Path,
) -> None:
    _sync()

    subject = _subject(git_store)
    assert subject.startswith("palinode prompt sync: added ")
    assert f"compaction.md→v{_packaged_version('compaction.md')}" in subject
    assert f"palinode {palinode_version}" in subject
    assert "--force" not in subject


def test_a_refresh_says_refreshed_and_names_the_new_version(
    git_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit question this exists for: when did compaction.md change?"""
    from palinode.prompts import content_hash

    monkeypatch.setattr(
        "palinode.prompts.shipped_hashes",
        lambda: {"compaction.md": [content_hash(_EARLIER_RELEASE)]},
    )
    for name in packaged_prompt_names():
        (_prompts(git_store) / name).write_bytes(
            (packaged_prompts_dir() / name).read_bytes()
        )
    (_prompts(git_store) / "compaction.md").write_bytes(_EARLIER_RELEASE)
    _git(git_store, "add", "specs")
    _git(git_store, "commit", "-qm", "provisioned by an earlier release")

    result = _sync()

    assert result.exit_code == 0, result.output
    subject = _subject(git_store)
    expected = f"refreshed compaction.md→v{_packaged_version('compaction.md')}"
    assert expected in subject, subject
    assert "added" not in subject
    assert _git(git_store, "status", "--porcelain") == ""


def test_force_is_recorded_in_the_commit_message(git_store: Path) -> None:
    """`--force` is the operator-discarded-edits event worth finding later."""
    (_prompts(git_store) / "compaction.md").write_text("MINE\n", encoding="utf-8")

    result = _sync("--force")

    assert result.exit_code == 0, result.output
    assert "--force" in _subject(git_store)


def test_dry_run_commits_nothing(git_store: Path) -> None:
    before = _commit_count(git_store)

    result = _sync("--dry-run")

    assert result.exit_code == 0, result.output
    assert _commit_count(git_store) == before
    assert _git(git_store, "status", "--porcelain") == ""


def test_a_sync_that_writes_nothing_makes_no_commit(git_store: Path) -> None:
    """An already-current store must not collect an empty commit per run."""
    _sync()
    after_first = _commit_count(git_store)

    _sync()

    assert _commit_count(git_store) == after_first


def test_auto_commit_off_writes_but_does_not_commit(
    git_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators who commit their store by hand keep that choice."""
    monkeypatch.setattr(config.git, "auto_commit", False)
    before = _commit_count(git_store)

    result = _sync("--format", "text")

    assert result.exit_code == 0, result.output
    assert sorted(
        p.name for p in _prompts(git_store).glob("*.md")
    ) == packaged_prompt_names()
    assert _commit_count(git_store) == before
    assert _git(git_store, "status", "--porcelain") != ""
    assert "not committed" in result.output


def test_json_output_reports_the_commit(git_store: Path) -> None:
    result = _sync("--format", "json")

    payload = json.loads(result.output)
    assert payload["committed"] is True
    assert payload["commit_message"] == _subject(git_store)


def test_json_output_reports_a_dry_run_as_uncommitted(git_store: Path) -> None:
    result = _sync("--dry-run", "--format", "json")

    payload = json.loads(result.output)
    assert payload["committed"] is False
    assert payload["commit_message"] is None
