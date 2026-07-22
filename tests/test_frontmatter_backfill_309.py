"""Frontmatter backfill (#309) — idempotency, dry-run, non-destructiveness, honesty.

Every test runs against a real memory tree under ``tmp_path`` (and, where git
provenance is in play, a real ``git init``ed repo) — no mocked filesystem, no
mocked git. The four properties the capability is defined by each get direct
coverage:

- **idempotent** — a second run plans nothing and writes nothing;
- **dry-run correctness** — the reported plan is byte-for-byte what ``--apply``
  writes, and dry-run leaves the tree untouched;
- **non-destructive** — existing values survive verbatim, including values that
  disagree with what the backfill would have derived;
- **undeliverable handled explicitly** — a store with no git history reports the
  date fields as not-derivable instead of inventing them.
"""
from __future__ import annotations

import os
import subprocess

import frontmatter
import pytest
from click.testing import CliRunner

from palinode.core.config import config
from palinode.migration.frontmatter_backfill import (
    CATEGORY_TO_TYPE,
    BackfillError,
    apply_fills,
    commit_message,
    plan_backfill,
    run_backfill,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    """A memory dir wired into config, with no git repo (git-less baseline)."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def git_memory_dir(memory_dir, monkeypatch):
    """The same memory dir, but a real git repo with committed content.

    Pins ``git.auto_commit`` on: the commit assertions below depend on it, and
    ``config`` is a process-wide singleton that a leaky fixture elsewhere in the
    suite can leave switched off.
    """
    monkeypatch.setattr(config.git, "auto_commit", True)
    _git(memory_dir, "init", "-q")
    _git(memory_dir, "config", "user.email", "test@example.com")
    _git(memory_dir, "config", "user.name", "Test")
    return memory_dir


def _git(cwd, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _write(base, rel_path: str, content: str):
    path = base / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _meta(path) -> dict:
    return dict(frontmatter.loads(path.read_text(encoding="utf-8")).metadata)


def _plans_by_path(result: dict) -> dict[str, dict]:
    return {entry["path"]: entry for entry in result["files"]}


# ── Derivation ───────────────────────────────────────────────────────────────


def test_category_to_type_inverts_the_save_path_map():
    """The type map is the save path's, inverted — not an independent list."""
    from palinode.api.memory_write import _TYPE_TO_CATEGORY

    assert CATEGORY_TO_TYPE == {v: k for k, v in _TYPE_TO_CATEGORY.items()}
    assert CATEGORY_TO_TYPE["research"] == "ResearchRef"
    assert CATEGORY_TO_TYPE["decisions"] == "Decision"
    assert CATEGORY_TO_TYPE["projects"] == "ProjectSnapshot"


def test_type_only_case_fills_type_from_directory(memory_dir):
    """The 91-file majority case: rich frontmatter, just no ``type:``."""
    _write(
        memory_dir,
        "research/2026-03-27-some-ref.md",
        "---\nid: research-2026-03-27-some-ref\ncategory: research\n"
        "created_at: '2026-03-27T00:00:00+00:00'\nlast_updated: '2026-03-27T00:00:00+00:00'\n"
        "tags:\n  - ai\n---\nBody\n",
    )

    result = run_backfill()

    entry = _plans_by_path(result)["research/2026-03-27-some-ref.md"]
    assert [(f["field"], f["value"], f["source"]) for f in entry["fills"]] == [
        ("type", "ResearchRef", "category-map")
    ]


def test_no_frontmatter_at_all_gets_the_full_required_set(memory_dir):
    """The 30-file case: no memory frontmatter whatsoever."""
    _write(memory_dir, "insights/bare-note.md", "Just a body, no frontmatter.\n")

    result = run_backfill()

    entry = _plans_by_path(result)["insights/bare-note.md"]
    filled = {f["field"]: (f["value"], f["source"]) for f in entry["fills"]}
    assert filled["id"] == ("insights-bare-note", "filename-slug")
    assert filled["category"] == ("insights", "directory")
    assert filled["type"] == ("Insight", "category-map")


def test_legacy_created_is_carried_forward_not_reinterpreted(memory_dir):
    """``created`` → ``created_at``: same meaning, canonical spelling."""
    _write(
        memory_dir,
        "research/prd-audit-v1.md",
        "---\ncreated: 2026-01-15\ntopic: PRD audit\n---\nBody\n",
    )

    result = run_backfill()

    entry = _plans_by_path(result)["research/prd-audit-v1.md"]
    filled = {f["field"]: (f["value"], f["source"]) for f in entry["fills"]}
    assert filled["created_at"] == ("2026-01-15", "legacy:created")
    # `topic` is neither dropped nor reinterpreted — it is reported.
    assert any("topic" in note for note in entry["notes"])


def test_topic_is_never_dropped_or_remapped(memory_dir):
    """#309 proposed dropping ``topic`` or folding it into ``description``."""
    path = _write(
        memory_dir,
        "research/prd-audit-v1.md",
        "---\ncreated: 2026-01-15\ntopic: PRD audit\n---\nBody\n",
    )

    run_backfill(apply=True)

    meta = _meta(path)
    assert meta["topic"] == "PRD audit"
    assert "description" not in meta


def test_fills_are_emitted_in_canonical_save_order(memory_dir):
    _write(memory_dir, "decisions/bare.md", "Body\n")

    result = run_backfill()

    entry = _plans_by_path(result)["decisions/bare.md"]
    assert [f["field"] for f in entry["fills"]][:3] == ["id", "category", "type"]


# ── Undeliverable fields ─────────────────────────────────────────────────────


def test_dates_are_undeliverable_without_git_and_are_never_invented(memory_dir):
    """No git history and no legacy field ⇒ report, don't guess.

    mtime exists on every one of these files. The backfill deliberately does not
    use it: a copy or a checkout rewrites mtime, so it is not provenance.
    """
    path = _write(memory_dir, "decisions/no-git.md", "Body\n")

    result = run_backfill(apply=True)

    entry = _plans_by_path(result)["decisions/no-git.md"]
    undeliverable = {u["field"]: u["reason"] for u in entry["undeliverable"]}
    assert set(undeliverable) == {"created_at", "last_updated"}
    assert all("mtime is not provenance" in r for r in undeliverable.values())

    meta = _meta(path)
    assert "created_at" not in meta
    assert "last_updated" not in meta
    assert meta["id"] == "decisions-no-git"


def test_dates_come_from_git_when_the_store_is_versioned(git_memory_dir):
    _write(git_memory_dir, "decisions/tracked.md", "Body\n")
    _git(git_memory_dir, "add", "decisions/tracked.md")
    _git(git_memory_dir, "commit", "-q", "-m", "seed")

    result = run_backfill()

    entry = _plans_by_path(result)["decisions/tracked.md"]
    filled = {f["field"]: f["source"] for f in entry["fills"]}
    assert filled["created_at"] == "git:first-commit"
    assert filled["last_updated"] == "git:last-commit"
    assert entry["undeliverable"] == []


def test_undeliverable_fields_appear_in_the_commit_message(memory_dir):
    """The commit body is the audit record — including what was *not* written."""
    _write(memory_dir, "decisions/no-git.md", "Body\n")

    plan = plan_backfill()
    message = commit_message(plan.planned[0])

    assert message.startswith(f"{config.git.commit_prefix} frontmatter backfill: ")
    assert "- id: decisions-no-git (source: filename-slug)" in message
    assert "Left absent — no honest derivation available:" in message
    assert "created_at" in message


# ── Dry-run correctness ──────────────────────────────────────────────────────


def test_dry_run_writes_nothing(memory_dir):
    path = _write(memory_dir, "insights/untouched.md", "Body\n")
    before = path.read_text(encoding="utf-8")

    result = run_backfill(apply=False)

    assert result["dry_run"] is True
    assert result["files_written"] == []
    assert result["commits"] == 0
    assert path.read_text(encoding="utf-8") == before


def test_dry_run_plan_matches_what_apply_writes(memory_dir):
    """The reported plan and the applied plan are the same computation."""
    path = _write(memory_dir, "people/alice.md", "---\ncategory: people\n---\nBody\n")

    dry = run_backfill(apply=False)
    applied = run_backfill(apply=True)

    assert dry["files"] == applied["files"]
    assert applied["files_written"] == ["people/alice.md"]

    meta = _meta(path)
    for fill in dry["files"][0]["fills"]:
        assert str(meta[fill["field"]]) == fill["value"]


def test_dry_run_is_the_default(memory_dir):
    path = _write(memory_dir, "insights/default.md", "Body\n")

    assert run_backfill()["dry_run"] is True
    assert "id" not in _meta(path)


# ── Non-destructiveness ──────────────────────────────────────────────────────


def test_existing_values_are_never_overwritten(memory_dir):
    """Every already-present field survives verbatim, contents unchanged."""
    original = (
        "---\n"
        "id: custom-hand-written-id\n"
        "category: decisions\n"
        "type: Decision\n"
        "created_at: '2020-01-01T00:00:00+00:00'\n"
        "last_updated: '2020-01-02T00:00:00+00:00'\n"
        "priority: 5\n"
        "---\n\nThe body.\n"
    )
    path = _write(memory_dir, "decisions/complete.md", original)

    result = run_backfill(apply=True)

    assert result["files"] == []
    assert path.read_text(encoding="utf-8") == original


def test_a_type_that_disagrees_with_its_directory_is_left_alone(memory_dir):
    """A mismatch is lint's business to report, not a migration's to 'fix'."""
    path = _write(
        memory_dir,
        "decisions/mislabelled.md",
        "---\nid: decisions-mislabelled\ncategory: decisions\ntype: Insight\n---\nBody\n",
    )

    run_backfill(apply=True)

    assert _meta(path)["type"] == "Insight"


def test_unrelated_frontmatter_and_body_survive_the_rewrite(memory_dir):
    path = _write(
        memory_dir,
        "projects/keeps-everything.md",
        "---\n"
        "category: projects\n"
        "entities:\n  - project/palinode\n"
        "tags:\n  - alpha\n  - beta\n"
        "custom_field: keep me\n"
        "---\n\n"
        "# Heading\n\nA body with [[wikilinks]] and `code`.\n",
    )

    run_backfill(apply=True)

    meta = _meta(path)
    assert meta["entities"] == ["project/palinode"]
    assert meta["tags"] == ["alpha", "beta"]
    assert meta["custom_field"] == "keep me"
    body = frontmatter.loads(path.read_text(encoding="utf-8")).content
    assert "# Heading" in body
    assert "[[wikilinks]]" in body


def test_new_keys_are_appended_after_existing_ones(memory_dir):
    """Existing key order is preserved; fills land at the end."""
    path = _write(
        memory_dir, "insights/ordered.md", "---\ncategory: insights\ntags:\n  - x\n---\nBody\n"
    )

    run_backfill(apply=True)

    keys = list(_meta(path))
    assert keys[:2] == ["category", "tags"]
    assert set(keys[2:]) >= {"id", "type"}


def test_apply_fills_is_a_no_op_for_an_empty_fill_list():
    content = "---\nid: x\n---\nBody\n"
    assert apply_fills(content, ()) == content


# ── Idempotency ──────────────────────────────────────────────────────────────


def test_second_run_plans_nothing(memory_dir):
    _write(memory_dir, "insights/one.md", "Body\n")
    _write(memory_dir, "decisions/two.md", "---\ncategory: decisions\n---\nBody\n")

    first = run_backfill(apply=True)
    assert len(first["files_written"]) == 2

    second = run_backfill(apply=True)
    assert second["files"] == []
    assert second["files_written"] == []
    assert second["conformant"] == 2


def test_second_run_leaves_the_files_byte_identical(git_memory_dir):
    path = _write(git_memory_dir, "insights/stable.md", "Body\n")
    _git(git_memory_dir, "add", "-A")
    _git(git_memory_dir, "commit", "-q", "-m", "seed")

    run_backfill(apply=True)
    after_first = path.read_text(encoding="utf-8")

    run_backfill(apply=True)
    assert path.read_text(encoding="utf-8") == after_first


def test_apply_commits_one_commit_per_file(git_memory_dir):
    _write(git_memory_dir, "insights/a.md", "Body A\n")
    _write(git_memory_dir, "decisions/b.md", "Body B\n")
    _git(git_memory_dir, "add", "-A")
    _git(git_memory_dir, "commit", "-q", "-m", "seed")

    before = int(_git(git_memory_dir, "rev-list", "--count", "HEAD").stdout.strip())
    result = run_backfill(apply=True)
    after = int(_git(git_memory_dir, "rev-list", "--count", "HEAD").stdout.strip())

    assert result["commits"] == 2
    assert after - before == 2

    log = _git(git_memory_dir, "log", "-2", "--format=%s").stdout
    assert "frontmatter backfill: insights/a.md" in log
    assert "frontmatter backfill: decisions/b.md" in log

    # The derivation of every value is recorded in the commit body.
    body = _git(git_memory_dir, "log", "-1", "--format=%b", "--", "decisions/b.md").stdout
    assert "source: filename-slug" in body

    # And the second run adds no commits at all.
    run_backfill(apply=True)
    assert int(_git(git_memory_dir, "rev-list", "--count", "HEAD").stdout.strip()) == after


def test_no_commit_writes_without_committing(git_memory_dir):
    path = _write(git_memory_dir, "insights/uncommitted.md", "Body\n")
    _git(git_memory_dir, "add", "-A")
    _git(git_memory_dir, "commit", "-q", "-m", "seed")
    before = int(_git(git_memory_dir, "rev-list", "--count", "HEAD").stdout.strip())

    result = run_backfill(apply=True, commit=False)

    assert result["commits"] == 0
    assert "id" in _meta(path)
    assert int(_git(git_memory_dir, "rev-list", "--count", "HEAD").stdout.strip()) == before


# ── Scope ────────────────────────────────────────────────────────────────────


def test_top_level_documents_are_excluded(memory_dir):
    _write(memory_dir, "README.md", "# Readme\n")
    _write(memory_dir, "PROGRAM.md", "# Program\n")

    result = run_backfill()

    excluded = {e["path"]: e["reason"] for e in result["excluded"]}
    assert set(excluded) == {"README.md", "PROGRAM.md"}
    assert all("structural" in r for r in excluded.values())
    assert result["files"] == []


def test_non_memory_directories_are_excluded_and_named(memory_dir):
    """``specs/`` has no honest category or type — say so, don't invent one."""
    _write(memory_dir, "specs/some-spec.md", "Body\n")

    result = run_backfill()

    excluded = {e["path"]: e["reason"] for e in result["excluded"]}
    assert "specs/some-spec.md" in excluded
    assert "not a memory-category directory" in excluded["specs/some-spec.md"]
    assert result["files"] == []


def test_lint_skip_dirs_are_not_scanned(memory_dir):
    """Same file universe as ``run_lint_pass`` — archive/logs/.obsidian are out."""
    _write(memory_dir, "archive/old.md", "Body\n")
    _write(memory_dir, "logs/run.md", "Body\n")
    _write(memory_dir, "insights/live.md", "Body\n")

    result = run_backfill()

    assert result["scanned"] == 1
    assert [e["path"] for e in result["files"]] == ["insights/live.md"]


def test_symlink_escaping_the_memory_dir_is_dropped(memory_dir, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    target = outside / "secret.md"
    target.write_text("Body\n", encoding="utf-8")
    (memory_dir / "insights").mkdir(parents=True)
    os.symlink(target, memory_dir / "insights" / "escape.md")

    result = run_backfill(apply=True)

    assert result["scanned"] == 0
    assert result["files"] == []
    assert target.read_text(encoding="utf-8") == "Body\n"


def test_unparseable_frontmatter_is_reported_not_rewritten(memory_dir):
    path = _write(
        memory_dir,
        "insights/broken.md",
        "---\nid: [unclosed\n  bad: : yaml\n---\nBody\n",
    )
    before = path.read_text(encoding="utf-8")

    result = run_backfill(apply=True)

    assert [u["path"] for u in result["unreadable"]] == ["insights/broken.md"]
    assert result["files"] == []
    assert path.read_text(encoding="utf-8") == before


# ── daily/ — the open design question ────────────────────────────────────────


def test_daily_is_excluded_by_default(memory_dir):
    """PROGRAM.md § File tiers: a daily note is a log, not a memory."""
    path = _write(memory_dir, "daily/2026-04-19.md", "## Session End\n\nNotes\n")
    before = path.read_text(encoding="utf-8")

    result = run_backfill(apply=True)

    excluded = {e["path"]: e["reason"] for e in result["excluded"]}
    assert "daily/2026-04-19.md" in excluded
    assert "structural log tier" in excluded["daily/2026-04-19.md"]
    assert path.read_text(encoding="utf-8") == before


def test_daily_minimal_fills_id_and_category_but_withholds_type(memory_dir):
    """The opt-in: minimal frontmatter, still no ``type:`` assertion."""
    path = _write(memory_dir, "daily/2026-04-19.md", "## Session End\n\nNotes\n")

    result = run_backfill(apply=True, daily_mode="minimal")

    entry = _plans_by_path(result)["daily/2026-04-19.md"]
    filled = {f["field"]: (f["value"], f["source"]) for f in entry["fills"]}
    assert filled["id"] == ("daily-2026-04-19", "filename-slug")
    assert filled["category"] == ("daily", "directory")
    assert "type" not in filled

    withheld = {w["field"]: w["reason"] for w in entry["withheld"]}
    assert "type" in withheld
    assert "a log, not a memory" in withheld["type"]

    assert "type" not in _meta(path)


def test_daily_created_at_comes_from_the_filename_date(memory_dir):
    """In ``daily/`` the filename IS the note's subject date, by construction."""
    _write(memory_dir, "daily/2026-04-19.md", "Notes\n")

    result = run_backfill(daily_mode="minimal")

    filled = {
        f["field"]: (f["value"], f["source"])
        for f in _plans_by_path(result)["daily/2026-04-19.md"]["fills"]
    }
    assert filled["created_at"] == ("2026-04-19", "filename-date")


def test_daily_minimal_is_idempotent(memory_dir):
    _write(memory_dir, "daily/2026-04-19.md", "Notes\n")

    run_backfill(apply=True, daily_mode="minimal")
    second = run_backfill(apply=True, daily_mode="minimal")

    assert second["files"] == []
    assert second["files_written"] == []


def test_invalid_daily_mode_is_rejected(memory_dir):
    """A full typed daily memory is not reachable by a flag — it needs a
    ``SessionEnd``/``DailyLog`` value in the canonical type enum first."""
    with pytest.raises(BackfillError) as exc:
        run_backfill(daily_mode="full")
    assert "daily_mode" in str(exc.value)


# ── CLI surface ──────────────────────────────────────────────────────────────


def test_cli_dry_run_by_default(memory_dir):
    from palinode.cli.migrate import migrate

    path = _write(memory_dir, "insights/cli.md", "Body\n")
    result = CliRunner().invoke(migrate, ["frontmatter", "--format", "json"])

    # rich colourises JSON output, so match the key and value separately.
    assert result.exit_code == 0, result.output
    assert '"dry_run"' in result.output
    assert "true" in result.output
    assert "id" not in _meta(path)


def test_cli_apply_writes(memory_dir):
    from palinode.cli.migrate import migrate

    path = _write(memory_dir, "insights/cli-apply.md", "Body\n")
    result = CliRunner().invoke(
        migrate, ["frontmatter", "--apply", "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    assert _meta(path)["id"] == "insights-cli-apply"


def test_cli_rejects_an_unknown_daily_mode(memory_dir):
    from palinode.cli.migrate import migrate

    result = CliRunner().invoke(migrate, ["frontmatter", "--daily-mode", "full"])
    assert result.exit_code != 0


def test_cli_is_registered_and_parity_accounted():
    """The new command is on the CLI and classified in the parity inventory."""
    from palinode.cli import main
    from palinode.core.parity import INVENTORY_INFRA

    assert "frontmatter" in main.commands["migrate"].commands
    assert "migrate frontmatter" in INVENTORY_INFRA["cli"]


# ── Interaction with lint ────────────────────────────────────────────────────


def test_backfill_clears_lints_missing_fields_report(memory_dir):
    """The point of the exercise: lint's missing_fields drops to the excluded set."""
    from palinode.core.lint import run_lint_pass

    _write(memory_dir, "insights/a.md", "Body\n")
    _write(memory_dir, "decisions/b.md", "---\ncategory: decisions\n---\nBody\n")
    _write(memory_dir, "research/c.md", "---\nid: research-c\ncategory: research\n---\nBody\n")

    before = run_lint_pass()
    assert len(before["missing_fields"]) == 3

    run_backfill(apply=True)

    after = run_lint_pass()
    assert after["missing_fields"] == []


def test_default_backfill_plus_lint_reach_zero_with_daily_present(memory_dir):
    """The two halves agree: what the backfill won't touch, lint won't flag.

    The default run skips `daily/` as structural; lint exempts it from
    `missing_fields` for the same reason. So a corpus of memories *and* daily
    logs reaches a genuinely clean report — not "clean except the 18 files
    nothing is allowed to fix".
    """
    from palinode.core.lint import run_lint_pass

    _write(memory_dir, "insights/a.md", "Body\n")
    _write(memory_dir, "daily/2026-04-19.md", "## Session End\n\nNotes\n")
    _write(memory_dir, "daily/2026-04-20.md", "## Session End\n\nMore notes\n")

    assert len(run_lint_pass()["missing_fields"]) == 1  # only insights/a.md

    result = run_backfill(apply=True)

    assert result["files_written"] == ["insights/a.md"]
    assert [e["path"] for e in result["excluded"]] == [
        "daily/2026-04-19.md",
        "daily/2026-04-20.md",
    ]
    assert run_lint_pass()["missing_fields"] == []
