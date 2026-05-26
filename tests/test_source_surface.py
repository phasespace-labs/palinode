import pytest
import os
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient
from palinode.api.server import app
from palinode.cli.save import save
from palinode.cli.session_end import session_end
from palinode.core.config import config
from unittest.mock import patch

client = TestClient(app)

@pytest.fixture
def mock_memory_dir(tmp_path):
    old_memory_dir = config.memory_dir
    
    config.memory_dir = str(tmp_path)
    
    yield str(tmp_path)
    
    config.memory_dir = old_memory_dir

def test_save_includes_source_in_frontmatter(mock_memory_dir):
    # Patch scan_memory_content to always return (True, "OK") to bypass Qwen api call
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post("/save", json={
            "content": "Test memory",
            "type": "Insight",
            "source": "cursor"
        })
        assert res.status_code == 200
        file_path = res.json()["file_path"]
        
        with open(file_path, "r") as f:
            content = f.read()
        assert "source: cursor" in content

def test_save_defaults_source_to_api(mock_memory_dir):
    """API saves without explicit source should default to 'api'."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post("/save", json={
            "content": "Test memory",
            "type": "Insight"
        })
        assert res.status_code == 200
        file_path = res.json()["file_path"]

        with open(file_path, "r") as f:
            content = f.read()
        assert "source: api" in content

def test_save_with_mcp_source(mock_memory_dir):
    """MCP saves pass source='mcp' through the API."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post("/save", json={
            "content": "Test MCP memory",
            "type": "Insight",
            "source": "mcp"
        })
        assert res.status_code == 200
        file_path = res.json()["file_path"]

        with open(file_path, "r") as f:
            content = f.read()
        assert "source: mcp" in content

def test_save_defaults_source_to_cli(mock_memory_dir):
    """ADR-010 / #167: when --source is not passed, the body field is None
    (the X-Palinode-Source header carries attribution; the API resolves it).
    """
    runner = CliRunner()

    # Patch at the call site (save.py's module-level reference), not at the
    # definition site (_api.py).  test_session_end_timeout.py deletes
    # palinode.cli._api from sys.modules and reimports it, which creates a
    # fresh api_client object.  save.py still holds the pre-reload reference,
    # so patching _api.api_client after the reload misses the call entirely.
    with patch("palinode.cli.save.api_client.save") as mock_save:
        mock_save.return_value = {"file": "test", "id": "test"}
        result = runner.invoke(save, ["test", "--type", "Insight"])

        assert result.exit_code == 0
        mock_save.assert_called_once()
        # No --source passed → body source field is None.  The CLI's httpx
        # Client carries `X-Palinode-Source: cli` so the API sees the
        # surface attribution via header.
        assert mock_save.call_args[1]["source"] is None


def test_cli_client_sends_source_header():
    """ADR-010 / #167: the CLI httpx Client must carry X-Palinode-Source: cli
    so saves without explicit body `source` are still attributed correctly.
    """
    from palinode.cli._api import api_client
    from palinode.core.defaults import SAVE_SOURCE_HEADER

    assert api_client.client.headers.get(SAVE_SOURCE_HEADER) == "cli"


def test_save_uses_header_when_body_source_absent(mock_memory_dir):
    """API resolves source via the X-Palinode-Source header when the body
    doesn't set one (ADR-010 / #167)."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post(
            "/save",
            json={"content": "hdr test", "type": "Insight"},
            headers={"X-Palinode-Source": "cursor"},
        )
        assert res.status_code == 200
        with open(res.json()["file_path"], "r") as f:
            content = f.read()
        assert "source: cursor" in content


def test_save_body_source_wins_over_header(mock_memory_dir):
    """Explicit body `source` beats the header (ADR-010 / #167 precedence)."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post(
            "/save",
            json={"content": "win test", "type": "Insight", "source": "explicit"},
            headers={"X-Palinode-Source": "cursor"},
        )
        assert res.status_code == 200
        with open(res.json()["file_path"], "r") as f:
            content = f.read()
        assert "source: explicit" in content

def test_session_end_includes_source(mock_memory_dir):
    """ADR-010 / #170: CLI now goes through the API. Patch
    ``api_client.session_end`` to invoke the in-process API handler so
    the daily-file end-to-end assertion still holds."""
    from palinode.api.server import session_end_api, SessionEndRequest

    runner = CliRunner()

    def _fake_session_end(**kwargs):
        # Drop None values so the API's pydantic defaults take effect.
        clean = {k: v for k, v in kwargs.items() if v is not None}
        # `decisions`/`blockers` come through as [] when no flags passed;
        # the in-process handler is happy with empty lists.
        return session_end_api(SessionEndRequest(**clean))

    # Patch at the call site (session_end.py's module-level reference), not
    # at _api.py.  test_session_end_timeout.py evicts palinode.cli._api from
    # sys.modules and reimports it; session_end.py's own api_client binding
    # therefore diverges from the freshly-created one in _api — patching
    # _api.api_client.session_end no longer intercepts the real call.
    with patch("palinode.cli.session_end.api_client.session_end", side_effect=_fake_session_end), \
         patch("subprocess.run"), \
         patch("palinode.api.server._generate_description", return_value="t"):
        result = runner.invoke(session_end, ["Tested something", "--source", "claude-code", "--project", "palinode"])
        assert result.exit_code == 0, result.output

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_file = os.path.join(mock_memory_dir, "daily", f"{today}.md")

    with open(daily_file, "r") as f:
        content = f.read()

    assert "**Source:** claude-code" in content
