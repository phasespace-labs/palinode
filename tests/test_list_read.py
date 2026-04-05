import pytest
import os
import yaml
from fastapi.testclient import TestClient
from palinode.api.server import app
from palinode.core.config import config

client = TestClient(app)

@pytest.fixture
def mock_memory_dir(tmp_path):
    old_memory_dir = config.memory_dir
    
    config.memory_dir = str(tmp_path)
    
    os.makedirs(os.path.join(tmp_path, "people"))
    os.makedirs(os.path.join(tmp_path, "projects"))
    os.makedirs(os.path.join(tmp_path, "daily"))
    
    with open(os.path.join(tmp_path, "people", "alice.md"), "w") as f:
        f.write("---\nname: Alice\ncategory: people\ncore: true\nsummary: SumA\n---\nAlice content")
        
    with open(os.path.join(tmp_path, "projects", "palinode.md"), "w") as f:
        f.write("---\nname: Palinode\ncategory: projects\ncore: false\nsummary: SumB\n---\nPalinode content")
        
    with open(os.path.join(tmp_path, "daily", "2024-01-01.md"), "w") as f:
        f.write("---\nname: Daily\ncategory: daily\n---\nDaily log")
        
    yield str(tmp_path)
    
    config.memory_dir = old_memory_dir

def test_list_returns_files_with_summaries(mock_memory_dir):
    res = client.get("/list")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    names = [d["name"] for d in data]
    assert "Alice" in names
    assert "Palinode" in names
    assert "Daily" not in names # daily skipped

def test_list_filters_by_category(mock_memory_dir):
    res = client.get("/list?category=people")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["name"] == "Alice"

def test_list_filters_core_only(mock_memory_dir):
    res = client.get("/list?core_only=true")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["name"] == "Alice"

def test_list_skips_daily_archive_inbox(mock_memory_dir):
    res = client.get("/list")
    for item in res.json():
        assert item["category"] not in ["daily", "archive", "inbox", "logs"]

def test_read_returns_file_content(mock_memory_dir):
    res = client.get("/read?file_path=people/alice.md")
    assert res.status_code == 200
    data = res.json()
    assert data["file"] == "people/alice.md"
    assert "Alice content" in data["content"]

def test_read_with_meta_parses_frontmatter(mock_memory_dir):
    res = client.get("/read?file_path=people/alice.md&meta=true")
    assert res.status_code == 200
    data = res.json()
    assert "frontmatter" in data
    assert data["frontmatter"]["name"] == "Alice"
    assert data["frontmatter"]["core"] is True

def test_read_rejects_path_traversal(mock_memory_dir):
    res = client.get("/read?file_path=../../etc/passwd")
    assert res.status_code == 403

def test_read_appends_md_extension(mock_memory_dir):
    res = client.get("/read?file_path=people/alice")
    assert res.status_code == 200
    assert res.json()["file"] == "people/alice.md"

def test_read_nonexistent_returns_error(mock_memory_dir):
    res = client.get("/read?file_path=people/bob.md")
    assert res.status_code == 404
