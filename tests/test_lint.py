from datetime import datetime, timedelta, timezone
from palinode.core.lint import run_lint_pass
from palinode.core.config import config
import os

def test_lint_pass(tmp_path, monkeypatch):
    """Test the lint logic with simulated memory files."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    
    people_dir = tmp_path / "people"
    people_dir.mkdir(parents=True)
    
    # 1. Missing Fields (No id, type, category)
    missing1 = people_dir / "missing1.md"
    missing1.write_text("---\nstatus: active\n---\nBody", encoding="utf-8")
    
    # 2. Stale Files (Active and older than 90 days)
    stale1 = people_dir / "stale1.md"
    old_date = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale1.write_text(f"---\nid: people-stale1\ncategory: people\ntype: Person\nstatus: active\ncreated_at: {old_date}\n---\nBody", encoding="utf-8")
    
    # 3. Healthy file referencing 'people/stale1' (meaning stale1 is not an orphan)
    healthy1 = people_dir / "healthy1.md"
    healthy1.write_text(f"---\nid: people-healthy1\ncategory: people\ntype: Person\nstatus: active\ncreated_at: {(datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')}\nentities:\n  - people/stale1\n---\nBody", encoding="utf-8")
    
    # 4. Orphaned file (No entities, and nobody references it)
    orphan1 = people_dir / "orphan1.md"
    orphan1.write_text("---\nid: people-orphan1\ncategory: people\ntype: Person\nstatus: active\n---\nBody", encoding="utf-8")
    
    # 5. Contradiction file 1
    contra1 = people_dir / "contra.md"
    contra1.write_text("---\nid: people-contra\ncategory: people\ntype: Person\nstatus: active\n---\nBody", encoding="utf-8")
    
    # 6. Contradiction file 2 (duplicate entity slug, both active)
    contra2 = tmp_path / "insights"
    contra2.mkdir(exist_ok=True)
    contra2_file = contra2 / "contra.md"
    # Actually wait, our contradiction logic creates entity ref like f"{cat}/{slug}".
    # Let's put it in the same category!
    contra_dup = people_dir / "contra-status.md"
    contra_dup.write_text("---\nid: people-contra-dup\ncategory: people\ntype: Person\nstatus: active\n---\nBody", encoding="utf-8")

    result = run_lint_pass()

    # Validate missing fields
    assert any(mf["file"].endswith("missing1.md") and "id" in mf["missing"] for mf in result["missing_fields"])

    # Validate stale
    assert any(sf["file"].endswith("stale1.md") for sf in result["stale_files"])

    # Validate orphan
    assert any(of.endswith("orphan1.md") for of in result["orphaned_files"])
    assert not any(of.endswith("stale1.md") for of in result["orphaned_files"])  # referenced!

    # Validate contradictions
    assert any(ct["entity"] == "people/contra" for ct in result["contradictions"])

    # M0: Validate new keys exist
    assert "missing_entities" in result
    assert "missing_descriptions" in result
    assert "core_count" in result


def test_lint_missing_entities(tmp_path, monkeypatch):
    """Files without entities should be flagged."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    people_dir = tmp_path / "people"
    people_dir.mkdir()

    # File with no entities
    (people_dir / "lonely.md").write_text(
        "---\nid: people-lonely\ncategory: people\ntype: Person\n---\nBody"
    )
    # File with entities
    (people_dir / "linked.md").write_text(
        "---\nid: people-linked\ncategory: people\ntype: Person\nentities:\n  - project/foo\n---\nBody"
    )

    result = run_lint_pass()
    assert any("lonely.md" in f for f in result["missing_entities"])
    assert not any("linked.md" in f for f in result["missing_entities"])


def test_lint_missing_descriptions(tmp_path, monkeypatch):
    """Files without description should be flagged."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    insights_dir = tmp_path / "insights"
    insights_dir.mkdir()

    # No description
    (insights_dir / "nodesc.md").write_text(
        "---\nid: insights-nodesc\ncategory: insights\ntype: Insight\n---\nBody"
    )
    # Has description
    (insights_dir / "hasdesc.md").write_text(
        "---\nid: insights-hasdesc\ncategory: insights\ntype: Insight\ndescription: A useful insight\n---\nBody"
    )

    result = run_lint_pass()
    assert any("nodesc.md" in f for f in result["missing_descriptions"])
    assert not any("hasdesc.md" in f for f in result["missing_descriptions"])


def test_lint_core_count(tmp_path, monkeypatch):
    """Core count should tally files with core: true."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Core file
    (projects_dir / "important.md").write_text(
        "---\nid: projects-important\ncategory: projects\ntype: ProjectSnapshot\ncore: true\n---\nBody"
    )
    # Non-core file
    (projects_dir / "normal.md").write_text(
        "---\nid: projects-normal\ncategory: projects\ntype: ProjectSnapshot\n---\nBody"
    )

    result = run_lint_pass()
    assert result["core_count"] == 1
