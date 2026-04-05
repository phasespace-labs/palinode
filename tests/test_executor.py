import os
import tempfile
import pytest
from palinode.consolidation.executor import apply_operations

@pytest.fixture
def temp_memory_file():
    content = """---
id: project-alpha
category: project
---

# Project Alpha

- [2024-01-01] The project started today <!-- fact:f1 -->
- [2024-01-02] An update occurred <!-- fact:f2 -->
- [2024-01-03] Another update <!-- fact:f3 -->
"""
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    yield path
    os.remove(path)

def test_keep_operation(temp_memory_file):
    ops = [{"op": "KEEP", "id": "f1"}]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["kept"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "The project started today <!-- fact:f1 -->" in content

def test_update_operation(temp_memory_file):
    ops = [{"op": "UPDATE", "id": "f2", "new_text": "- [2024-01-02] A significant update occurred"}]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["updated"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "A significant update occurred <!-- fact:f2 -->" in content
    assert "An update occurred" not in content

def test_merge_operation(temp_memory_file):
    ops = [{"op": "MERGE", "ids": ["f2", "f3"], "new_text": "- [2024-01-02] Important combined updates"}]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["merged"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "Important combined updates <!-- fact:merged-f2 -->" in content
    assert "<!-- fact:f3 -->" not in content

def test_supersede_operation(temp_memory_file):
    ops = [{"op": "SUPERSEDE", "id": "f1", "new_text": "- [2024-01-04] The project was restarted", "reason": "Change of plans"}]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["superseded"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "~~[2024-01-01] The project started today~~" in content
    assert "The project was restarted <!-- fact:supersedes-f1 -->" in content
    
    # Check history file
    history_file = temp_memory_file.replace(".md", "-history.md")
    assert os.path.exists(history_file)
    with open(history_file) as f:
        hist = f.read()
    assert "Superseded" in hist
    os.remove(history_file)

def test_archive_operation(temp_memory_file):
    ops = [{"op": "ARCHIVE", "id": "f2", "reason": "No longer relevant"}]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["archived"] == 1
    with open(temp_memory_file) as f:
        content = f.read()
    assert "An update occurred" not in content
    
    history_file = temp_memory_file.replace(".md", "-history.md")
    assert os.path.exists(history_file)
    with open(history_file) as f:
        hist = f.read()
    assert "Archived" in hist
    os.remove(history_file)

def test_malformed_operations(temp_memory_file):
    # Should skip malformed items without crashing
    ops = [{"op": "KEEP", "id": "f1"}, ["nested", "list"], "string item", {"op": "UPDATE", "id": "f2", "new_text": "- New text"}]
    stats = apply_operations(temp_memory_file, ops)
    assert stats["kept"] == 1
    assert stats["updated"] == 1
