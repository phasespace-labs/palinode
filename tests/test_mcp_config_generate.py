"""Installation resolution, client formats and preview safety for MCP configuration."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tomllib

import click
from click.testing import CliRunner
import pytest
import yaml

from palinode.cli import main

mod = importlib.import_module("palinode.cli.mcp_config")


def script(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch):
    folder = tmp_path / "installed space" / "bin"
    executable = script(folder / "palinode-mcp")
    monkeypatch.setattr(mod.sys, "executable", str(folder / "python"))
    monkeypatch.setattr(mod.sysconfig, "get_path", lambda _: str(folder))
    monkeypatch.setattr(mod.metadata, "distribution", lambda _: SimpleNamespace(files=[]))
    return executable


def test_resolution_ignores_path(installation, tmp_path, monkeypatch):
    other = script(tmp_path / "wrong" / "palinode-mcp")
    monkeypatch.setenv("PATH", str(other.parent))
    assert mod._resolve_executable() == str(installation)


def test_recorded_install_wins_over_interpreter_sibling(installation, tmp_path, monkeypatch):
    user_install = script(tmp_path / "user install" / "palinode-mcp")
    monkeypatch.setattr(mod.metadata, "distribution", lambda _: SimpleNamespace(
        files=[Path("../../../bin/palinode-mcp")], locate_file=lambda _: user_install))
    assert mod._resolve_executable() == str(user_install)


def test_missing_recorded_script_does_not_select_other_install(installation, monkeypatch):
    monkeypatch.setattr(mod.metadata, "distribution", lambda _: SimpleNamespace(
        files=[Path("bin/palinode-mcp")], locate_file=lambda _: installation / "missing"))
    with pytest.raises(click.ClickException, match="Missing.*reinstall.*--executable"):
        mod._resolve_executable()


def test_missing_script(installation):
    installation.unlink()
    with pytest.raises(click.ClickException, match="Missing.*--executable"):
        mod._resolve_executable()


def test_non_executable(installation):
    installation.chmod(0o644)
    with pytest.raises(click.ClickException, match="non-executable"):
        mod._resolve_executable()


def test_ambiguous_scripts(installation, tmp_path, monkeypatch):
    other = script(tmp_path / "another" / "palinode-mcp")
    monkeypatch.setattr(mod.sysconfig, "get_path", lambda _: str(other.parent))
    with pytest.raises(click.ClickException, match="Ambiguous.*--executable"):
        mod._resolve_executable()
    assert mod._resolve_executable(str(installation)) == str(installation)


@pytest.mark.parametrize("value", ["relative/bin/palinode-mcp", "/bin/../palinode-mcp"])
def test_explicit_path_validation(value):
    with pytest.raises(click.ClickException, match="absolute path without"):
        mod._resolve_executable(value)


def test_explicit_symlink_rejected(installation, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(installation)
    with pytest.raises(click.ClickException, match="symlink"):
        mod._resolve_executable(str(link))


@pytest.mark.parametrize("editor", mod.EDITORS)
@pytest.mark.parametrize("transport", ["--stdio", "--http"])
def test_client_native_shapes_and_no_overwrite(editor, transport, installation, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / ".mcp.json"
    sentinel.write_text('{"mcpServers":{"other":{"command":"unchanged"}}}')
    before = sentinel.read_bytes()
    args = ["mcp-config", transport, "--editor", editor]
    if transport == "--http":
        args += ["--url", "https://example.test/mcp/", "--bearer", 'token"\\value']
    result = CliRunner().invoke(main, args)
    assert sentinel.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [".mcp.json", "installed space"]
    if editor == "claude-desktop" and transport == "--http":
        assert result.exit_code == 2
        return
    assert result.exit_code == 0, result.output
    block = (tomllib.loads(result.output) if editor == "codex" else
             yaml.safe_load(result.output) if editor == "continue" else json.loads(result.output))
    entry = (block["mcp_servers"]["palinode"] if editor == "codex" else
             block["mcpServers"][0] if editor == "continue" else block["mcpServers"]["palinode"])
    if transport == "--stdio":
        assert entry["command"] == str(installation)
        assert "PALINODE_API_PORT" in entry["env"]
        if editor == "claude-code":
            assert entry["type"] == "stdio"
    else:
        assert entry["url"] == "https://example.test/mcp/"
        headers = (entry["http_headers"] if editor == "codex" else
                   entry["requestOptions"]["headers"] if editor == "continue" else entry["headers"])
        assert headers["Authorization"] == 'Bearer token"\\value'
        if editor == "codex":
            assert "type" not in entry
        if editor == "continue":
            assert entry["type"] == "streamable-http"


@pytest.mark.parametrize("args", [
    ["--stdio", "--editor", "unsupported"], ["--editor", "codex"],
    ["--http", "--executable", "/somewhere"], ["--stdio", "--bearer", "secret"],
    ["--stdio", "--editor", "codex", "--json"],
    ["--http", "--editor", "continue", "--json"],
])
def test_invalid_options_fail_before_output(args):
    result = CliRunner().invoke(main, ["mcp-config", *args])
    assert result.exit_code == 2
    assert '"mcpServers"' not in result.output
    assert "secret" not in result.output


def test_connection_env_allowlist(installation, tmp_path, monkeypatch):
    from palinode.core.config import config
    monkeypatch.setattr(config, "memory_dir", str(tmp_path / "memory space"))
    monkeypatch.setattr(config.services.api, "host", "remote.example.test")
    monkeypatch.setattr(config.services.api, "port", 9876)
    monkeypatch.setenv("PALINODE_API_TOKEN", "explicit-secret")
    monkeypatch.setenv("PALINODE_API_TOKEN_FILE", "token file")
    monkeypatch.setenv("PALINODE_PROJECT", "project/sample")
    monkeypatch.setenv("UNRELATED_SECRET", "never-copy")
    monkeypatch.chdir(tmp_path)
    entry = mod._build_stdio_entry()
    assert entry["env"]["PALINODE_API_HOST"] == "remote.example.test"
    assert entry["env"]["PALINODE_API_PORT"] == "9876"
    assert entry["env"]["PALINODE_API_TOKEN"] == "explicit-secret"
    assert entry["env"]["PALINODE_API_TOKEN_FILE"] == str(tmp_path / "token file")
    assert entry["env"]["PALINODE_PROJECT"] == "project/sample"
    assert "never-copy" not in json.dumps(entry)


@pytest.mark.parametrize("value", ["../escape", "project/alpha", "two words", ""])
def test_project_option_rejects_non_slug(value):
    result = CliRunner().invoke(main, ["mcp-config", "--stdio", "--project", value])
    assert result.exit_code == 2
    assert "slug" in result.output


def test_project_option_needs_a_transport():
    result = CliRunner().invoke(main, ["mcp-config", "--project", "alpha"])
    assert result.exit_code == 2
    assert "requires --stdio or --http" in result.output


@pytest.mark.parametrize("editor,path", [
    ("generic", ("mcpServers", "palinode", "headers")),
    ("claude-code", ("mcpServers", "palinode", "headers")),
    ("codex", ("mcp_servers", "palinode", "http_headers")),
    ("continue", ("mcpServers", 0, "requestOptions", "headers")),
])
def test_http_project_is_carried_in_the_project_header(editor, path):
    """An HTTP server cannot see its client's directory or environment.

    The project therefore rides on every request as a header, in each
    client's own header field, and a bearer token still sits beside it.
    """
    args = ["mcp-config", "--http", "--editor", editor, "--project", "harbor-notes",
            "--bearer", "tok"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    if editor == "codex":
        block = tomllib.loads(result.output)
    elif editor == "continue":
        block = yaml.safe_load(result.output)
    else:
        block = json.loads(result.output)
    for key in path:
        block = block[key]
    assert block == {"Authorization": "Bearer tok", "X-Palinode-Project": "harbor-notes"}


def test_http_project_header_is_not_redacted_as_a_credential():
    from palinode.cli.mcp_config import _redact

    entry = {"headers": {"Authorization": "Bearer tok", "X-Palinode-Project": "harbor-notes"}}
    assert _redact(entry) == {
        "headers": {"Authorization": "<redacted>", "X-Palinode-Project": "harbor-notes"},
    }


def test_generated_project_configs_isolate_clients_in_a_linked_worktree(
    installation, tmp_path, monkeypatch,
):
    """Each generated stdio env resolves independently despite a task worktree name."""
    checkout = tmp_path / "renamed-checkout"
    checkout.mkdir()
    worktree = tmp_path / "unrelated-task-name"

    def git(*args):
        subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.name=Tests", "-c", "user.email=tests@example.com",
        "commit", "--allow-empty", "-qm", "fixture")
    git("remote", "add", "origin", "git@example.com:team/git-derived-name.git")
    git("worktree", "add", "-qb", "task", str(worktree))

    generated = []
    for project in ("alpha-client", "beta-client"):
        result = CliRunner().invoke(main, [
            "mcp-config", "--stdio", "--editor", "codex", "--project", project,
            "--executable", str(installation),
        ])
        assert result.exit_code == 0, result.output
        generated.append(tomllib.loads(result.output)["mcp_servers"]["palinode"]["env"])

    from palinode.cli.search import _cli_resolve_context
    from palinode.core.context_prime import resolve_context

    for project, env in zip(("alpha-client", "beta-client"), generated, strict=True):
        # patch.dict models two separately-launched stdio server processes.
        with monkeypatch.context() as client_env:
            client_env.setenv("CWD", str(worktree))
            client_env.setenv("PALINODE_PROJECT", env["PALINODE_PROJECT"])
            assert _cli_resolve_context() == [f"project/{project}"]
            resolution = resolve_context(cwd=str(worktree))
            assert resolution.project == f"project/{project}"
            assert resolution.basis == "environment"
            # A tool-call project remains the explicit higher-precedence path.
            assert resolve_context(cwd=str(worktree), project="per-call").project == "project/per-call"

    assert generated[0]["PALINODE_PROJECT"] != generated[1]["PALINODE_PROJECT"]


@pytest.mark.parametrize("editor", mod.EDITORS[:-1])
def test_interactive_http_preview_hides_credentials(editor, monkeypatch, capsys):
    monkeypatch.setattr(mod.sys.stdout, "isatty", lambda: True)
    mod._emit_config(emit_http=True, url="https://user:password@example.test/mcp/?token=secret",
                     host="unused", port=1, bearer="bearer-secret", output_json=False, editor=editor)
    output = capsys.readouterr().out
    for secret in ("password", "token=secret", "bearer-secret"):
        assert secret not in output
    assert "Credentials hidden" in output
    assert "Merge" in output


def test_diagnostic_diff_hides_secrets():
    entries = [{"command": "palinode-mcp", "env": {"PALINODE_API_TOKEN": value}}
               for value in ("first-secret", "second-secret")]
    results = [mod.ConfigResult("test", Path(f"{i}.json"), True, entry,
                                json.dumps(entry), None) for i, entry in enumerate(entries)]
    output = json.dumps([r.to_dict() for r in results]) + str(mod._check_divergence(results))
    assert "first-secret" not in output
    assert "second-secret" not in output
    assert len(mod._check_divergence(results)) == 1


def test_venv_python_symlink_keeps_venv_script(installation, tmp_path, monkeypatch):
    base = script(tmp_path / "base" / "python")
    python = installation.parent / "python"
    python.symlink_to(base)
    monkeypatch.setattr(mod.sys, "executable", str(python))
    assert mod._resolve_executable() == str(installation)


@pytest.mark.parametrize("editor,suffix", [("codex", ".toml"), ("continue", ".yaml")])
def test_native_diagnostic_read_and_extract(editor, suffix, tmp_path):
    block = mod._client_block({"command": "/some path/palinode-mcp", "env": {}}, editor)
    path = tmp_path / ("config" + suffix)
    path.write_text(mod._serialize_block(block, editor))
    data, error = mod._read_config(path)
    assert error is None
    assert mod._extract_palinode_entry(data)["command"] == "/some path/palinode-mcp"


def test_nested_claude_local_entry_uses_current_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    entry = {"command": "/installed/bin/palinode-mcp"}
    data = {"projects": {str(tmp_path): {"mcpServers": {"palinode": entry}},
                         "/other": {"mcpServers": {"palinode": {"command": "wrong"}}}}}
    assert mod._extract_palinode_entry(data) == entry


@pytest.mark.parametrize("suffix", [".json", ".toml", ".yaml"])
def test_parse_errors_never_echo_file_contents(tmp_path, suffix):
    path = tmp_path / ("config" + suffix)
    path.write_text('token = ["super-private"\n : invalid')
    _data, error = mod._read_config(path)
    assert error is not None
    assert "super-private" not in error


def test_no_implicit_project_or_shell_copy(installation, monkeypatch):
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setenv("CWD", "/unrelated/project")
    env = mod._build_stdio_entry()["env"]
    assert "PALINODE_PROJECT" not in env
    assert "CWD" not in env
    assert "PATH" not in env


def test_toml_windows_and_unicode_paths_roundtrip():
    entry = {"command": 'C:\\Program Files\\Palinode 🐟\\palinode-mcp.exe',
             "env": {"SAMPLE": 'quotes" tab\t newline\n del\x7f'}}
    output = mod._serialize_block(mod._client_block(entry, "codex"), "codex")
    assert tomllib.loads(output)["mcp_servers"]["palinode"] == entry


def test_local_claude_scope_precedes_user(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local = {"command": "local"}
    data = {"mcpServers": {"palinode": {"command": "user"}},
            "projects": {str(tmp_path): {"mcpServers": {"palinode": local}}}}
    assert mod._extract_palinode_entry(data) == local
