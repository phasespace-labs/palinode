"""``scripts/regen-prompt-hashes.py`` — the manifest is derived, not curated.

``palinode prompt sync`` will only refresh a store prompt whose body hash it
recognises as one palinode shipped, so a hash missing from
``palinode/prompts/shipped-hashes.json`` is a store that can never be refreshed
without ``--force``. Remembering to prepend a hash by hand is exactly the kind
of step that gets skipped, and the omission is invisible until an operator
upgrades. The script derives the manifest from every historical blob of the
prompt files instead; these are its three load-bearing properties.

Runs the real script against this checkout — a fake git history would test the
fake. Skipped where there is no checkout to read (an installed sdist).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from palinode.prompts import prompt_body_hash

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "regen-prompt-hashes.py"
MANIFEST = REPO_ROOT / "palinode" / "prompts" / "shipped-hashes.json"
SOURCE_PROMPTS = REPO_ROOT / "specs" / "prompts"

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists() or not SCRIPT.is_file(),
    reason="needs the source checkout the script reads prompt history from",
)


def _run(output: Path, catalogue: Path | None = None) -> dict:
    args = [sys.executable, str(SCRIPT), "--output", str(output)]
    if catalogue is not None:
        args += ["--catalogue", str(catalogue)]
    subprocess.run(args, cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    return json.loads(output.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """One regeneration, shared: each run walks the whole prompt history."""
    return _run(tmp_path_factory.mktemp("regen") / "generated.json")


def test_regeneration_records_every_current_body(generated: dict) -> None:
    """The property the next prompt bump depends on: today's bodies are in it."""
    assert generated["hash_domain"] == "body"
    for source in sorted(SOURCE_PROMPTS.glob("*.md")):
        digest = prompt_body_hash(source.read_text(encoding="utf-8"))
        assert digest in generated["prompts"][source.name], source.name


def test_regeneration_is_a_fixed_point(tmp_path: Path) -> None:
    """Run it on its own output and nothing moves.

    Not a tidiness property: the manifest is regenerated on every prompt change,
    so a script whose ordering churned would rewrite unrelated entries each time
    and make the diff useless for seeing which revision was actually added.
    """
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    _run(first)
    _run(second, catalogue=first)

    assert second.read_text(encoding="utf-8") == first.read_text(encoding="utf-8")


def test_regeneration_never_drops_a_recorded_hash(generated: dict) -> None:
    """History is whatever this clone has; the manifest is every release.

    A shallow clone, or the squashed release repo, sees fewer revisions than the
    development repo. Pruning the manifest to what the local history proves
    would strand every store running one of the revisions it cannot see.
    """
    committed = json.loads(MANIFEST.read_text(encoding="utf-8"))["prompts"]

    for name, hashes in committed.items():
        missing = [h for h in hashes if h not in generated["prompts"].get(name, [])]
        assert not missing, f"{name} lost {missing}"


def test_the_committed_manifest_carries_the_history(generated: dict) -> None:
    """The committed file is in the shape the script produces, not hand-rolled."""
    committed = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert committed["schema"] == generated["schema"]
    assert committed["hash_domain"] == generated["hash_domain"]
    assert sorted(committed["prompts"]) == sorted(generated["prompts"])


def _load_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("regen_prompt_hashes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_at(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()


def test_batched_blob_reads_match_one_git_show_per_commit(tmp_path: Path) -> None:
    """The batched reader returns exactly what `git show <commit>:<path>` would.

    Covered: the same blob at many commits (the shallow-boundary case that
    made the per-commit loop time out on a reused CI checkout), a commit
    without the file, the commit that deleted it, and a non-UTF-8 revision.
    """
    regen = _load_script()
    repo = tmp_path / "r"
    repo.mkdir()
    _git_at(repo, "init", "-q", "-b", "main")
    path = "specs/prompts/p.md"
    (repo / "README").write_text("x\n")
    _git_at(repo, "add", "README")
    _git_at(repo, "commit", "-qm", "no prompt yet")
    commits = [_git_at(repo, "rev-parse", "HEAD")]
    (repo / "specs/prompts").mkdir(parents=True)
    for body in ("one\n", "two\n", "two\n"):          # a repeated blob
        (repo / path).write_text(body)
        _git_at(repo, "add", path)
        _git_at(repo, "commit", "-qm", body.strip(), "--allow-empty")
        commits.append(_git_at(repo, "rev-parse", "HEAD"))
    (repo / path).write_bytes(b"\xff\xfe not utf-8\n")
    _git_at(repo, "add", path)
    _git_at(repo, "commit", "-qm", "binary")
    commits.append(_git_at(repo, "rev-parse", "HEAD"))
    _git_at(repo, "rm", "-q", path)
    _git_at(repo, "commit", "-qm", "delete")
    commits.append(_git_at(repo, "rev-parse", "HEAD"))

    def one_show(commit: str) -> str | None:
        result = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True, check=False)
        if result.returncode:
            return None
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None

    expected = {c: t for c in commits if (t := one_show(c)) is not None}
    assert regen._blobs_at(repo, commits, path) == expected
    assert set(expected.values()) == {"one\n", "two\n"}
    assert regen._blobs_at(repo, [], path) == {}
