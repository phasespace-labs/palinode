"""``palinode aliases``: curating entity-aliases.yaml through the API.

Real SQLite and real git under ``tmp_path`` (the resolve-bundle store fixture);
only the embedder is stubbed. Every name is fictional.

Pinned here:

* add / remove / move / refuse, and the bytes written (sorted, stable);
* a dry run writes nothing — file bytes, git HEAD, working-tree status;
* a write is one commit touching only the alias file, and never a memory file;
* the alias cache sees a write at once, including a same-size rewrite;
* ``check`` reports the lint clusters (marked grouped once curated) and the
  ``project_tags_unmapped`` result;
* the CLI renders what the API returned, and the API refuses a path field.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess

import httpx
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import alias_curation, aliases, scope
from palinode.core.alias_curation import (
    AliasConflictError,
    AliasCurationError,
    AliasFileError,
    AliasNotFoundError,
)
from palinode.core.config import config
from tests import test_resolve_bundle as scenarios

mem = scenarios.mem
_write = scenarios._write


@pytest.fixture(autouse=True)
def _fresh_alias_cache():
    aliases.reset_cache()
    yield
    aliases.reset_cache()


@pytest.fixture()
def store(mem):
    """Two harbor spellings and one orbit record, committed and indexed."""
    _write(mem, "decisions/harbor-queue.md", "# Harbor queue\n\nThe queue is durable.",
           type="Decision", status="active", date="2026-09-01",
           entities=["project/harbor"])
    _write(mem, "decisions/harbor-dev-queue.md", "# Harbor dev queue\n\nDev uses memory.",
           type="Decision", status="active", date="2026-09-02",
           entities=["project/harbor-dev"])
    _write(mem, "decisions/orbit-cache.md", "# Orbit cache\n\nThe cache is warm.",
           type="Decision", status="active", date="2026-09-03",
           entities=["project/orbit-app"])
    _git(mem, "add", "decisions")
    _git(mem, "commit", "-q", "-m", "seed")
    return mem


def _git(mem, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(mem), *args], check=True, capture_output=True, text=True
    ).stdout


def _cli_http_client(tc: TestClient) -> httpx.Client:
    """A real ``httpx.Client`` whose transport dispatches to the in-process app.

    Handing the CLI the ``TestClient`` itself is not the production path:
    starlette's TestClient is built on ``httpx2`` when that package is
    installed (it is in CI), so its ``raise_for_status`` raises
    ``httpx2.HTTPStatusError``, which the CLI's ``httpx.HTTPStatusError``
    handler rightly does not catch. The CLI only ever talks to an ``httpx``
    client, so that is what it gets here.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        reply = tc.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(
            status_code=reply.status_code,
            headers=reply.headers.multi_items(),
            content=reply.content,
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")


def _alias_path(mem) -> str:
    return os.path.join(str(mem), "entity-aliases.yaml")


def _memory_hashes(mem) -> dict[str, str]:
    out: dict[str, str] = {}
    for root, dirs, names in os.walk(str(mem)):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            if name.endswith(".md"):
                path = os.path.join(root, name)
                with open(path, "rb") as fh:
                    out[path] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _snapshot(mem) -> dict[str, object]:
    path = _alias_path(mem)
    data = open(path, "rb").read() if os.path.exists(path) else None
    return {
        "alias": data,
        "memories": _memory_hashes(mem),
        "head": _git(mem, "rev-parse", "HEAD"),
        "status": _git(mem, "status", "--porcelain"),
    }


# ── add / remove / move / refuse ─────────────────────────────────────────────


def test_add_creates_a_group_and_commits_only_the_alias_file(store):
    memories = _memory_hashes(store)
    head = _git(store, "rev-parse", "HEAD")

    result = alias_curation.add("project/harbor", ["project/harbor-dev", "project/Harbor"])

    assert result["changed"] and result["committed"], result
    assert result["added"] == ["project/harbor-dev", "project/Harbor"]
    text = open(_alias_path(store), encoding="utf-8").read()
    assert text == alias_curation.render({
        "project/harbor": ["project/Harbor", "project/harbor-dev"],
    })
    assert yaml.safe_load(text) == {
        "aliases": {"project/harbor": ["project/Harbor", "project/harbor-dev"]},
    }
    # One new commit, touching the alias file and nothing else.
    assert _git(store, "rev-list", "--count", f"{head.strip()}..HEAD").strip() == "1"
    changed = _git(store, "show", "--name-only", "--format=%s", "HEAD").split("\n")
    assert changed[0].startswith(f"{config.git.commit_prefix} aliases add: project/harbor += ")
    assert [line for line in changed[1:] if line] == ["entity-aliases.yaml"]
    assert _git(store, "status", "--porcelain", "--", "entity-aliases.yaml") == ""
    # Aliases stay query-time: no memory file was rewritten.
    assert _memory_hashes(store) == memories


def test_output_is_sorted_and_stable_whatever_the_order_of_edits(store):
    alias_curation.add("project/orbit-app", ["project/orbitapp"])
    alias_curation.add("project/harbor", ["project/harbor-dev"])
    alias_curation.add("project/orbit-app", ["project/orbit"])
    first = open(_alias_path(store), "rb").read()

    os.remove(_alias_path(store))
    alias_curation.add("project/harbor", ["project/harbor-dev"])
    alias_curation.add("project/orbit-app", ["project/orbit", "project/orbitapp"])
    assert open(_alias_path(store), "rb").read() == first
    doc = yaml.safe_load(first)
    assert list(doc["aliases"]) == ["project/harbor", "project/orbit-app"]
    assert doc["aliases"]["project/orbit-app"] == ["project/orbit", "project/orbitapp"]


def test_adding_what_is_already_there_writes_nothing(store):
    alias_curation.add("project/harbor", ["project/harbor-dev"])
    before = _snapshot(store)
    result = alias_curation.add("project/harbor", ["project/harbor-dev"])
    assert result["changed"] is False and result["committed"] is False
    assert _snapshot(store) == before


def test_a_member_of_another_group_is_refused_without_move(store):
    alias_curation.add("project/harbor", ["project/harbor-dev"])
    before = _snapshot(store)
    with pytest.raises(AliasConflictError, match="already a member of project/harbor"):
        alias_curation.add("project/orbit-app", ["project/harbor-dev"])
    # Project refs compare case-insensitively, as recall isolation does.
    with pytest.raises(AliasConflictError):
        alias_curation.add("project/orbit-app", ["project/HARBOR-DEV"])
    assert _snapshot(store) == before


def test_move_regroups_the_member_and_removes_the_emptied_group(store):
    alias_curation.add("project/harbor", ["project/harbor-dev"])
    result = alias_curation.add("project/orbit-app", ["project/harbor-dev"], move=True)
    assert result["moved"] == [{"ref": "project/harbor-dev", "from": "project/harbor"}]
    assert result["removed_groups"] == ["project/harbor"]
    doc = yaml.safe_load(open(_alias_path(store), encoding="utf-8"))
    assert doc == {"aliases": {"project/orbit-app": ["project/harbor-dev"]}}


def test_another_groups_canonical_is_always_refused(store):
    alias_curation.add("project/harbor", ["project/harbor-dev"])
    with pytest.raises(AliasConflictError, match="canonical of its own group"):
        alias_curation.add("project/orbit-app", ["project/harbor"], move=True)
    # A case variant of an existing canonical cannot start a second group.
    with pytest.raises(AliasConflictError, match="same project as the existing canonical"):
        alias_curation.add("project/Harbor", ["project/harbour"])


def test_remove_drops_a_member_and_the_emptied_group(store):
    alias_curation.add("project/harbor", ["project/harbor-dev", "project/harbour"])
    first = alias_curation.remove("project/harbour")
    assert first["committed"] and first["removed_groups"] == []
    second = alias_curation.remove("project/harbor-dev")
    assert second["removed_groups"] == ["project/harbor"]
    doc = yaml.safe_load(open(_alias_path(store), encoding="utf-8"))
    assert doc == {"aliases": {}}
    subjects = _git(store, "log", "--format=%s", "-3").splitlines()
    assert subjects[0].endswith("aliases remove: project/harbor-dev from project/harbor")
    assert subjects[1].endswith("aliases remove: project/harbour from project/harbor")


def test_remove_refuses_a_canonical_and_an_unknown_ref(store):
    alias_curation.add("project/harbor", ["project/harbor-dev"])
    with pytest.raises(AliasCurationError, match="canonical"):
        alias_curation.remove("project/harbor")
    with pytest.raises(AliasNotFoundError):
        alias_curation.remove("project/nowhere")


@pytest.mark.parametrize("bad", [
    "harbor", "project/", "/project/harbor", "project/a/b", "project/../x",
    "project/har bor", "", "project/har\nbor",
])
def test_refs_must_match_the_entity_grammar(store, bad):
    with pytest.raises(AliasCurationError):
        alias_curation.add("project/harbor", [bad])
    with pytest.raises(AliasCurationError):
        alias_curation.add(bad, ["project/harbor-dev"])
    assert not os.path.exists(_alias_path(store))


def test_a_malformed_file_is_refused_not_rewritten(store):
    path = _alias_path(store)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("aliases:\n  project/harbor: 7\n")
    before = open(path, "rb").read()
    with pytest.raises(AliasFileError):
        alias_curation.add("project/harbor", ["project/harbor-dev"])
    with pytest.raises(AliasFileError):
        alias_curation.list_groups()
    assert open(path, "rb").read() == before


def test_a_symlinked_alias_file_is_refused(store, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "aliases.yaml"
    outside.write_text("aliases: {}\n", encoding="utf-8")
    os.symlink(outside, _alias_path(store))
    with pytest.raises(AliasFileError, match="not a regular file"):
        alias_curation.add("project/harbor", ["project/harbor-dev"])
    assert outside.read_text(encoding="utf-8") == "aliases: {}\n"


# ── dry run ──────────────────────────────────────────────────────────────────


def test_dry_run_add_and_remove_write_nothing(store):
    alias_curation.add("project/harbor", ["project/harbor-dev"])
    before = _snapshot(store)

    preview = alias_curation.add(
        "project/orbit-app", ["project/harbor-dev", "project/orbitapp"],
        move=True, dry_run=True,
    )
    assert preview["dry_run"] and preview["changed"] and not preview["committed"]
    assert "+  project/orbit-app:" in preview["diff"]
    assert "-  project/harbor:" in preview["diff"]
    assert preview["removed_groups"] == ["project/harbor"]
    assert _snapshot(store) == before

    removal = alias_curation.remove("project/harbor-dev", dry_run=True)
    assert removal["dry_run"] and removal["removed_groups"] == ["project/harbor"]
    assert _snapshot(store) == before
    assert aliases.resolve("project/harbor-dev") == ["project/harbor-dev", "project/harbor"]


def test_dry_run_on_a_store_without_the_file_creates_nothing(store):
    before = _snapshot(store)
    preview = alias_curation.add("project/harbor", ["project/harbor-dev"], dry_run=True)
    assert preview["changed"] and "+aliases:" in preview["diff"]
    assert _snapshot(store) == before
    assert not os.path.exists(_alias_path(store))


# ── the cache ────────────────────────────────────────────────────────────────


def test_the_alias_cache_sees_each_write_at_once(store):
    assert aliases.resolve("project/harbor-dev") == ["project/harbor-dev"]
    assert scope.canonical_project_ref("project/harbor-dev") == "project/harbor-dev"

    alias_curation.add("project/harbor", ["project/harbor-dev"])
    assert aliases.resolve("project/harbor-dev") == ["project/harbor-dev", "project/harbor"]
    assert scope.canonical_project_ref("project/harbor-dev") == "project/harbor"

    # A rewrite of identical size within the same timestamp tick: the stamp
    # alone could not tell the two files apart.
    size = os.path.getsize(_alias_path(store))
    alias_curation.remove("project/harbor-dev")
    alias_curation.add("project/harbor", ["project/harbor-qa1"])
    assert os.path.getsize(_alias_path(store)) == size
    assert aliases.resolve("project/harbor-dev") == ["project/harbor-dev"]
    assert aliases.resolve("project/harbor-qa1") == ["project/harbor-qa1", "project/harbor"]
    assert scope.canonical_project_ref("project/harbor-dev") == "project/harbor-dev"


def test_another_reader_sees_a_same_size_rewrite(store):
    """A reader in another process relies on the file's stamp, not reset_cache."""
    alias_curation.add("project/harbor", ["project/harbor-aaa"])
    assert aliases.resolve("project/harbor-aaa")[1:] == ["project/harbor"]
    path = _alias_path(store)
    st = os.stat(path)
    same_size = open(path, encoding="utf-8").read().replace("harbor-aaa", "harbor-bbb")
    from palinode.core import git_tools
    git_tools.write_memory_file(path, same_size)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.path.getsize(path) == st.st_size
    # No reset_cache here: the inode changed with the atomic replace.
    assert aliases.resolve("project/harbor-aaa") == ["project/harbor-aaa"]
    assert aliases.resolve("project/harbor-bbb")[1:] == ["project/harbor"]


# ── list and check ───────────────────────────────────────────────────────────


def test_list_names_each_ref_with_its_file_count(store):
    alias_curation.add("project/harbor", ["project/harbor-dev", "project/harbour"])
    listing = alias_curation.list_groups()
    assert listing["exists"] and listing["indexed"]
    assert listing["groups"] == [{
        "canonical": "project/harbor",
        "files": 1,
        "members": [
            {"ref": "project/harbor-dev", "files": 1},
            {"ref": "project/harbour", "files": 0},
        ],
    }]


def _tag_files(mem, ref: str, n: int) -> None:
    """Index rows only: ``check`` reads the entities table, as doctor does."""
    con = sqlite3.connect(os.path.join(str(mem), ".palinode.db"))
    try:
        con.executemany(
            "INSERT OR IGNORE INTO entities (entity_ref, file_path, category, last_seen) "
            "VALUES (?, ?, 'decisions', '2026-09-01')",
            [(ref, f"decisions/{ref.split('/')[1]}-{i}.md") for i in range(n)],
        )
        con.commit()
    finally:
        con.close()


def test_check_reports_open_clusters_and_the_doctor_result(store):
    _tag_files(store, "project/orbitapp", 12)
    _tag_files(store, "project/orbit-app", 12)

    before = alias_curation.check()
    cluster = next(c for c in before["alias_candidates"]
                   if {r["ref"] for r in c["refs"]} == {"project/orbitapp", "project/orbit-app"})
    assert cluster["kind"] == "separator" and cluster["grouped"] is False
    assert before["open_candidates"] >= 1
    doctor = before["project_tags_unmapped"]
    assert doctor["passed"] is False and doctor["severity"] == "warn"
    assert "project/orbitapp (12)" in doctor["message"]
    assert "palinode aliases add" in doctor["remediation"]
    assert "docs/ENTITY-ALIASES.md" in doctor["remediation"]

    alias_curation.add("project/orbit-app", ["project/orbitapp"])
    after = alias_curation.check()
    cluster = next(c for c in after["alias_candidates"]
                   if {r["ref"] for r in c["refs"]} == {"project/orbitapp", "project/orbit-app"})
    assert cluster["grouped"] is True
    assert after["open_candidates"] == before["open_candidates"] - 1
    assert after["project_tags_unmapped"]["passed"] is True


# ── API and CLI ──────────────────────────────────────────────────────────────


def test_api_routes_match_the_core_and_refuse_a_path(store):
    with TestClient(app) as client:
        preview = client.post("/aliases/add", json={
            "canonical": "project/harbor", "members": ["project/harbor-dev"], "dry_run": True,
        })
        assert preview.status_code == 200 and preview.json()["dry_run"] is True
        assert not os.path.exists(_alias_path(store))

        applied = client.post("/aliases/add", json={
            "canonical": "project/harbor", "members": ["project/harbor-dev"],
        })
        assert applied.status_code == 200 and applied.json()["committed"] is True

        listing = client.get("/aliases").json()
        assert listing == alias_curation.list_groups()

        conflict = client.post("/aliases/add", json={
            "canonical": "project/orbit-app", "members": ["project/harbor-dev"],
        })
        assert conflict.status_code == 409
        assert "move" in conflict.json()["detail"]

        # The file is never caller-supplied.
        smuggled = client.post("/aliases/add", json={
            "canonical": "project/orbit-app", "members": ["project/orbit"],
            "path": "/tmp/elsewhere.yaml",
        })
        assert smuggled.status_code == 422
        bad = client.post("/aliases/remove", json={"member": "../etc/passwd"})
        assert bad.status_code == 400
        missing = client.post("/aliases/remove", json={"member": "project/nowhere"})
        assert missing.status_code == 404

        removed = client.post("/aliases/remove", json={"member": "project/harbor-dev"})
        assert removed.status_code == 200 and removed.json()["removed_groups"] == ["project/harbor"]

        check = client.get("/aliases/check")
        assert check.status_code == 200
        assert set(check.json()) >= {"alias_candidates", "project_tags_unmapped", "open_candidates"}


def test_cli_drives_the_api(store, monkeypatch):
    from palinode.cli._api import api_client
    from palinode.cli.aliases import aliases as cli_aliases

    runner = CliRunner()
    with TestClient(app) as client:
        monkeypatch.setattr(api_client, "client", _cli_http_client(client))
        dry = runner.invoke(cli_aliases, [
            "add", "project/harbor", "project/harbor-dev", "--dry-run", "--format", "text",
        ])
        assert dry.exit_code == 0, dry.output
        assert "Dry run: nothing written." in dry.output
        assert "Apply: palinode aliases add project/harbor project/harbor-dev" in dry.output
        assert not os.path.exists(_alias_path(store))

        added = runner.invoke(cli_aliases, [
            "add", "project/harbor", "project/harbor-dev", "--format", "text",
        ])
        assert added.exit_code == 0, added.output
        assert "Written and committed: entity-aliases.yaml" in added.output

        listed = runner.invoke(cli_aliases, ["list", "--format", "text"])
        assert listed.exit_code == 0, listed.output
        assert "project/harbor (1 files)" in listed.output
        assert "  project/harbor-dev (1)" in listed.output

        as_json = runner.invoke(cli_aliases, ["list", "--format", "json"])
        assert yaml.safe_load(as_json.output)["groups"][0]["canonical"] == "project/harbor"

        refused = runner.invoke(cli_aliases, [
            "add", "project/orbit-app", "project/harbor-dev", "--format", "text",
        ])
        assert refused.exit_code == 1, refused.output
        assert isinstance(refused.exception, SystemExit), refused.exception
        assert "Error: API returned 409: " in refused.output
        assert "already a member of project/harbor" in refused.output

        moved = runner.invoke(cli_aliases, [
            "add", "project/orbit-app", "project/harbor-dev", "--move", "--format", "text",
        ])
        assert moved.exit_code == 0, moved.output
        assert "moves project/harbor-dev out of project/harbor" in moved.output
        assert "removes the emptied group project/harbor" in moved.output

        removed = runner.invoke(cli_aliases, ["remove", "project/harbor-dev", "--format", "text"])
        assert removed.exit_code == 0, removed.output

        checked = runner.invoke(cli_aliases, ["check", "--format", "text"])
        assert checked.exit_code == 0, checked.output
        assert "Alias candidates:" in checked.output
        assert "project_tags_unmapped [" in checked.output


@pytest.mark.parametrize("case", ["not_found", "file_error", "bad_ref"])
def test_cli_prints_the_api_refusal_and_exits_1(store, monkeypatch, case):
    """A 404, a 422 (malformed alias file) and a 400 (bad ref) each print the
    API's detail on stdout and exit 1; none escapes as an exception."""
    from palinode.cli._api import api_client
    from palinode.cli.aliases import aliases as cli_aliases

    if case == "file_error":
        with open(_alias_path(store), "w", encoding="utf-8") as fh:
            fh.write("aliases: [not, a, mapping]\n")
    args, expected = {
        "not_found": (["remove", "project/nowhere"],
                      ("Error: API returned 404: ", "project/nowhere is in no alias group")),
        "file_error": (["list"],
                       ("Error: API returned 422: ", "`aliases` is not a mapping")),
        "bad_ref": (["add", "project/harbor", "../escape"],
                    ("Error: API returned 400: ", "not a well-formed ref")),
    }[case]
    runner = CliRunner()
    with TestClient(app) as client:
        monkeypatch.setattr(api_client, "client", _cli_http_client(client))
        result = runner.invoke(cli_aliases, [*args, "--format", "text"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    for text in expected:
        assert text in result.output


def test_a_pydantic_validation_list_is_rendered_as_its_messages():
    from palinode.cli.aliases import _detail

    request = httpx.Request("POST", "http://testserver/aliases/add")
    response = httpx.Response(
        422, request=request,
        json={"detail": [{"loc": ["body", "members"], "msg": "Input should be a valid list"}]},
    )
    error = httpx.HTTPStatusError("422", request=request, response=response)
    assert _detail(error) == "Input should be a valid list"
