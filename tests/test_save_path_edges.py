# ruff: noqa: F811
"""Edges of saving to a path: A11 parity, A12 citable names, A13 errors that are the caller's.

A11  ``path`` is a canonical parameter of ``save`` on every surface, so the parity
     contract (ADR-010) covers it.
A12  A path whose ref form (the path without ``.md``) fails the typed-link ref check is
     refused like any other bad path, because the file could never be cited as a link.
A13  An operating-system error that the caller caused (a name over the filesystem limit,
     a file used as a folder, a symlink loop) is a 4xx, an MCP error and a non-zero exit,
     each naming its condition. Any other OSError stays a 500.

Fixtures and helpers are those of ``test_save_path.py``: real route, real MCP dispatcher,
real CLI command, real filesystem under ``tmp_path``; the embedder and the scanner are stubs.
"""
from __future__ import annotations

import errno
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.core.parity import REGISTRY
from palinode.core.save import SaveValidationError, save_memory
from palinode.core.typed_links import TypedLinkError, normalize_link_refs
from test_save_path import (  # noqa: F401  (fixtures are used by name)
    _BODY,
    _BROKEN,
    _PATH,
    _api_save,
    _cli_save,
    _directories,
    _files,
    _mcp_save,
    _says_one_of,
    cli_via_api,
    mcp_via_api,
    memory_dir,
    no_network,
)

@pytest.fixture()
def client(memory_dir):
    """Answers as a real server does: an unhandled error is a 500 response, not a raise."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


PLUGIN = Path(__file__).resolve().parent.parent / "plugin" / "index.ts"


# ── A11 · parity ────────────────────────────────────────────────────────────

def _save_operation():
    return next(op for op in REGISTRY if op.name == "save")


class TestParity:

    def test_the_registry_lists_path_as_an_optional_string_of_save(self):
        params = {p.name: p for p in _save_operation().canonical_params}

        assert "path" in params
        assert params["path"].type == "string"
        assert not params["path"].required

    def test_the_plugin_declares_path_on_its_save_tool(self):
        source = PLUGIN.read_text(encoding="utf-8")
        start = source.index('name: "palinode_save",')
        block = source[start : source.index('{ name: "palinode_save" }', start)]

        assert re.search(r"\bpath:\s*Type\.Optional\(\s*Type\.String\(", block)

    def test_the_cli_exposes_path_under_its_canonical_name(self):
        from test_surface_parity import _cli_param_names, _resolve_cli_command

        names = _cli_param_names(_resolve_cli_command(_save_operation().cli_command))

        assert "path" in names

    @pytest.mark.asyncio
    async def test_the_mcp_tool_exposes_path_under_its_canonical_name(self):
        tools = {t.name: t for t in await mcp.list_tools()}

        assert "path" in tools["palinode_save"].input_schema["properties"]

    def test_the_api_request_exposes_path_under_its_canonical_name(self):
        assert "path" in app.openapi()["components"]["schemas"]["SaveRequest"]["properties"]


# ── A12 · citable names ─────────────────────────────────────────────────────

#: path → why a link could not cite it
_UNCITABLE = [
    "my notes/x.md",
    "team/my role.md",
    "team/role .md",
    "-draft/x.md",
    "-x.md",
    ".hidden/x.md",
    ".x.md",
    "zażółć/x.md",
    "team/café.md",
    "team/日本.md",
    "team/a#b.md",
    "team/a?b.md",
    "team/a%20b.md",
    "team/a+b.md",
    "team/a@b.md",
    "team/a(b).md",
    "team/a,b.md",
    "team/a~b.md",
    "team/a..b.md",
    "a/..b/c.md",
]

_CITABLE = [
    "team/designer/role.md",
    "Team/Designer_Role.md",
    "x/y/z/deep-name_1.v2.md",
    "9lives/x.md",
    "team/.gitkeep-notes/x.md",
    "team/a.b/c.d.md",
    "gitlab/x.md",
    "team/-x.md",
]


class TestCitableNames:

    @pytest.mark.parametrize("written", _UNCITABLE)
    def test_api_refuses_a_path_no_link_could_cite(self, client, memory_dir, written):
        res = _api_save(client, path=written)

        assert 400 <= res.status_code < 500, (written, res.status_code, res.text)
        assert "path" in res.text.lower()
        assert "link" in res.text.lower(), res.text

    @pytest.mark.parametrize("written", _UNCITABLE)
    def test_a_refused_save_writes_nothing_and_makes_no_folder(self, client, memory_dir, written):
        _api_save(client, path=written)

        assert _files(memory_dir) == []
        assert _directories(memory_dir) == []

    @pytest.mark.parametrize("written", _UNCITABLE)
    def test_core_raises_a_save_validation_error(self, memory_dir, written):
        with pytest.raises(SaveValidationError) as raised:
            save_memory(content=_BODY, type="Insight", path=written)

        assert "link" in str(raised.value).lower()

    @pytest.mark.parametrize("written", _UNCITABLE[:6])
    def test_mcp_returns_an_error_naming_the_link(self, mcp_via_api, memory_dir, written):
        text = _mcp_save(path=written)

        assert text.startswith(mcp.DISPATCH_ERROR_PREFIXES), text
        assert "link" in text.lower()
        assert _files(memory_dir) == []

    @pytest.mark.parametrize("written", _UNCITABLE[:6])
    def test_cli_exits_non_zero_and_writes_nothing(self, cli_via_api, memory_dir, written):
        result = _cli_save("--path", written)

        assert result.exit_code != 0, (written, result.output)
        assert "API returned 400" in result.output
        assert _files(memory_dir) == []

    @pytest.mark.parametrize("written", _CITABLE)
    def test_a_path_a_link_can_cite_is_still_written(self, client, memory_dir, written):
        res = _api_save(client, path=written)

        assert res.status_code == 200, (written, res.text)
        assert _files(memory_dir) == [written]

    @pytest.mark.parametrize("written", _UNCITABLE + _CITABLE)
    def test_the_rule_is_the_typed_link_ref_check_on_the_path_without_md(
        self, client, memory_dir, written
    ):
        ref = written.removesuffix(".md")
        try:
            citable = normalize_link_refs([ref], "path") == [ref]
        except TypedLinkError:
            citable = False

        res = _api_save(client, path=written)

        assert (res.status_code == 200) is citable, (written, res.status_code, res.text)

    def test_the_message_differs_from_every_other_path_condition(self, client, memory_dir):
        messages = {
            condition: json.dumps(_api_save(client, path=paths[0]).json().get("detail"))
            for condition, (paths, _words) in _BROKEN.items()
        }
        res = _api_save(client, path="my notes/x.md")

        assert 400 <= res.status_code < 500, res.text
        assert json.dumps(res.json().get("detail")) not in messages.values()


# ── A13 · errors the caller caused ──────────────────────────────────────────

_LONG_NAME = "n" * 300 + ".md"
_LONG_PATH = "/".join(["d" * 100] * 50) + "/x.md"


def _make_file_used_as_folder(mem: Path) -> str:
    (mem / "people").mkdir()
    (mem / "people" / "a.md").write_text("---\nid: a\n---\nThe file.\n", encoding="utf-8")
    return "people/a.md/x.md"


def _make_deep_file_used_as_folder(mem: Path) -> str:
    _make_file_used_as_folder(mem)
    return "people/a.md/b/c.md"


def _make_a_folder_used_as_a_file(mem: Path) -> str:
    (mem / "people" / "a.md").mkdir(parents=True)
    return "people/a.md"


def _make_a_self_loop(mem: Path) -> str:
    os.symlink("ring", mem / "ring")
    return "ring/x.md"


def _make_a_loop_of_two(mem: Path) -> str:
    os.symlink("two", mem / "one")
    os.symlink("one", mem / "two")
    return "one/deep/x.md"


#: condition → (how to build the path, words that name it)
_OS_CASES = {
    "name too long": (lambda mem: f"team/{_LONG_NAME}", ["too long", "limit", "length"]),
    "path too long": (lambda mem: _LONG_PATH, ["too long", "limit", "length"]),
    "file used as a folder": (_make_file_used_as_folder, ["folder", "directory"]),
    "file used as a deep folder": (_make_deep_file_used_as_folder, ["folder", "directory"]),
    "folder used as a file": (_make_a_folder_used_as_a_file, ["folder", "directory"]),
    "symlink loop": (_make_a_self_loop, ["symbolic link"]),
    "symlink loop of two": (_make_a_loop_of_two, ["symbolic link"]),
}
_OS_IDS = list(_OS_CASES)


def _built(memory_dir, condition) -> tuple[str, list[str]]:
    build, words = _OS_CASES[condition]
    return build(memory_dir), words


class TestErrorsTheCallerCaused:

    @pytest.mark.parametrize("condition", _OS_IDS)
    def test_api_answers_a_4xx_that_names_the_condition(self, client, memory_dir, condition):
        written, words = _built(memory_dir, condition)

        res = _api_save(client, path=written)

        assert 400 <= res.status_code < 500, (condition, res.status_code, res.text)
        assert _says_one_of(res.text, words), (condition, res.text)

    @pytest.mark.parametrize("condition", _OS_IDS)
    def test_core_raises_a_save_validation_error(self, memory_dir, condition):
        written, words = _built(memory_dir, condition)

        with pytest.raises(SaveValidationError) as raised:
            save_memory(content=_BODY, type="Insight", path=written)

        assert _says_one_of(str(raised.value), words), (condition, str(raised.value))

    @pytest.mark.parametrize("condition", _OS_IDS)
    def test_mcp_returns_an_error_that_names_the_condition(
        self, mcp_via_api, memory_dir, condition
    ):
        written, words = _built(memory_dir, condition)

        text = _mcp_save(path=written)

        assert text.startswith(mcp.DISPATCH_ERROR_PREFIXES), (condition, text)
        assert _says_one_of(text, words), (condition, text)

    @pytest.mark.parametrize("condition", _OS_IDS)
    def test_cli_exits_non_zero_on_the_4xx(self, cli_via_api, memory_dir, condition):
        written, _words = _built(memory_dir, condition)

        result = _cli_save("--path", written)

        assert result.exit_code != 0, (condition, result.output)
        assert "API returned 400" in result.output

    @pytest.mark.parametrize("condition", _OS_IDS)
    def test_a_refused_save_leaves_the_store_as_it_was(self, client, memory_dir, condition):
        written, _words = _built(memory_dir, condition)
        before = (_files(memory_dir), _directories(memory_dir))

        _api_save(client, path=written)

        assert (_files(memory_dir), _directories(memory_dir)) == before

    def test_the_file_used_as_a_folder_keeps_its_content(self, client, memory_dir):
        written = _make_file_used_as_folder(memory_dir)
        content = (memory_dir / "people" / "a.md").read_text(encoding="utf-8")

        _api_save(client, path=written)

        assert (memory_dir / "people" / "a.md").read_text(encoding="utf-8") == content

    def test_each_condition_gets_its_own_message(self, client, memory_dir):
        messages = {}
        for condition in ("name too long", "file used as a folder", "symlink loop"):
            written, _words = _built(memory_dir, condition)
            messages[condition] = json.dumps(_api_save(client, path=written).json().get("detail"))

        assert len(set(messages.values())) == len(messages), messages

    def test_the_same_save_succeeds_once_the_obstacle_is_gone(self, client, memory_dir):
        written = _make_file_used_as_folder(memory_dir)
        assert _api_save(client, path=written).status_code == 400

        (memory_dir / "people" / "a.md").unlink()

        assert _api_save(client, path=written).status_code == 200
        assert (memory_dir / written).is_file()


class TestAnyOtherOsErrorIsOurs:
    """A failure the caller did not cause is a server fault, and says so."""

    @pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC, errno.EACCES, errno.EROFS])
    def test_a_failure_making_a_folder_answers_500(self, client, memory_dir, code):
        real = os.makedirs

        def fail_for_the_folder_under_test(name, *args, **kwargs):
            if os.path.basename(os.fspath(name)) == "victim":
                raise OSError(code, os.strerror(code))
            return real(name, *args, **kwargs)

        with patch("os.makedirs", fail_for_the_folder_under_test):
            res = _api_save(client, path="victim/x.md")

        assert res.status_code == 500, (code, res.text)

    @pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC, errno.EACCES, errno.EROFS])
    def test_a_failure_writing_the_file_answers_500(self, client, memory_dir, code):
        with patch(
            "palinode.core.git_tools.write_memory_file",
            side_effect=OSError(code, os.strerror(code)),
        ):
            res = _api_save(client, path="team/x.md")

        assert res.status_code == 500, (code, res.text)

    def test_core_lets_the_os_error_through(self, memory_dir):
        with patch(
            "palinode.core.git_tools.write_memory_file",
            side_effect=OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)),
        ):
            with pytest.raises(OSError) as raised:
                save_memory(content=_BODY, type="Insight", path=_PATH)

        assert not isinstance(raised.value, SaveValidationError)
