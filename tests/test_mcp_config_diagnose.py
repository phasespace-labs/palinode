"""Tests for `palinode mcp-config --diagnose`.

Uses monkeypatching to redirect Path.home() so no real user configs are
touched or read. All fixture files live under tmp_path.
"""
from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli.mcp_config import (
    _candidate_paths,
    _check_divergence,
    _extract_palinode_entry,
    _read_config,
    _render_entry,
    ConfigResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STDIO_ENTRY = {"command": "palinode-mcp", "env": {}}
HTTP_ENTRY = {"url": "http://my-server:6341/mcp/"}
SSH_ENTRY = {
    "command": "ssh",
    "args": ["-o", "BatchMode=yes", "user@host", "palinode-mcp"],
}

PALINODE_BLOCK = {"mcpServers": {"palinode": STDIO_ENTRY}}
HTTP_BLOCK = {"mcpServers": {"palinode": HTTP_ENTRY}}
NO_PALINODE_BLOCK = {"mcpServers": {"other-server": {"command": "other"}}}

# Zed uses context_servers instead of mcpServers
ZED_PALINODE_BLOCK = {"context_servers": {"palinode": STDIO_ENTRY}}
ZED_NO_PALINODE_BLOCK = {"context_servers": {"other-server": {"command": "other"}}}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestReadConfig:
    def test_reads_valid_json(self, tmp_path):
        f = tmp_path / "cfg.json"
        f.write_text('{"mcpServers": {}}')
        data, err = _read_config(f)
        assert err is None
        assert data == {"mcpServers": {}}

    def test_reports_parse_error(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{ broken json ]]]")
        data, err = _read_config(f)
        assert data is None
        assert "JSON parse error" in err

    def test_reports_wrong_top_level_type(self, tmp_path):
        f = tmp_path / "array.json"
        f.write_text('[1, 2, 3]')
        data, err = _read_config(f)
        assert data is None
        assert "unexpected top-level type" in err


class TestExtractPalinodeEntry:
    def test_returns_entry_when_present(self):
        data = {"mcpServers": {"palinode": STDIO_ENTRY}}
        assert _extract_palinode_entry(data) == STDIO_ENTRY

    def test_returns_none_when_absent(self):
        data = {"mcpServers": {"other": {}}}
        assert _extract_palinode_entry(data) is None

    def test_returns_none_when_no_mcp_servers(self):
        assert _extract_palinode_entry({}) is None

    def test_handles_non_dict_mcp_servers(self):
        assert _extract_palinode_entry({"mcpServers": "bad"}) is None

    # Zed shape — context_servers
    def test_returns_entry_from_context_servers(self):
        """Zed uses context_servers instead of mcpServers."""
        data = {"context_servers": {"palinode": STDIO_ENTRY}}
        assert _extract_palinode_entry(data) == STDIO_ENTRY

    def test_returns_none_when_context_servers_has_no_palinode(self):
        data = {"context_servers": {"other-server": {"command": "other"}}}
        assert _extract_palinode_entry(data) is None

    def test_handles_non_dict_context_servers(self):
        assert _extract_palinode_entry({"context_servers": "bad"}) is None

    def test_prefers_mcp_servers_when_both_keys_present(self):
        """If both keys exist and both have palinode, mcpServers wins (checked first)."""
        http_via_mcp = HTTP_ENTRY
        stdio_via_ctx = STDIO_ENTRY
        data = {
            "mcpServers": {"palinode": http_via_mcp},
            "context_servers": {"palinode": stdio_via_ctx},
        }
        assert _extract_palinode_entry(data) == http_via_mcp


class TestRenderEntry:
    def test_renders_http(self):
        r = _render_entry(HTTP_ENTRY)
        assert r.startswith("HTTP")
        assert "http://my-server:6341/mcp/" in r

    def test_renders_stdio(self):
        r = _render_entry(STDIO_ENTRY)
        assert r.startswith("stdio")
        assert "palinode-mcp" in r

    def test_renders_none(self):
        assert _render_entry(None) == "(no palinode entry)"

    def test_renders_stdio_with_args(self):
        r = _render_entry(SSH_ENTRY)
        assert "ssh" in r


class TestCheckDivergence:
    def _make_result(self, path_str, entry):
        entry_json = json.dumps(entry, sort_keys=True, indent=2) if entry else None
        return ConfigResult(
            label="test",
            path=Path(path_str),
            present=True,
            entry=entry,
            entry_json=entry_json,
            error=None,
        )

    def test_no_divergence_when_single_entry(self):
        results = [self._make_result("/a.json", STDIO_ENTRY)]
        assert _check_divergence(results) == []

    def test_no_divergence_when_same_entry(self):
        results = [
            self._make_result("/a.json", STDIO_ENTRY),
            self._make_result("/b.json", STDIO_ENTRY),
        ]
        assert _check_divergence(results) == []

    def test_divergence_detected_on_different_entries(self):
        results = [
            self._make_result("/a.json", STDIO_ENTRY),
            self._make_result("/b.json", HTTP_ENTRY),
        ]
        divergences = _check_divergence(results)
        assert len(divergences) == 1
        a, b, diff = divergences[0]
        assert "palinode-mcp" in diff or "http" in diff

    def test_skips_absent_files(self):
        present = self._make_result("/a.json", STDIO_ENTRY)
        absent = ConfigResult(
            label="x", path=Path("/missing.json"),
            present=False, entry=None, entry_json=None, error=None,
        )
        assert _check_divergence([present, absent]) == []

    def test_skips_error_files(self):
        present = self._make_result("/a.json", STDIO_ENTRY)
        errored = ConfigResult(
            label="x", path=Path("/bad.json"),
            present=True, entry=None, entry_json=None, error="parse error",
        )
        assert _check_divergence([present, errored]) == []


# ---------------------------------------------------------------------------
# Integration tests via CLI runner
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to a temp directory so no real configs are read."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Also patch platform.system to get deterministic path lists
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    return tmp_path


def _macos_paths(home: Path) -> dict[str, Path]:
    """Return the canonical paths for a fake macOS home."""
    return {
        "claude_json": home / ".claude.json",
        "desktop": home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        "desktop_3p": home / "Library" / "Application Support" / "Claude-3p" / "claude_desktop_config.json",
        "integration": home / ".claude" / "claude_desktop_config.json",
        "cline": (
            home / "Library" / "Application Support" / "Code" / "User"
            / "globalStorage" / "saoudrizwan.claude-dev" / "settings"
            / "cline_mcp_settings.json"
        ),
        "zed": home / ".config" / "zed" / "settings.json",
        "zed_fallback": home / "Library" / "Application Support" / "Zed" / "settings.json",
    }


class TestDiagnoseCommand:
    def test_no_configs_present(self, fake_home):
        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "No MCP configs found" in result.output

    def test_single_config_reports_cleanly(self, fake_home):
        paths = _macos_paths(fake_home)
        _write(paths["claude_json"], {"mcpServers": {"palinode": STDIO_ENTRY}})

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "palinode-mcp" in result.output
        assert "WARNING" not in result.output

    def test_multiple_consistent_configs(self, fake_home):
        paths = _macos_paths(fake_home)
        _write(paths["claude_json"], PALINODE_BLOCK)
        _write(paths["desktop"], PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "consistent" in result.output
        assert "WARNING" not in result.output

    def test_divergent_configs_prints_warning(self, fake_home):
        paths = _macos_paths(fake_home)
        _write(paths["claude_json"], PALINODE_BLOCK)
        _write(paths["desktop"], HTTP_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output

    def test_malformed_json_reported_not_crashed(self, fake_home):
        paths = _macos_paths(fake_home)
        paths["claude_json"].parent.mkdir(parents=True, exist_ok=True)
        paths["claude_json"].write_text("{ this is : not : json }")

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "ERROR" in result.output or "error" in result.output.lower()

    def test_file_with_no_palinode_entry(self, fake_home):
        paths = _macos_paths(fake_home)
        _write(paths["claude_json"], NO_PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "no 'palinode' entry" in result.output

    def test_json_output_single_config(self, fake_home):
        paths = _macos_paths(fake_home)
        _write(paths["desktop"], PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "configs" in payload
        assert isinstance(payload["configs"], list)
        assert payload["diverged"] is False

    def test_json_output_divergent_exits_nonzero(self, fake_home):
        paths = _macos_paths(fake_home)
        _write(paths["claude_json"], PALINODE_BLOCK)
        _write(paths["desktop"], HTTP_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config", "--json"])
        payload = json.loads(result.output)
        assert payload["diverged"] is True
        assert len(payload["divergences"]) >= 1
        assert result.exit_code == 1

    def test_json_output_is_valid_json_when_no_configs(self, fake_home):
        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "configs" in payload
        assert payload["diverged"] is False

    def test_closing_recommendation_mentions_both_files(self, fake_home):
        paths = _macos_paths(fake_home)
        _write(paths["desktop"], PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        # macOS recommendation should mention both canonical clients — check for
        # substrings that are short enough not to be split by Rich's line-wrapping.
        assert "Claude Desktop" in result.output
        assert "claude_desktop_config.json" in result.output
        assert ".claude.json" in result.output


# ---------------------------------------------------------------------------
# Cline globalStorage coverage
# ---------------------------------------------------------------------------


class TestCliineGlobalStorage:
    def test_cline_file_present_with_palinode_entry(self, fake_home):
        """Cline config with palinode entry is detected and reported."""
        paths = _macos_paths(fake_home)
        _write(paths["cline"], PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        # Rich may line-wrap the long path; check for the stable short stem
        assert "saoudrizwan.claude-dev" in result.output
        assert "palinode-mcp" in result.output

    def test_cline_file_absent(self, fake_home):
        """Missing Cline config is listed as not present without error."""
        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        # Rich may line-wrap the long path; check for the stable short stem
        assert "saoudrizwan.claude-dev" in result.output
        # File not present — no crash, no palinode entry
        assert "ERROR" not in result.output

    def test_cline_file_present_no_palinode_entry(self, fake_home):
        """Cline config without a palinode entry reports 'no palinode entry'."""
        paths = _macos_paths(fake_home)
        _write(paths["cline"], {"mcpServers": {"other-server": {"command": "other"}}})

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "no 'palinode' entry" in result.output

    def test_cline_json_output_includes_entry(self, fake_home):
        """--json output lists the Cline config with its palinode entry."""
        paths = _macos_paths(fake_home)
        _write(paths["cline"], PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        cline_entries = [
            c for c in payload["configs"]
            if "cline_mcp_settings.json" in c["path"]
        ]
        assert len(cline_entries) == 1
        assert cline_entries[0]["palinode_entry"] == PALINODE_BLOCK["mcpServers"]["palinode"]


# ---------------------------------------------------------------------------
# Zed context_servers coverage
# ---------------------------------------------------------------------------


class TestZedContextServers:
    def test_zed_file_present_with_context_servers_entry(self, fake_home):
        """Zed config with palinode under context_servers is detected."""
        paths = _macos_paths(fake_home)
        _write(paths["zed"], ZED_PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "zed" in result.output.lower()
        assert "palinode-mcp" in result.output

    def test_zed_file_absent(self, fake_home):
        """Missing Zed config is listed without error."""
        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "zed" in result.output.lower()
        assert "ERROR" not in result.output

    def test_zed_file_present_no_palinode_entry(self, fake_home):
        """Zed config without palinode reports 'no palinode entry'."""
        paths = _macos_paths(fake_home)
        _write(paths["zed"], ZED_NO_PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "no 'palinode' entry" in result.output

    def test_zed_macos_fallback_path_scanned(self, fake_home):
        """On macOS the ~/Library/Application Support/Zed/settings.json fallback is also walked."""
        paths = _macos_paths(fake_home)
        _write(paths["zed_fallback"], ZED_PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        zed_paths = [c["path"] for c in payload["configs"] if "Zed" in c["path"]]
        assert len(zed_paths) >= 1

    def test_zed_json_output_includes_entry(self, fake_home):
        """--json output lists the Zed config with its palinode entry."""
        paths = _macos_paths(fake_home)
        _write(paths["zed"], ZED_PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        zed_entries = [
            c for c in payload["configs"]
            if "zed" in c["path"].lower() and c.get("palinode_entry") is not None
        ]
        assert len(zed_entries) >= 1
        assert zed_entries[0]["palinode_entry"] == STDIO_ENTRY


# ---------------------------------------------------------------------------
# Multi-client scenario
# ---------------------------------------------------------------------------


class TestMultiClientScenario:
    def test_three_clients_all_reported(self, fake_home):
        """Claude Desktop, Cline, and Zed entries are all found and reported."""
        paths = _macos_paths(fake_home)
        _write(paths["desktop"], PALINODE_BLOCK)
        _write(paths["cline"], PALINODE_BLOCK)
        _write(paths["zed"], ZED_PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        entries_with_palinode = [
            c for c in payload["configs"] if c.get("palinode_entry") is not None
        ]
        assert len(entries_with_palinode) == 3

    def test_consistent_across_three_clients_no_divergence(self, fake_home):
        """Same entry in 3 places — no divergence warning."""
        paths = _macos_paths(fake_home)
        _write(paths["desktop"], PALINODE_BLOCK)
        _write(paths["cline"], PALINODE_BLOCK)
        _write(paths["zed"], ZED_PALINODE_BLOCK)

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "WARNING" not in result.output
        assert "consistent" in result.output

    def test_divergence_detected_across_clients(self, fake_home):
        """Different entries across clients triggers divergence warning."""
        paths = _macos_paths(fake_home)
        _write(paths["desktop"], PALINODE_BLOCK)    # stdio
        _write(paths["cline"], HTTP_BLOCK)          # http — different

        runner = CliRunner()
        result = runner.invoke(main, ["mcp-config"])
        assert result.exit_code == 0, result.output
        assert "WARNING" in result.output


# ---------------------------------------------------------------------------
# Test that init.py MCP_JSON_BLOCK includes the _warning field
# ---------------------------------------------------------------------------


class TestInitMcpJsonBlock:
    def test_mcp_json_block_has_warning_field(self):
        from palinode.cli.init import MCP_JSON_BLOCK
        assert "_warning" in MCP_JSON_BLOCK
        assert "mcp-config" in MCP_JSON_BLOCK["_warning"]

    def test_mcp_json_block_is_valid_json_serializable(self):
        from palinode.cli.init import MCP_JSON_BLOCK
        dumped = json.dumps(MCP_JSON_BLOCK)
        reloaded = json.loads(dumped)
        assert reloaded["mcpServers"]["palinode"]["command"] == "palinode-mcp"

    def test_init_writes_warning_to_mcp_json(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        mcp_json = tmp_path / ".mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text())
        assert "_warning" in data
        assert "mcp-config" in data["_warning"]
