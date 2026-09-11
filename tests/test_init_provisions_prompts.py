"""`palinode init` puts the consolidation prompts into the memory store.

The prompts live in the *store*, not in the project being scaffolded, because
that is where the consolidation runner reads them from and where an operator
edits them. Before this, nothing wrote them: a `pip install` + `init` store had
an empty `specs/prompts/` (or no `specs/` at all) and the first `consolidate`
died. The prompts only ever arrived by cloning the repo.

The rules under test are the ones an operator's tuning depends on: never
overwrite, not even under `--force`, and `--dry-run` reports exactly the set
the real run writes.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.core.config import config
from palinode.prompts import packaged_prompt_names, packaged_prompts_dir


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A memory store separate from the project being scaffolded."""
    memory_dir = tmp_path / "store"
    memory_dir.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    return memory_dir


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "project"
    proj.mkdir()
    return proj


def _init(project: Path, *extra: str):
    return CliRunner().invoke(main, ["init", "--dir", str(project), *extra])


def test_init_writes_every_packaged_prompt_into_the_store(
    store: Path, project: Path
) -> None:
    result = _init(project)
    assert result.exit_code == 0, result.output

    prompts_dir = store / "specs" / "prompts"
    assert sorted(p.name for p in prompts_dir.glob("*.md")) == packaged_prompt_names()
    for name in packaged_prompt_names():
        assert (prompts_dir / name).read_bytes() == (
            packaged_prompts_dir() / name
        ).read_bytes()


def test_prompts_go_to_the_store_not_the_project(store: Path, project: Path) -> None:
    """The distinction the whole feature turns on."""
    _init(project)
    assert not (project / "specs").exists()
    assert (store / "specs" / "prompts" / "compaction.md").is_file()


def test_rerunning_init_is_idempotent(store: Path, project: Path) -> None:
    _init(project)
    first = (store / "specs" / "prompts" / "compaction.md").read_bytes()

    result = _init(project)

    assert result.exit_code == 0, result.output
    assert (store / "specs" / "prompts" / "compaction.md").read_bytes() == first
    assert "prompt compaction.md: skipped (exists)" in result.output


def test_an_edited_prompt_survives_a_rerun(store: Path, project: Path) -> None:
    prompts_dir = store / "specs" / "prompts"
    prompts_dir.mkdir(parents=True)
    edited = prompts_dir / "compaction.md"
    edited.write_text("MY OWN COMPACTION PROMPT\n", encoding="utf-8")

    _init(project)

    assert edited.read_text(encoding="utf-8") == "MY OWN COMPACTION PROMPT\n"
    # The rest still get provisioned.
    assert (prompts_dir / "update.md").is_file()


def test_force_does_not_overwrite_an_edited_prompt(store: Path, project: Path) -> None:
    """`--force` redoes the scaffolding; a tuned prompt is not scaffolding.

    Every other `init` write honours `--force`. This one must not: the file is
    memory-store content the operator was invited to edit, and `init` cannot
    tell a tuned prompt from a stale one. `palinode prompt sync` can, and is
    the sanctioned refresh path.
    """
    prompts_dir = store / "specs" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "compaction.md").write_text("MY OWN PROMPT\n", encoding="utf-8")

    _init(project, "--force")

    assert (prompts_dir / "compaction.md").read_text(encoding="utf-8") == "MY OWN PROMPT\n"


def test_dry_run_writes_nothing_and_names_every_prompt(
    store: Path, project: Path
) -> None:
    result = _init(project, "--dry-run")

    assert result.exit_code == 0, result.output
    assert not (store / "specs").exists()
    for name in packaged_prompt_names():
        assert name in result.output


def test_no_prompts_skips_provisioning(store: Path, project: Path) -> None:
    result = _init(project, "--no-prompts")

    assert result.exit_code == 0, result.output
    assert not (store / "specs").exists()


def test_a_provisioned_store_satisfies_the_doctor_check(
    store: Path, project: Path
) -> None:
    """The end state the ``prompts_current`` check was written to look for."""
    from palinode.diagnostics.checks.prompts_current import prompts_current
    from palinode.diagnostics.types import DoctorContext

    _init(project)

    res = prompts_current(DoctorContext(config=config))

    assert res.passed is True, res.message
    assert res.severity == "info"
