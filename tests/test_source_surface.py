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
            "source": "antigravity"
        })
        assert res.status_code == 200
        file_path = res.json()["file_path"]
        
        with open(file_path, "r") as f:
            content = f.read()
        assert "source: antigravity" in content

def test_save_defaults_source_to_mcp(mock_memory_dir):
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
        
    from palinode.mcp import _save_memory
    file_path = _save_memory("Test MCP", "Insight", None, [])
    with open(file_path, "r") as f:
        content = f.read()
    assert "source: mcp" in content

def test_save_defaults_source_to_cli(mock_memory_dir):
    runner = CliRunner()
    
    with patch("palinode.cli._api.api_client.save") as mock_save:
        mock_save.return_value = {"file": "test", "id": "test"}
        result = runner.invoke(save, ["test", "--type", "Insight"])
        
        assert result.exit_code == 0
        mock_save.assert_called_once()
        assert mock_save.call_args[1]["source"] == "cli"

def test_session_end_includes_source(mock_memory_dir):
    runner = CliRunner()
    
    with patch("subprocess.run"):
        result = runner.invoke(session_end, ["Tested something", "--source", "claude-code", "--project", "palinode"])
        assert result.exit_code == 0
        
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_file = os.path.join(mock_memory_dir, "daily", f"{today}.md")
    
    with open(daily_file, "r") as f:
        content = f.read()
        
    assert "**Source:** claude-code" in content
