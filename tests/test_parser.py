from palinode.core.parser import parse_markdown, _build_canonical_question_prefix

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


# ── canonical_question tests ──────────────────────────────────────────────

def test_build_canonical_question_prefix_string():
    prefix = _build_canonical_question_prefix({"canonical_question": "What is auth?"})
    assert prefix == "Q: What is auth?\n\n"

def test_build_canonical_question_prefix_list():
    prefix = _build_canonical_question_prefix({
        "canonical_question": ["What is auth?", "How does login work?"]
    })
    assert prefix == "Q: What is auth?\nQ: How does login work?\n\n"

def test_build_canonical_question_prefix_absent():
    assert _build_canonical_question_prefix({}) == ""
    assert _build_canonical_question_prefix({"id": "x"}) == ""

def test_build_canonical_question_prefix_empty_string():
    assert _build_canonical_question_prefix({"canonical_question": ""}) == ""

def test_build_canonical_question_prefix_empty_list():
    assert _build_canonical_question_prefix({"canonical_question": []}) == ""

def test_build_canonical_question_prefix_non_string_type():
    assert _build_canonical_question_prefix({"canonical_question": 42}) == ""


def test_canonical_question_prepended_short_doc():
    content = """---
id: auth-decision
canonical_question: What did we decide about authentication?
---
We chose JWT tokens for stateless auth.
"""
    meta, sections = parse_markdown(content)
    assert len(sections) == 1
    assert sections[0]["content"].startswith("Q: What did we decide about authentication?\n\n")
    assert "JWT tokens" in sections[0]["content"]


def test_canonical_question_list_prepended_short_doc():
    content = """---
id: auth-decision
canonical_question:
  - What did we decide about authentication?
  - How does login work?
---
We chose JWT tokens for stateless auth.
"""
    meta, sections = parse_markdown(content)
    assert sections[0]["content"].startswith("Q: What did we decide about authentication?\n")
    assert "Q: How does login work?" in sections[0]["content"]
    assert "JWT tokens" in sections[0]["content"]


def test_canonical_question_prepended_long_doc():
    """For long docs with sections, canonical_question is prepended to the first chunk only."""
    preamble = "a" * 2000
    content = f"""---
id: test-long
canonical_question: Why is the preamble so long?
---
{preamble}

## Section 1

Content 1

## Section 2

Content 2
"""
    meta, sections = parse_markdown(content)
    # First chunk (root/preamble) should have the prefix
    first = sections[0]
    assert first["content"].startswith("Q: Why is the preamble so long?\n\n")
    # Later sections should NOT have the prefix
    for sec in sections[1:]:
        assert not sec["content"].startswith("Q:")


def test_no_canonical_question_leaves_content_unchanged():
    content = """---
id: plain
---
Just some content.
"""
    meta, sections = parse_markdown(content)
    assert not sections[0]["content"].startswith("Q:")
    assert "Just some content." in sections[0]["content"]
