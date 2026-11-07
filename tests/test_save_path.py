"""A save can name the file it writes: ``path``, relative to the memory dir.

Today a save writes ``<category>/<slug>.md``, the category fixed by ``type``.
A store that lays its files out by something other than type (one folder per
team role, say) cannot save into it. ``path`` is an opt-in on every surface (API
``POST /save``, MCP ``palinode_save``, CLI ``palinode save --path``) and the
core ``save_memory``, and leaves every save without it as it was.

Tests are named after the behaviours A1–A10 of the feature's spec; the number
is in each section header. They go through the real route, the real MCP
dispatcher and the real CLI command, wired to the real API by an in-process
client, so what is pinned is what a caller sees. Real SQLite, real git and the
real filesystem under ``tmp_path``; the embedder and the content scanner are
stubbed, so nothing reaches the network.
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import frontmatter
import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api import server as srv
from palinode.api.server import app
from palinode.cli import main as cli_main
from palinode.cli._api import PalinodeAPI
from palinode.core import store
from palinode.core.config import config
from palinode.core.save import SaveValidationError, save_memory

save_cli = importlib.import_module("palinode.cli.save")

_FAKE_VECTOR = [0.01] * 1024

_PATH = "team/designer/role.md"
_BODY = "Quokka marmalade ledger for the designer's role."

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+(?:\+00:00|Z)")


# ── fixtures and surfaces ───────────────────────────────────────────────────

@pytest.fixture()
def memory_dir(tmp_path, monkeypatch) -> Path:
    mem = tmp_path / "mem"
    mem.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(mem))
    monkeypatch.setattr(config, "db_path", str(mem / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    srv._rate_counters.clear()
    store.init_db()
    yield mem
    srv._rate_counters.clear()


@pytest.fixture(autouse=True)
def no_network():
    """The scanner and the embedder are stubs; the embedder is recorded."""
    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR),
    ):
        yield


@pytest.fixture()
def client(memory_dir):
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def mcp_via_api(client, monkeypatch):
    """The MCP tool's HTTP hop, answered by the real API in-process."""

    async def post(path, json=None, timeout=30.0):
        return client.post(path, json=json)

    monkeypatch.setattr(mcp, "_post", post)


@pytest.fixture()
def cli_via_api(client, monkeypatch):
    """The CLI's HTTP client, answered by the real API in-process.

    The test client of this stack raises its own ``HTTPStatusError``, which the
    CLI does not catch as it catches httpx's, so the answer is relayed as an
    ``httpx.Response``: what the CLI sees from a real server.
    """

    def relay(request: httpx.Request) -> httpx.Response:
        answer = client.request(request.method, request.url.path, content=request.content,
                                headers={"content-type": "application/json"})
        return httpx.Response(answer.status_code, content=answer.content,
                              headers={"content-type": "application/json"})

    http = httpx.Client(transport=httpx.MockTransport(relay), base_url="http://test")
    monkeypatch.setattr(save_cli, "api_client", PalinodeAPI(client=http))
    yield
    http.close()


def _body(**fields) -> dict:
    return {"content": _BODY, "type": "Insight", **fields}


def _api_save(client, **fields) -> httpx.Response:
    return client.post("/save", json=_body(**fields))


def _mcp_save(**arguments) -> str:
    import anyio

    async def call():
        result = await mcp._dispatch_tool(
            "palinode_save", {"content": _BODY, "type": "Insight", **arguments}
        )
        return result[0].text

    return anyio.run(call)


def _cli_save(*args: str, content: str = _BODY):
    return CliRunner().invoke(cli_main, ["save", "--type", "Insight", *args, content])


def _read(mem: Path, rel: str):
    return frontmatter.load(mem / rel)


def _files(mem: Path) -> list[str]:
    """Every markdown file under the memory dir, POSIX, relative."""
    return sorted(p.relative_to(mem).as_posix() for p in mem.rglob("*.md"))


def _directories(mem: Path) -> list[str]:
    return sorted(
        p.relative_to(mem).as_posix() for p in mem.rglob("*") if p.is_dir() and ".git" not in p.parts
    )


# ── A1 · surfaces ───────────────────────────────────────────────────────────

class TestSurfaces:

    def test_api_accepts_path(self, client, memory_dir):
        res = _api_save(client, path=_PATH)

        assert res.status_code == 200, res.text
        assert (memory_dir / _PATH).is_file()

    def test_api_documents_path_in_its_schema(self):
        props = app.openapi()["components"]["schemas"]["SaveRequest"]["properties"]

        assert "path" in props
        assert "string" in json.dumps(props["path"])

    def test_core_accepts_path(self, memory_dir):
        save_memory(content=_BODY, type="Insight", path=_PATH)

        assert (memory_dir / _PATH).is_file()

    def test_mcp_accepts_path(self, mcp_via_api, memory_dir):
        text = _mcp_save(path=_PATH)

        assert not text.startswith(mcp.DISPATCH_ERROR_PREFIXES), text
        assert (memory_dir / _PATH).is_file()

    @pytest.mark.asyncio
    async def test_mcp_declares_path_in_the_tool_schema(self):
        tools = {t.name: t for t in await mcp.list_tools()}
        props = tools["palinode_save"].input_schema["properties"]

        assert props["path"]["type"] == "string"
        assert "path" not in tools["palinode_save"].input_schema.get("required", [])

    def test_cli_accepts_path(self, cli_via_api, memory_dir):
        result = _cli_save("--path", _PATH)

        assert result.exit_code == 0, result.output
        assert (memory_dir / _PATH).is_file()

    def test_cli_lists_path_in_its_help(self):
        result = CliRunner().invoke(cli_main, ["save", "--help"])

        assert "--path" in result.output


# ── A2 · where it lands ─────────────────────────────────────────────────────

class TestWhereItLands:

    def test_the_file_is_written_at_the_path_under_the_memory_dir(self, client, memory_dir):
        _api_save(client, path=_PATH)

        assert _files(memory_dir) == [_PATH]
        assert _BODY in (memory_dir / _PATH).read_text(encoding="utf-8")

    def test_nothing_is_written_under_the_types_category_folder(self, client, memory_dir):
        _api_save(client, path=_PATH, type="Decision")

        assert not (memory_dir / "decisions").exists()
        assert not (memory_dir / "insights").exists()

    @pytest.mark.parametrize(
        "memory_type",
        ["PersonMemory", "Decision", "ProjectSnapshot", "Insight", "ResearchRef", "ActionItem"],
    )
    def test_the_type_still_sets_type_in_the_front_matter(self, client, memory_dir, memory_type):
        res = _api_save(client, path=_PATH, type=memory_type)

        assert res.status_code == 200, res.text
        assert _read(memory_dir, _PATH).metadata["type"] == memory_type

    def test_a_path_without_a_folder_lands_at_the_memory_root(self, client, memory_dir):
        _api_save(client, path="role.md")

        assert _files(memory_dir) == ["role.md"]

    def test_the_path_is_the_name_whatever_the_first_line_says(self, client, memory_dir):
        _api_save(client, path="team/a.md", content="Some other first line\n\nbody")

        assert _files(memory_dir) == ["team/a.md"]


# ── A3 · valid paths ────────────────────────────────────────────────────────

class TestValidPaths:

    @pytest.mark.parametrize(
        "written, landed",
        [
            ("a.md", "a.md"),
            ("team/designer/role.md", "team/designer/role.md"),
            ("x/y/z/deep-name_1.v2.md", "x/y/z/deep-name_1.v2.md"),
            ("team\\designer\\role.md", "team/designer/role.md"),
            ("team\\designer/role.md", "team/designer/role.md"),
        ],
    )
    def test_a_relative_md_path_is_written_and_a_backslash_reads_as_a_slash(
        self, client, memory_dir, written, landed
    ):
        res = _api_save(client, path=written)

        assert res.status_code == 200, res.text
        assert _files(memory_dir) == [landed]


#: condition → (paths that break it, words the message may name it by)
_BROKEN = {
    "not .md": (["role", "team/role.txt", "team/role.md.bak", "team/"], [".md"]),
    "parent segment": (
        ["../x.md", "a/../../x.md", "a/../x.md", "..\\x.md", "a\\..\\x.md"],
        [".."],
    ),
    "absolute or drive": (
        ["/abs/x.md", "\\abs\\x.md", "C:/x.md", "C:\\x.md", "\\\\server\\share\\x.md"],
        ["absolute", "relative", "drive"],
    ),
    "reserved folder": (
        [
            ".git/x.md", ".git\\x.md", ".palinode/x.md", ".palinode/deep/x.md", ".git/hooks/pre.md",
            "specs/x.md", "prompts/x.md", "logs/x.md", ".obsidian/x.md",
            "team/logs/x.md", "a/b/specs/c/x.md", "specs\\x.md",
            ".GIT/x.md", "LOGS/x.md", "prompts./x.md", ".git /x.md",
        ],
        ["reserved"],
    ),
}
_BROKEN_PATHS = [(c, p, w) for c, (ps, w) in _BROKEN.items() for p in ps]


def _says_one_of(text: str, words: list[str]) -> bool:
    return any(word in text.lower() for word in words)


class TestRefusedPaths:

    @pytest.mark.parametrize("condition, written, words", _BROKEN_PATHS)
    def test_api_refuses_with_a_4xx_that_names_the_condition(
        self, client, memory_dir, condition, written, words
    ):
        res = _api_save(client, path=written)

        assert 400 <= res.status_code < 500, (condition, written, res.status_code)
        assert "path" in res.text.lower()
        assert _says_one_of(res.text, words), (condition, res.text)

    @pytest.mark.parametrize("condition, written, words", _BROKEN_PATHS)
    def test_a_refused_save_writes_nothing_and_makes_no_folder(
        self, client, memory_dir, condition, written, words
    ):
        _api_save(client, path=written)

        assert _files(memory_dir) == []
        assert _directories(memory_dir) == []
        assert not (memory_dir.parent / "x.md").exists()

    @pytest.mark.parametrize("condition, written, words", _BROKEN_PATHS)
    def test_core_raises_a_save_validation_error(self, memory_dir, condition, written, words):
        with pytest.raises(SaveValidationError) as raised:
            save_memory(content=_BODY, type="Insight", path=written)

        assert _says_one_of(str(raised.value), words), (condition, str(raised.value))

    @pytest.mark.parametrize("condition, written, words", _BROKEN_PATHS)
    def test_mcp_returns_an_error_that_names_the_condition(
        self, mcp_via_api, memory_dir, condition, written, words
    ):
        text = _mcp_save(path=written)

        assert text.startswith(mcp.DISPATCH_ERROR_PREFIXES), text
        assert _says_one_of(text, words), (condition, text)
        assert _files(memory_dir) == []

    @pytest.mark.parametrize("condition, written, words", _BROKEN_PATHS)
    def test_cli_exits_non_zero_and_writes_nothing(
        self, cli_via_api, memory_dir, condition, written, words
    ):
        result = _cli_save("--path", written)

        assert result.exit_code != 0, (condition, written, result.output)
        assert "API returned 400" in result.output
        assert _files(memory_dir) == []

    def test_each_condition_gets_its_own_message(self, client, memory_dir):
        messages = {}
        for condition, (paths, _words) in _BROKEN.items():
            res = _api_save(client, path=paths[0])
            messages[condition] = json.dumps(res.json().get("detail"))

        assert len(set(messages.values())) == len(_BROKEN), messages

    def test_a_path_that_only_looks_reserved_is_allowed(self, client, memory_dir):
        looks_reserved = ["team/.gitkeep-notes/x.md", "gitlab/x.md", "palinode/x.md", "gitignore-notes/x.md"]
        for written in looks_reserved:
            res = _api_save(client, path=written)
            assert res.status_code == 200, (written, res.text)

        assert _files(memory_dir) == sorted(looks_reserved)

    def test_a_dotted_name_that_is_not_a_parent_segment_is_allowed(self, client, memory_dir):
        res = _api_save(client, path="a/.b/c.d.md")

        assert res.status_code == 200, res.text
        assert _files(memory_dir) == ["a/.b/c.d.md"]


# ── A4 · one place ──────────────────────────────────────────────────────────

class TestPathWithSlug:

    def test_api_refuses_path_with_slug(self, client, memory_dir):
        res = _api_save(client, path=_PATH, slug="role")

        assert 400 <= res.status_code < 500, res.status_code
        assert "slug" in res.text.lower() and "path" in res.text.lower()
        assert _files(memory_dir) == []

    def test_core_refuses_path_with_slug(self, memory_dir):
        with pytest.raises(SaveValidationError) as raised:
            save_memory(content=_BODY, type="Insight", path=_PATH, slug="role")

        assert "slug" in str(raised.value).lower()
        assert _files(memory_dir) == []

    def test_mcp_refuses_path_with_slug(self, mcp_via_api, memory_dir):
        text = _mcp_save(path=_PATH, slug="role")

        assert text.startswith(mcp.DISPATCH_ERROR_PREFIXES), text
        assert "slug" in text.lower()
        assert _files(memory_dir) == []

    def test_cli_refuses_path_with_slug(self, cli_via_api, memory_dir):
        result = _cli_save("--path", _PATH, "--slug", "role")

        assert result.exit_code != 0, result.output
        assert "No such option" not in result.output
        assert _files(memory_dir) == []

    def test_a_slug_that_equals_the_path_stem_is_still_refused(self, client, memory_dir):
        res = _api_save(client, path="insights/role.md", slug="role")

        assert 400 <= res.status_code < 500
        assert _files(memory_dir) == []


# ── A5 · folders ────────────────────────────────────────────────────────────

class TestFolders:

    def test_missing_folders_on_the_path_are_created(self, client, memory_dir):
        res = _api_save(client, path="a/b/c/d.md")

        assert res.status_code == 200, res.text
        assert (memory_dir / "a" / "b" / "c").is_dir()
        assert _files(memory_dir) == ["a/b/c/d.md"]

    def test_an_existing_folder_is_reused_and_its_files_are_left_alone(self, client, memory_dir):
        (memory_dir / "team").mkdir()
        (memory_dir / "team" / "other.md").write_text("---\ntype: Insight\n---\n\nkept\n")

        _api_save(client, path="team/new.md")

        assert (memory_dir / "team" / "other.md").read_text().endswith("kept\n")
        assert _files(memory_dir) == ["team/new.md", "team/other.md"]

    def test_a_folder_is_created_through_every_surface(
        self, mcp_via_api, cli_via_api, memory_dir
    ):
        _mcp_save(path="m/n/one.md")
        _cli_save("--path", "c/d/two.md")

        assert _files(memory_dir) == ["c/d/two.md", "m/n/one.md"]


# ── A6 · an existing file ───────────────────────────────────────────────────

def _normalised(mem: Path, rel: str):
    """A saved file as a comparison can hold it: no id, no clock, no hash of the clock."""
    post = _read(mem, rel)
    meta = {
        k: _TIMESTAMP.sub("<t>", str(v))
        for k, v in post.metadata.items()
        if k not in {"id", "content_hash"}
    }
    return meta, _TIMESTAMP.sub("<t>", post.content)


class TestExistingFile:

    @pytest.mark.parametrize("policy", [None, "append", "replace"])
    def test_a_second_save_to_a_path_behaves_as_one_to_a_category_slug(
        self, client, memory_dir, policy
    ):
        policy_field = {} if policy is None else {"update_policy": policy}
        for number in (1, 2):
            _api_save(client, slug="role", content=f"Designer note number {number}.", **policy_field)
        by_slug = _normalised(memory_dir, "insights/role.md")

        for number in (1, 2):
            _api_save(client, path="team/role.md", content=f"Designer note number {number}.", **policy_field)
        by_path = _normalised(memory_dir, "team/role.md")

        assert by_path == by_slug

    def test_no_policy_overwrites_the_body_and_keeps_the_birth_date(self, client, memory_dir):
        first = _api_save(client, path=_PATH, content="First body.").json()
        created_at = _read(memory_dir, _PATH).metadata["created_at"]

        second = _api_save(client, path=_PATH, content="Second body.").json()

        post = _read(memory_dir, _PATH)
        assert "First body." not in post.content and "Second body." in post.content
        assert post.metadata["created_at"] == created_at
        assert (first["save_outcome"], second["save_outcome"]) == ("created", "replaced")

    def test_append_keeps_the_prior_body_and_adds_the_new_one_beneath(self, client, memory_dir):
        _api_save(client, path=_PATH, content="First body.", update_policy="append")
        res = _api_save(client, path=_PATH, content="Second body.", update_policy="append")

        content = _read(memory_dir, _PATH).content
        assert content.index("First body.") < content.index("Second body.")
        assert res.json()["save_outcome"] == "appended"

    def test_replace_marks_the_file_a_living_document(self, client, memory_dir):
        _api_save(client, path=_PATH, content="First body.", update_policy="replace")
        _api_save(client, path=_PATH, content="Second body.", update_policy="replace")

        post = _read(memory_dir, _PATH)
        assert post.metadata["update_policy"] == "replace"
        assert "First body." not in post.content

    def test_a_policy_is_sticky_on_a_path_as_it_is_on_a_slug(self, client, memory_dir):
        _api_save(client, path=_PATH, content="First body.", update_policy="append")
        _api_save(client, path=_PATH, content="Second body.")

        content = _read(memory_dir, _PATH).content
        assert "First body." in content and "Second body." in content

    def test_a_repeated_save_to_one_path_never_makes_a_sibling_file(self, client, memory_dir):
        for _ in range(3):
            _api_save(client, path=_PATH, content="The same first line\n\nbody")

        assert _files(memory_dir) == [_PATH]

    def test_one_file_name_in_two_folders_is_two_files(self, client, memory_dir):
        _api_save(client, path="team/designer/role.md", content="Designer.")
        _api_save(client, path="team/architect/role.md", content="Architect.")

        assert "Designer." in _read(memory_dir, "team/designer/role.md").content
        assert "Architect." in _read(memory_dir, "team/architect/role.md").content


# ── A7 · the commit ─────────────────────────────────────────────────────────

def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


@pytest.fixture()
def repo(client, memory_dir, monkeypatch) -> Path:
    """The memory dir as a git repo with a base commit, auto-commit on, and no
    ambient git identity or configuration."""
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_DIR", "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.setattr(config.git, "auto_push", False)
    _git(memory_dir, "init", "-q", "-b", "main")
    _git(memory_dir, "config", "user.name", "Palinode Tests")
    _git(memory_dir, "config", "user.email", "tests@example.com")
    (memory_dir / ".gitignore").write_text(".palinode.db*\n", encoding="utf-8")
    (memory_dir / "README.md").write_text("store readme\n", encoding="utf-8")
    _git(memory_dir, "add", "-A")
    _git(memory_dir, "commit", "-q", "-m", "base")
    return memory_dir


def _head_files(repo: Path) -> list[str]:
    out = _git(repo, "show", "--name-only", "--format=", "HEAD")
    return sorted(line for line in out.splitlines() if line)


def _staged(repo: Path) -> list[str]:
    return sorted(_git(repo, "diff", "--cached", "--name-status").splitlines())


class TestCommit:

    def test_the_commit_holds_that_path_only(self, client, repo):
        before = _git(repo, "rev-list", "--count", "HEAD").strip()

        res = _api_save(client, path=_PATH)

        assert res.json()["git_committed"] is True
        assert _head_files(repo) == [_PATH]
        assert int(_git(repo, "rev-list", "--count", "HEAD")) == int(before) + 1

    def test_other_staged_files_are_left_staged(self, client, repo):
        (repo / "notes.txt").write_text("hand edit\n", encoding="utf-8")
        (repo / "README.md").write_text("readme edit in progress\n", encoding="utf-8")
        _git(repo, "add", "--", "notes.txt", "README.md")
        staged = _staged(repo)

        _api_save(client, path=_PATH)

        assert _head_files(repo) == [_PATH]
        assert _staged(repo) == staged

    def test_a_second_save_commits_that_path_only(self, client, repo):
        _api_save(client, path=_PATH, content="First body.")
        (repo / "notes.txt").write_text("hand edit\n", encoding="utf-8")
        _git(repo, "add", "--", "notes.txt")

        _api_save(client, path=_PATH, content="Second body.")

        assert _head_files(repo) == [_PATH]
        assert _staged(repo) == ["A\tnotes.txt"]

    def test_a_refused_path_makes_no_commit(self, client, repo):
        before = _git(repo, "rev-parse", "HEAD")

        _api_save(client, path="../x.md")

        assert _git(repo, "rev-parse", "HEAD") == before

    def test_the_commit_is_made_through_the_other_surfaces_too(self, mcp_via_api, cli_via_api, repo):
        _mcp_save(path="m/one.md")
        assert _head_files(repo) == ["m/one.md"]

        _cli_save("--path", "c/two.md")
        assert _head_files(repo) == ["c/two.md"]


# ── A8 · the answer ─────────────────────────────────────────────────────────

class TestAnswer:

    def test_the_response_names_the_file_as_path(self, client, memory_dir):
        res = _api_save(client, path=_PATH).json()

        assert res["path"] == _PATH

    def test_core_returns_the_same_key(self, memory_dir):
        assert save_memory(content=_BODY, type="Insight", path=_PATH)["path"] == _PATH

    def test_a_backslash_in_the_request_is_a_slash_in_the_answer(self, client, memory_dir):
        res = _api_save(client, path="team\\designer\\role.md").json()

        assert res["path"] == "team/designer/role.md"
        assert "\\" not in res["path"]

    def test_the_answer_is_relative_and_agrees_with_rel_path(self, client, memory_dir):
        res = _api_save(client, path=_PATH).json()

        assert not res["path"].startswith("/")
        assert res["path"] == res["rel_path"]
        assert (memory_dir / res["path"]).samefile(res["file_path"])

    def test_the_cli_prints_the_path_in_json(self, cli_via_api, memory_dir):
        result = _cli_save("--path", _PATH, "--format", "json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.output[result.output.index("{"):])["path"] == _PATH

    def test_the_mcp_receipt_names_the_path(self, mcp_via_api, memory_dir):
        assert _PATH in _mcp_save(path=_PATH)


# ── A9 · found ──────────────────────────────────────────────────────────────

class TestFound:

    def test_search_returns_a_file_saved_to_a_path(self, client, memory_dir):
        _api_save(client, path=_PATH)

        res = client.post("/search", json={"query": "marmalade"})

        assert res.status_code == 200, res.text
        payload = res.json()
        hits = payload["results"] if isinstance(payload, dict) else payload
        assert _PATH in [hit.get("rel_path") for hit in hits], hits

    def test_search_finds_it_as_it_finds_one_saved_by_slug(self, client, memory_dir):
        _api_save(client, slug="by-slug", content="Quokka marmalade by slug.")
        _api_save(client, path="team/by-path.md", content="Quokka marmalade by path.")

        res = client.post("/search", json={"query": "marmalade"})
        payload = res.json()
        hits = payload["results"] if isinstance(payload, dict) else payload

        found = {hit.get("rel_path") for hit in hits}
        assert {"insights/by-slug.md", "team/by-path.md"} <= found, found

    def test_the_chunks_of_the_file_are_in_both_indexes(self, client, memory_dir):
        res = _api_save(client, path=_PATH).json()

        assert (memory_dir / _PATH).is_file()
        assert res["indexed"] is True
        assert (res["indexed_vec"], res["indexed_fts"]) == (True, True)

    def test_list_returns_a_file_saved_to_a_path(self, client, memory_dir):
        _api_save(client, path=_PATH, title="Designer role")

        listed = client.get("/list").json()

        entry = next(item for item in listed if item["file"] == _PATH)
        assert entry["title"] == "Designer role"
        assert entry["type"] == "Insight"

    def test_list_returns_it_beside_one_saved_by_slug(self, client, memory_dir):
        _api_save(client, slug="by-slug")
        _api_save(client, path="team/by-path.md")

        assert {item["file"] for item in client.get("/list").json()} == {
            "insights/by-slug.md",
            "team/by-path.md",
        }


# ── A10 · unchanged ─────────────────────────────────────────────────────────

class TestUnchanged:

    @pytest.mark.parametrize(
        "memory_type, folder",
        [
            ("PersonMemory", "people"),
            ("Decision", "decisions"),
            ("ProjectSnapshot", "projects"),
            ("Insight", "insights"),
            ("ResearchRef", "research"),
            ("ActionItem", "inbox"),
        ],
    )
    def test_a_save_without_path_lands_in_its_types_folder(
        self, client, memory_dir, memory_type, folder
    ):
        res = _api_save(client, type=memory_type, slug="probe")

        assert res.status_code == 200, res.text
        assert _files(memory_dir) == [f"{folder}/probe.md"]

    def test_a_save_without_path_has_no_path_in_its_answer(self, client, memory_dir):
        res = _api_save(client, slug="probe").json()

        assert "path" not in res
        assert res["rel_path"] == "insights/probe.md"

    def test_a_null_path_is_no_path(self, client, memory_dir):
        res = _api_save(client, slug="probe", path=None)

        assert res.status_code == 200, res.text
        assert _files(memory_dir) == ["insights/probe.md"]

    def test_a_derived_slug_that_collides_is_still_disambiguated(self, client, memory_dir):
        first = _api_save(client, content="Same first line\n\none").json()
        second = _api_save(client, content="Same first line\n\ntwo").json()

        assert (first["save_outcome"], second["save_outcome"]) == ("created", "disambiguated")
        assert len(_files(memory_dir)) == 2

    def test_core_save_without_path_is_the_same_call_as_before(self, memory_dir):
        result = save_memory(content=_BODY, type="Insight", slug="probe")

        assert result["rel_path"] == "insights/probe.md"
        assert "path" not in result

    def test_a_slug_with_separators_is_sanitised_and_never_read_as_a_path(self, client, memory_dir):
        _api_save(client, slug="Mixed Case/../Slug")

        assert _files(memory_dir) == ["insights/mixed-case-slug.md"]
