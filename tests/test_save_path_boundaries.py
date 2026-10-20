# ruff: noqa: F811
"""The maintainer's boundaries for saving to a path, one scenario each.

Issue 250 sets them: a save without ``path`` is as it was; a ``path`` never reaches a
reserved folder or runs through a symbolic link; ids stay stable and distinct; and every
name the save writes into a link, a claim, a commit or an answer is the actual path.

Fixtures and helpers are those of ``test_save_path.py``: real route, real MCP dispatcher,
real CLI command, real git and filesystem under ``tmp_path``; the embedder and the scanner
are stubs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api import server as srv
from palinode.api.server import app
from palinode.core import cross_refs, store
from palinode.core.claims import derive_claim_id
from palinode.core.config import config
from palinode.core.save import SaveValidationError, save_memory
from palinode.core.skip_dirs import ALWAYS_SKIP
from test_save_path import (  # noqa: F401  (fixtures are used by name)
    _BODY,
    _PATH,
    _api_save,
    _cli_save,
    _directories,
    _files,
    _git,
    _head_files,
    _mcp_save,
    _read,
    cli_via_api,
    client,
    mcp_via_api,
    memory_dir,
    no_network,
    repo,
)

needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")


def _snapshot(*roots: Path) -> dict[str, object]:
    """Every file, folder and link under the roots, with bytes or link target.

    The index is left out: it is derived, and no refusal is meant to touch the Markdown.
    """
    seen: dict[str, object] = {}
    for root in roots:
        for folder, folders, names in os.walk(root, followlinks=False):
            for name in [*folders, *names]:
                full = Path(folder) / name
                if name.startswith(".palinode.db"):
                    continue
                if full.is_symlink():
                    seen[str(full)] = ("link", os.readlink(full))
                elif full.is_dir():
                    seen[str(full)] = "folder"
                else:
                    seen[str(full)] = full.read_bytes()
    return seen


# ── 1 · no path: the save is what it was ────────────────────────────────────

class TestSaveWithoutPath:

    def test_core_lands_in_the_types_folder_with_the_category_and_slug_id(self, memory_dir):
        result = save_memory(content=_BODY, type="Insight", slug="probe")

        assert _files(memory_dir) == ["insights/probe.md"]
        assert result["id"] == "insights-probe"
        assert "path" not in result
        assert _read(memory_dir, "insights/probe.md").metadata["id"] == "insights-probe"

    def test_core_derived_slug_gives_the_same_shape(self, memory_dir):
        result = save_memory(content="Quokka marmalade ledger\n\nbody", type="Decision")

        assert _files(memory_dir) == ["decisions/quokka-marmalade-ledger.md"]
        assert result["id"] == "decisions-quokka-marmalade-ledger"
        assert "path" not in result

    def test_api_answers_without_a_path_key(self, client, memory_dir):
        answer = _api_save(client, slug="probe").json()

        assert _files(memory_dir) == ["insights/probe.md"]
        assert answer["id"] == "insights-probe"
        assert answer["rel_path"] == "insights/probe.md"
        assert "path" not in answer

    def test_mcp_receipt_names_the_category_and_slug_file(self, mcp_via_api, memory_dir):
        text = _mcp_save(slug="probe")

        assert text.startswith("Saved to insights/probe.md (created)"), text
        assert _files(memory_dir) == ["insights/probe.md"]
        assert _read(memory_dir, "insights/probe.md").metadata["id"] == "insights-probe"

    def test_cli_json_has_the_id_and_no_path_key(self, cli_via_api, memory_dir):
        result = _cli_save("--slug", "probe", "--format", "json")

        answer = json.loads(result.output[result.output.index("{"):])
        assert result.exit_code == 0, result.output
        assert answer["id"] == "insights-probe"
        assert answer["rel_path"] == "insights/probe.md"
        assert "path" not in answer
        assert _files(memory_dir) == ["insights/probe.md"]


# ── 2 · every reserved folder, at any depth ─────────────────────────────────

class TestReservedFolders:

    @pytest.mark.parametrize("folder", sorted(ALWAYS_SKIP))
    @pytest.mark.parametrize("shape", ["{folder}/x.md", "team/{folder}/x.md", "a/{folder}/b/x.md"])
    def test_api_refuses_every_folder_the_indexes_skip_and_makes_no_folder(
        self, client, memory_dir, folder, shape
    ):
        written = shape.format(folder=folder)

        res = _api_save(client, path=written)

        assert 400 <= res.status_code < 500, (written, res.status_code, res.text)
        assert "reserved" in res.text.lower(), res.text
        assert _files(memory_dir) == []
        assert _directories(memory_dir) == []

    @pytest.mark.parametrize("folder", sorted(ALWAYS_SKIP))
    @pytest.mark.parametrize("shape", ["{folder}/x.md", "team/{folder}/x.md", "a/{folder}/b/x.md"])
    def test_core_names_the_folder_apart_from_the_path(self, memory_dir, folder, shape):
        written = shape.format(folder=folder)

        with pytest.raises(SaveValidationError) as raised:
            save_memory(content=_BODY, type="Insight", path=written)

        said = str(raised.value).replace(repr(written), "").lower()
        assert "reserved" in said, said
        assert folder in said, said

    @pytest.mark.parametrize(
        "written, folder",
        [
            (".GIT/x.md", ".git"),
            ("prompts./x.md", "prompts"),
            (".git /x.md", ".git"),
            ("LOGS/x.md", "logs"),
            ("Specs/x.md", "specs"),
            ("team\\specs\\x.md", "specs"),
        ],
    )
    def test_a_spelling_the_filesystem_reads_as_the_folder_is_named_by_the_folder(
        self, memory_dir, written, folder
    ):
        with pytest.raises(SaveValidationError) as raised:
            save_memory(content=_BODY, type="Insight", path=written)

        said = str(raised.value).replace(repr(written), "").lower()
        assert "reserved" in said, said
        assert folder in said, said
        assert _directories(memory_dir) == []

    def test_a_dot_folder_is_refused_as_reserved_and_not_as_an_uncitable_link(self, client, memory_dir):
        res = _api_save(client, path=".obsidian/x.md")

        assert "reserved" in res.text.lower(), res.text
        assert "link" not in res.text.lower(), res.text


# ── 3 · names that only look reserved ───────────────────────────────────────

class TestNamesThatAreNotReservedFolders:

    @pytest.mark.parametrize(
        "written",
        ["logs.md", "specs.md", "prompts.md", "team/logs.md", "specsheet/x.md", "logs-2026/x.md"],
    )
    def test_a_file_named_like_a_folder_or_a_folder_that_starts_like_one_is_written(
        self, client, memory_dir, written
    ):
        res = _api_save(client, path=written)

        assert res.status_code == 200, (written, res.text)
        assert _files(memory_dir) == [written]


# ── 4 · a symbolic link anywhere below the store root ───────────────────────

def _link(target: Path, at: Path) -> None:
    at.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, at)


def _link_to_a_folder_inside(mem: Path, outside: Path) -> tuple[str, str]:
    (mem / "real").mkdir()
    _link(mem / "real", mem / "link")
    return "link/x.md", "link"


def _link_to_a_folder_outside(mem: Path, outside: Path) -> tuple[str, str]:
    _link(outside, mem / "exit")
    return "exit/x.md", "exit"


def _dangling_link(mem: Path, outside: Path) -> tuple[str, str]:
    _link(outside / "nowhere", mem / "gone")
    return "gone/x.md", "gone"


def _link_deep_in_the_path(mem: Path, outside: Path) -> tuple[str, str]:
    (mem / "real").mkdir()
    (mem / "a").mkdir()
    _link(mem / "real", mem / "a" / "b")
    return "a/b/c/x.md", "a/b"


def _link_to_the_store_root(mem: Path, outside: Path) -> tuple[str, str]:
    _link(mem, mem / "self")
    return "self/x.md", "self"


def _last_component_links_to_a_file_inside(mem: Path, outside: Path) -> tuple[str, str]:
    (mem / "team").mkdir()
    (mem / "real.md").write_text("---\nid: real\n---\n\nkept\n", encoding="utf-8")
    _link(mem / "real.md", mem / "team" / "x.md")
    return "team/x.md", "team/x.md"


def _last_component_links_to_a_file_outside(mem: Path, outside: Path) -> tuple[str, str]:
    outside.mkdir(exist_ok=True)
    (outside / "target.md").write_text("kept\n", encoding="utf-8")
    _link(outside / "target.md", mem / "team" / "x.md")
    return "team/x.md", "team/x.md"


def _last_component_is_dangling(mem: Path, outside: Path) -> tuple[str, str]:
    _link(outside / "nowhere.md", mem / "team" / "x.md")
    return "team/x.md", "team/x.md"


#: condition → how to build it; returns the path to save and the link the message must name
_LINKS = {
    "a link to a folder inside the store": _link_to_a_folder_inside,
    "a link to a folder outside the store": _link_to_a_folder_outside,
    "a dangling link": _dangling_link,
    "a link in the middle of the path": _link_deep_in_the_path,
    "a link to the store root": _link_to_the_store_root,
    "a last component that links to a file inside": _last_component_links_to_a_file_inside,
    "a last component that links to a file outside": _last_component_links_to_a_file_outside,
    "a last component that is dangling": _last_component_is_dangling,
}


@pytest.fixture()
def outside(tmp_path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    return folder


@needs_symlinks
class TestSymbolicLinks:

    @pytest.mark.parametrize("condition", list(_LINKS))
    def test_core_refuses_with_the_message_that_names_the_link(self, memory_dir, outside, condition):
        written, linked = _LINKS[condition](memory_dir, outside)

        with pytest.raises(SaveValidationError) as raised:
            save_memory(content=_BODY, type="Insight", path=written)

        assert str(raised.value) == f"path {written!r} runs through {linked!r}, a symbolic link"

    @pytest.mark.parametrize("condition", list(_LINKS))
    def test_api_answers_a_4xx_that_names_the_link(self, client, memory_dir, outside, condition):
        written, linked = _LINKS[condition](memory_dir, outside)

        res = _api_save(client, path=written)

        assert 400 <= res.status_code < 500, (condition, res.status_code, res.text)
        assert "symbolic link" in res.text, res.text
        assert repr(linked) in res.text, res.text

    @pytest.mark.parametrize("condition", list(_LINKS))
    def test_nothing_is_written_through_the_link_and_no_folder_is_made(
        self, client, memory_dir, outside, condition
    ):
        written, _linked = _LINKS[condition](memory_dir, outside)
        before = _snapshot(memory_dir, outside)

        _api_save(client, path=written)

        assert _snapshot(memory_dir, outside) == before

    @pytest.mark.parametrize("condition", list(_LINKS)[:3])
    def test_mcp_returns_an_error_that_names_the_link(
        self, mcp_via_api, memory_dir, outside, condition
    ):
        written, _linked = _LINKS[condition](memory_dir, outside)

        text = _mcp_save(path=written)

        assert text.startswith(mcp.DISPATCH_ERROR_PREFIXES), text
        assert "symbolic link" in text, text

    @pytest.mark.parametrize("condition", list(_LINKS)[:3])
    def test_cli_exits_non_zero_on_the_4xx(self, cli_via_api, memory_dir, outside, condition):
        written, _linked = _LINKS[condition](memory_dir, outside)

        result = _cli_save("--path", written)

        assert result.exit_code != 0, result.output
        assert "API returned 400" in result.output

    def test_a_link_beside_the_path_does_not_matter(self, client, memory_dir, outside):
        (memory_dir / "team").mkdir()
        _link(outside, memory_dir / "team" / "other-folder")

        res = _api_save(client, path="team/x.md")

        assert res.status_code == 200, res.text
        assert (memory_dir / "team" / "x.md").is_file()
        assert list(outside.iterdir()) == []


# ── 5 · a store root that is itself a link ──────────────────────────────────

@pytest.fixture(params=["the root is a link", "a folder above the root is a link"])
def linked_store(request, tmp_path, monkeypatch) -> tuple[Path, Path]:
    """The store root as the config names it, and the folder that really holds the files."""
    real = tmp_path / "real"
    real.mkdir()
    if request.param == "the root is a link":
        named, held = tmp_path / "linked", real
        os.symlink(real, named)
    else:
        (real / "mem").mkdir()
        os.symlink(real, tmp_path / "above")
        named, held = tmp_path / "above" / "mem", real / "mem"
    monkeypatch.setattr(config, "memory_dir", str(named))
    monkeypatch.setattr(config, "db_path", str(named / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    srv._rate_counters.clear()
    store.init_db()
    yield named, held
    srv._rate_counters.clear()


@needs_symlinks
class TestStoreRootThatIsALink:

    def test_api_writes_the_file_in_the_folder_the_link_points_to(self, linked_store):
        _named, held = linked_store

        with TestClient(app, raise_server_exceptions=True) as c:
            res = _api_save(c, path=_PATH)

        assert res.status_code == 200, res.text
        assert _files(held) == [_PATH]
        assert res.json()["path"] == _PATH
        assert res.json()["rel_path"] == _PATH

    def test_core_writes_it_too(self, linked_store):
        _named, held = linked_store

        result = save_memory(content=_BODY, type="Insight", path=_PATH)

        assert _files(held) == [_PATH]
        assert result["id"] == "team-designer-role"

    def test_a_link_inside_such_a_store_is_still_refused(self, linked_store):
        _named, held = linked_store
        (held / "real").mkdir()
        os.symlink(held / "real", held / "link")

        with pytest.raises(SaveValidationError) as raised:
            save_memory(content=_BODY, type="Insight", path="link/x.md")

        assert "symbolic link" in str(raised.value)
        assert _files(held) == []


# ── 6 and 7 · ids ───────────────────────────────────────────────────────────

class TestIds:

    def test_one_file_name_in_two_folders_gets_two_ids(self, client, memory_dir):
        first = _api_save(client, path="team/alice.md", content="Team note about Alice.").json()
        second = _api_save(client, path="people/alice.md", type="PersonMemory",
                           content="Person file for Alice.").json()

        assert (first["id"], second["id"]) == ("team-alice", "people-alice")
        assert _files(memory_dir) == ["people/alice.md", "team/alice.md"]
        assert _read(memory_dir, "team/alice.md").metadata["id"] == "team-alice"
        assert _read(memory_dir, "people/alice.md").metadata["id"] == "people-alice"
        assert "Team note" in _read(memory_dir, "team/alice.md").content
        assert "Person file" in _read(memory_dir, "people/alice.md").content

    def test_a_save_by_path_then_by_slug_is_one_file_and_one_id(self, client, memory_dir):
        by_path = _api_save(client, path="decisions/foo.md", type="Decision",
                            content="First body.").json()
        by_slug = _api_save(client, slug="foo", type="Decision", content="Second body.").json()

        assert (by_path["id"], by_slug["id"]) == ("decisions-foo", "decisions-foo")
        assert _files(memory_dir) == ["decisions/foo.md"]
        assert by_slug["save_outcome"] == "replaced"
        assert _read(memory_dir, "decisions/foo.md").metadata["id"] == "decisions-foo"
        assert "Second body." in _read(memory_dir, "decisions/foo.md").content

    def test_a_save_by_slug_then_by_path_is_one_file_and_one_id(self, client, memory_dir):
        by_slug = _api_save(client, slug="foo", type="Decision", content="First body.").json()
        by_path = _api_save(client, path="decisions/foo.md", type="Decision",
                            content="Second body.").json()

        assert (by_slug["id"], by_path["id"]) == ("decisions-foo", "decisions-foo")
        assert _files(memory_dir) == ["decisions/foo.md"]
        assert by_path["save_outcome"] == "replaced"

    def test_a_path_in_a_folder_the_type_does_not_name_keeps_the_path_in_the_id(
        self, client, memory_dir
    ):
        answer = _api_save(client, path="a/b/c.md", type="Decision").json()

        assert answer["id"] == "a-b-c"
        assert _read(memory_dir, "a/b/c.md").metadata["category"] == "decisions"


# ── 8 · claims ──────────────────────────────────────────────────────────────

_CLAIM_TEXT = "the chat breaker trips on model eviction"


def _claim() -> dict:
    return {
        "text": _CLAIM_TEXT,
        "source_id": "research/contention.md",
        "span": {"quote": "the two models share the card"},
    }


class TestClaims:

    def test_a_claims_id_is_bound_to_the_path_of_the_memory(self, client, memory_dir):
        res = _api_save(client, path=_PATH, claims=[_claim()])

        assert res.status_code == 200, res.text
        claim_id = _read(memory_dir, _PATH).metadata["claims"][0]["claim_id"]
        assert claim_id == derive_claim_id("team/designer/role.md", _CLAIM_TEXT)
        assert claim_id != derive_claim_id("insights/role.md", _CLAIM_TEXT)

    def test_blame_reads_the_same_id_back_from_the_file(self, client, memory_dir):
        from palinode.core.claims import resolve_memory_claims

        _api_save(client, path=_PATH, claims=[_claim()])

        resolved = resolve_memory_claims(str(memory_dir / _PATH), str(memory_dir))

        assert [r["claim_id_status"] for r in resolved] == ["ok"]


# ── 9 · reciprocal contradicts ──────────────────────────────────────────────

class TestContradicts:

    def test_the_link_written_into_the_other_memory_names_the_path_without_md(
        self, client, memory_dir
    ):
        (memory_dir / "insights").mkdir()
        (memory_dir / "insights" / "other.md").write_text(
            "---\nid: insights-other\ncategory: insights\n---\n\nOther memory.\n", encoding="utf-8"
        )

        res = _api_save(client, path=_PATH, contradicts=["insights/other"])

        assert res.status_code == 200, res.text
        assert _read(memory_dir, _PATH).metadata["contradicts"] == ["insights/other"]
        assert _read(memory_dir, "insights/other.md").metadata["contradicts"] == [
            "team/designer/role"
        ]


# ── 10 · cross_refs through the watcher's update ────────────────────────────

class TestCrossRefs:

    @pytest.fixture(autouse=True)
    def cross_refs_on(self, monkeypatch):
        monkeypatch.setattr(config.capture.cross_refs, "enabled", True)
        monkeypatch.setattr(config.capture.cross_refs, "min_token_len", 6)

    def _update(self, memory_dir: Path, rel: str) -> dict:
        file_path = memory_dir / rel
        return cross_refs.update_file_cross_refs(
            str(file_path), content=file_path.read_text(encoding="utf-8")
        )

    def test_a_body_that_names_a_path_saved_memory_by_its_path_links_to_the_path(
        self, client, memory_dir
    ):
        _api_save(client, path="team/designer-role.md", content="The designer role.")
        _api_save(client, path="notes/review.md", content="Read team/designer-role before the review.")

        result = self._update(memory_dir, "notes/review.md")

        assert result["refs"] == ["team/designer-role"]
        assert _read(memory_dir, "notes/review.md").metadata["cross_refs"] == ["team/designer-role"]

    def test_a_body_that_names_it_by_its_slug_links_to_the_path_too(self, client, memory_dir):
        _api_save(client, path="team/designer-role.md", content="The designer role.")
        _api_save(client, slug="review", content="Ask what the designer-role decided.")

        result = self._update(memory_dir, "insights/review.md")

        assert result["refs"] == ["team/designer-role"]

    def test_a_path_saved_body_that_names_a_category_memory_links_to_it(self, client, memory_dir):
        _api_save(client, slug="breaker-notes", content="Breaker notes.")
        _api_save(client, path="team/review.md", content="See insights/breaker-notes for the breaker.")

        result = self._update(memory_dir, "team/review.md")

        assert result["refs"] == ["insights/breaker-notes"]

    def test_a_path_saved_memory_never_links_to_itself(self, client, memory_dir):
        _api_save(client, path="team/designer-role.md",
                  content="This is team/designer-role, the designer-role file.")

        result = self._update(memory_dir, "team/designer-role.md")

        assert result["refs"] == []


# ── 11 · privacy front matter on an existing file ───────────────────────────

_PRIVATE_FILE = (
    "---\n"
    "id: team-designer-role\n"
    "category: insights\n"
    "type: Insight\n"
    "visibility: private\n"
    "scope: project/secret-work\n"
    "access:\n"
    "- member/alice\n"
    "---\n\n"
    "Existing private body.\n"
)


class TestPrivacyOfAnExistingFile:

    @pytest.mark.parametrize("policy", [None, "append", "replace"])
    def test_a_resave_without_privacy_keeps_visibility_scope_and_access(
        self, client, memory_dir, policy
    ):
        (memory_dir / "team" / "designer").mkdir(parents=True)
        (memory_dir / _PATH).write_text(_PRIVATE_FILE, encoding="utf-8")
        policy_field = {} if policy is None else {"update_policy": policy}

        res = _api_save(client, path=_PATH, content="A new body.", **policy_field)

        assert res.status_code == 200, res.text
        assert res.json()["path"] == _PATH
        assert "A new body." in _read(memory_dir, _PATH).content
        kept = _read(memory_dir, _PATH).metadata
        assert kept["visibility"] == "private"
        assert kept["scope"] == "project/secret-work"
        assert kept["access"] == ["member/alice"]

    def test_the_same_resave_by_slug_keeps_them_in_the_same_way(self, client, memory_dir):
        (memory_dir / "insights").mkdir()
        (memory_dir / "insights" / "role.md").write_text(_PRIVATE_FILE, encoding="utf-8")

        _api_save(client, slug="role", content="A new body.")

        kept = _read(memory_dir, "insights/role.md").metadata
        assert (kept["visibility"], kept["scope"], kept["access"]) == (
            "private", "project/secret-work", ["member/alice"],
        )


# ── 12 · a refused path leaves the store as it was ──────────────────────────

def _store_with_a_file_used_as_a_folder(mem: Path, outside: Path) -> str:
    (mem / "people").mkdir()
    (mem / "people" / "a.md").write_text("---\nid: a\n---\nThe file.\n", encoding="utf-8")
    return "people/a.md/x.md"


#: condition → how to build the store; returns the path to save
_REFUSED = {
    "not .md": lambda mem, outside: "team/role.txt",
    "a parent segment": lambda mem, outside: "a/../../x.md",
    "absolute": lambda mem, outside: "/abs/x.md",
    "a drive": lambda mem, outside: "C:/x.md",
    "a reserved folder": lambda mem, outside: "team/specs/x.md",
    "a dot folder": lambda mem, outside: ".git/x.md",
    "a name no link could cite": lambda mem, outside: "my notes/x.md",
    "a name too long": lambda mem, outside: f"team/{'n' * 300}.md",
    "a file used as a folder": _store_with_a_file_used_as_a_folder,
    "a link to a folder outside": lambda mem, outside: _link_to_a_folder_outside(mem, outside)[0],
    "a dangling link": lambda mem, outside: _dangling_link(mem, outside)[0],
    "a last component that is dangling": lambda mem, outside: _last_component_is_dangling(mem, outside)[0],
}


class TestRefusedPathLeavesTheStoreAsItWas:

    @needs_symlinks
    @pytest.mark.parametrize("condition", list(_REFUSED))
    def test_nothing_is_written_no_folder_is_made_and_no_commit_is_made(
        self, client, repo, outside, condition
    ):
        written = _REFUSED[condition](repo, outside)
        head = _git(repo, "rev-parse", "HEAD")
        before = _snapshot(repo, outside)

        res = _api_save(client, path=written)

        assert 400 <= res.status_code < 500, (condition, res.status_code, res.text)
        assert _git(repo, "rev-parse", "HEAD") == head
        assert _snapshot(repo, outside) == before

    def test_path_with_slug_is_refused_and_leaves_the_store_as_it_was(self, client, repo, outside):
        head = _git(repo, "rev-parse", "HEAD")
        before = _snapshot(repo, outside)

        res = _api_save(client, path="team/new-folder/x.md", slug="x")

        assert 400 <= res.status_code < 500, res.text
        assert _git(repo, "rev-parse", "HEAD") == head
        assert _snapshot(repo, outside) == before


# ── 13 · the commit ─────────────────────────────────────────────────────────

class TestCommitNamesThePath:

    def test_the_message_names_the_path_and_the_commit_holds_that_file_only(self, client, repo):
        _api_save(client, path=_PATH)

        assert _git(repo, "log", "-1", "--format=%s").strip() == "palinode auto-save: team/designer/role.md"
        assert _head_files(repo) == [_PATH]

    def test_a_resave_commits_again_under_the_same_message(self, client, repo):
        _api_save(client, path=_PATH, content="First body.")
        _api_save(client, path=_PATH, content="Second body.")

        assert _git(repo, "log", "-2", "--format=%s").split("\n")[:2] == [
            "palinode auto-save: team/designer/role.md",
            "palinode auto-save: team/designer/role.md",
        ]
        assert _head_files(repo) == [_PATH]

    def test_a_save_without_path_keeps_its_message(self, client, repo):
        _api_save(client, slug="probe")

        assert _git(repo, "log", "-1", "--format=%s").strip() == "palinode auto-save: insights/probe.md"
        assert _head_files(repo) == ["insights/probe.md"]
