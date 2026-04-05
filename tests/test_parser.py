from palinode.core.parser import parse_markdown

def test_parse_frontmatter():
    content = """---
id: test-1
category: person
---
# Header
Some content
"""
    meta, sections = parse_markdown(content)
    assert meta["id"] == "test-1"
    assert meta["category"] == "person"
    assert len(sections) == 1
    assert "Some content" in sections[0]["content"]

def test_parse_malformed_frontmatter():
    content = """---
id: test-1
category: person
malformed: [
---
Body text
"""
    meta, sections = parse_markdown(content)
    # Should not crash, meta may be empty depending on python-frontmatter parsing
    assert len(sections) == 1
    assert "Body text" in sections[0]["content"]

def test_parse_fact_id_extraction():
    content = """---
id: test-1
---
- A fact here <!-- fact:abc-123 -->
"""
    meta, sections = parse_markdown(content)
    assert "abc-123" in sections[0]["content"]

def test_parse_splits_sections_if_long():
    # Make it long enough (>2000 chars) to trigger splitting
    content = "---\nid: test-1\n---\n" + ("a" * 2000) + "\n\n## Section 1\n\nContent 1\n\n## Section 2\n\nContent 2"
    meta, sections = parse_markdown(content)
    assert len(sections) >= 2
    assert any(s["section_id"] == "section-1" for s in sections)
    assert any(s["section_id"] == "section-2" for s in sections)
