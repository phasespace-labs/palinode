"""Tests for the local read-only provenance UI (Phase 0).

Covers the three required behaviors:

1. ``GET /ui`` returns 200 and renders the dashboard shell.
2. ``GET /ui/memory/{id}`` renders a seeded fact (body + provenance panel).
3. Markdown rendering sanitizes a ``<script>`` payload (no live XSS node).

Plus: the loopback guard refuses to serve on a non-loopback bind, and the
markdown/provenance helpers (the weir-reusable pieces) are unit-checked.

Real SQLite in tmp_path (no mocked DB — repo rule). The save path is driven
through the API with the embedder + security scanner mocked, so a fact's
chunks are actually indexed (matching test_read_recall_e2e.py).
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config

_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with fresh tmp memory_dir + real SQLite; git + auth off.

    Loopback host is forced so the UI guard does not 403 the test. Reloads the
    server module with no PALINODE_API_TOKEN so bearer auth does not 401 — same
    pattern as test_read_recall_e2e.py / test_update_policy_axis.py.
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.services.api, "host", "127.0.0.1")
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE", "PALINODE_API_HOST"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with TestClient(srv.app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _patch_io():
    """Mock the embedder and security scanner — neither is under test here."""
    return (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR),
    )


def _seed_fact(client, *, slug: str, content: str, **kw) -> str:
    """POST /save a Decision and return its memory-relative path.

    ``/save`` returns ``file_path`` as the absolute on-disk path; relativize it
    against ``config.memory_dir`` so the UI routes (which take memory-relative
    paths) can address it.
    """
    scan_p, embed_p = _patch_io()
    with scan_p, embed_p:
        body = {"content": content, "type": "Decision", "slug": slug}
        body.update(kw)
        res = client.post("/save", json=body)
    assert res.status_code == 200, res.text
    abs_path = res.json()["file_path"]
    return os.path.relpath(abs_path, config.memory_dir)


def _git(*args: str) -> None:
    subprocess.run(
        ["git", "-C", config.memory_dir, *args],
        check=True,
        capture_output=True,
    )


def _seed_two_commit_history() -> str:
    """Create a real saved-and-superseded memory timeline for the UI."""
    _git("init", "-q")
    _git("config", "user.email", "ui-history@example.invalid")
    _git("config", "user.name", "UI history fixture")
    rel = "decisions/storage-choice.md"
    path = Path(config.memory_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: Decision\n---\n\n# Storage choice\n\nUse SQLite.\n",
        encoding="utf-8",
    )
    _git("add", "--", rel)
    _git("commit", "-qm", "save initial SQLite decision")
    path.write_text(
        "---\ntype: Decision\n---\n\n# Storage choice\n\nUse PostgreSQL.\n",
        encoding="utf-8",
    )
    _git("add", "--", rel)
    _git("commit", "-qm", "supersession to PostgreSQL <script>alert(1)</script>")
    return rel


# ── 1. Dashboard ──────────────────────────────────────────────────────────────
def test_dashboard_returns_200_and_renders(client):
    res = client.get("/ui")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    html = res.text
    # Shell markers from the design system.
    assert 'class="brand"' in html
    assert "audit-grade memory" in html
    assert "Memory health" in html
    # CSS is linked from the in-package static mount (offline-first, no CDN).
    assert "/ui/static/palinode.css" in html
    assert "https://cdn" not in html and "unpkg" not in html


def test_dashboard_trailing_slash_also_renders(client):
    res = client.get("/ui/")
    assert res.status_code == 200
    assert "Memory health" in res.text


def test_dashboard_lists_seeded_memory(client):
    _seed_fact(client, slug="auth-tokens", content="# Auth\n\nUse server-side tokens.")
    res = client.get("/ui")
    assert res.status_code == 200
    # The seeded fact appears in the recent list and links to its detail view.
    assert "decisions/auth-tokens.md" in res.text
    assert "/ui/memory/decisions/auth-tokens" in res.text


def test_static_css_served_in_package(client):
    res = client.get("/ui/static/palinode.css")
    assert res.status_code == 200
    assert "--accent:#1f5e5b" in res.text  # the deep-teal token, ported verbatim


# ── 2. Fact detail ──────────────────────────────────────────────────────────
def test_memory_detail_renders_seeded_fact(client):
    path = _seed_fact(
        client,
        slug="session-tokens",
        content=(
            "# Use session tokens\n\n"
            "Stateless `JWT` sessions make revocation hard.\n\n"
            "> Supersedes the earlier JWT decision.\n"
        ),
        title="Use server-side session tokens, not JWTs",
    )
    res = client.get(f"/ui/memory/{path.removesuffix('.md')}")
    assert res.status_code == 200
    html = res.text
    # Title + rendered body.
    assert "Use server-side session tokens, not JWTs" in html
    assert "<code>JWT</code>" in html  # markdown rendered, inline code styled
    assert "make revocation hard" in html
    # Provenance panel present with the verified seal and attestation gap tags.
    assert "Provenance" in html
    assert "chain intact" in html
    for gap in ("G1", "G2", "R2", "R1", "G4", "G3"):
        assert gap in html, f"missing attestation gap tag {gap}"
    # Real lineage: a Saved row backed by a git commit (auto_commit off here,
    # so it shows the no-history placeholder — still a real, honest row).
    assert "Saved" in html


def test_memory_detail_hides_the_auto_footer_marker_and_keeps_its_links(client):
    """The footer's HTML-comment marker is not shown as text; the link list is."""
    root = Path(config.memory_dir) / "decisions"
    root.mkdir(parents=True, exist_ok=True)
    shapes = {
        "footer-writer-shape": "## See also\n<!-- palinode-auto-footer -->\n- [[harbor-notes]]\n",
        "footer-blank-line": "## See also\n\n<!-- palinode-auto-footer -->\n- [[harbor-notes]]\n",
    }
    for slug, footer in shapes.items():
        (root / f"{slug}.md").write_text(
            f"---\ntype: Decision\n---\n\n# {slug}\n\nBody.\n\n{footer}", encoding="utf-8"
        )
        page = client.get(f"/ui/memory/decisions/{slug}")
        assert page.status_code == 200
        assert "palinode-auto-footer" not in page.text, slug
        assert "See also" in page.text and "harbor-notes" in page.text, slug


def test_auto_footer_marker_quoted_outside_a_footer_still_renders():
    from palinode.api.ui.render import render_markdown

    quoted = "The footer starts with `<!-- palinode-auto-footer -->` on its own line.\n"
    assert "palinode-auto-footer" in render_markdown(quoted)
    fenced = "```\n<!-- palinode-auto-footer -->\n```\n"
    assert "palinode-auto-footer" in render_markdown(fenced)
    fenced_footer = "```\n## See also\n<!-- palinode-auto-footer -->\n```\n"
    assert "palinode-auto-footer" in render_markdown(fenced_footer)
    tilde = "~~~markdown\n## See also\n\n<!-- palinode-auto-footer -->\n~~~\n"
    assert "palinode-auto-footer" in render_markdown(tilde)
    long_fence = "````\n```\n## See also\n<!-- palinode-auto-footer -->\n````\n"
    assert "palinode-auto-footer" in render_markdown(long_fence)
    indented = "    ## See also\n    <!-- palinode-auto-footer -->\n"
    assert "palinode-auto-footer" in render_markdown(indented)
    quoted_block = "> ## See also\n> <!-- palinode-auto-footer -->\n"
    assert "palinode-auto-footer" in render_markdown(quoted_block)
    in_list = "- ## See also\n\n  <!-- palinode-auto-footer -->\n"
    assert "palinode-auto-footer" in render_markdown(in_list)


def test_a_real_footer_after_a_list_local_fence_is_still_hidden():
    from palinode.api.ui.render import render_markdown

    body = (
        "- item\n\n  ```\n  code\n\nAfter the list.\n\n"
        "## See also\n<!-- palinode-auto-footer -->\n- [[harbor-notes]]\n"
    )
    html = render_markdown(body)
    assert "palinode-auto-footer" not in html and "harbor-notes" in html


def test_memory_detail_404_for_missing(client):
    res = client.get("/ui/memory/decisions/does-not-exist")
    assert res.status_code == 404


def test_memory_detail_supersedes_links_to_target(client):
    path = _seed_fact(
        client,
        slug="new-policy",
        content="# New policy\n\nbody",
        metadata={"supersedes": "decisions/old-policy.md"},
    )
    res = client.get(f"/ui/memory/{path.removesuffix('.md')}")
    assert res.status_code == 200
    assert "/ui/memory/decisions/old-policy.md" in res.text


def test_memory_title_ignores_generated_footer_and_keeps_real_titles(client):
    footer_only = Path(config.memory_dir) / "decisions" / "storage-choice.md"
    footer_only.parent.mkdir(parents=True, exist_ok=True)
    footer_only.write_text(
        "---\ntype: Decision\n---\n\n## See also\n\n"
        "<!-- palinode-auto-footer -->\n- [[palinode]]\n",
        encoding="utf-8",
    )
    fallback = client.get("/ui/memory/decisions/storage-choice")
    assert fallback.status_code == 200
    assert "<h1>storage choice</h1>" in fallback.text
    assert "<h1>See also</h1>" not in fallback.text

    heading = _seed_fact(client, slug="heading-kept", content="# User heading\n\nbody")
    heading_page = client.get(f"/ui/memory/{heading.removesuffix('.md')}")
    assert "<h1>User heading</h1>" in heading_page.text

    titled = _seed_fact(
        client,
        slug="title-kept",
        content="# Different body heading\n\nbody",
        title="Explicit decision title",
    )
    titled_page = client.get(f"/ui/memory/{titled.removesuffix('.md')}")
    assert "<h1>Explicit decision title</h1>" in titled_page.text


def test_history_renders_actual_two_commit_timeline_while_paused(client):
    rel = _seed_two_commit_history()

    paused = client.post("/controls", json={"capture_paused": True, "recall_paused": True})
    assert paused.status_code == 200
    res = client.get(f"/ui/history/{rel.removesuffix('.md')}")
    assert res.status_code == 200
    assert "Commit history" in res.text
    assert "Read-only Git history" in res.text
    assert "save initial SQLite decision" in res.text
    # The real supersession commit is rendered as text, never executable HTML.
    assert "supersession to PostgreSQL" in res.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in res.text
    assert "<script>alert(1)</script>" not in res.text
    assert "/ui/memory/decisions/storage-choice.md" in res.text


def test_history_rejects_traversal_and_symlink_escape(client):
    traversal = client.get("/ui/history/%2e%2e/%2e%2e/etc/passwd")
    assert traversal.status_code in {400, 403, 404}

    outside = Path(config.memory_dir).parent / "outside-memory.md"
    outside.write_text("not a memory", encoding="utf-8")
    link = Path(config.memory_dir) / "decisions" / "escaped.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    escaped = client.get("/ui/history/decisions/escaped")
    assert escaped.status_code == 403

    directory = Path(config.memory_dir) / "decisions" / "not-a-memory"
    directory.mkdir()
    not_a_file = client.get("/ui/history/decisions/not-a-memory")
    assert not_a_file.status_code == 404


def test_history_distinguishes_git_failure_from_no_commits(client, monkeypatch):
    path = _seed_fact(client, slug="history-unavailable", content="# Memory\n\nbody")
    with patch("palinode.api.ui.router.git_tools.history", side_effect=OSError("git unavailable")):
        unavailable = client.get(f"/ui/history/{path.removesuffix('.md')}")
    assert unavailable.status_code == 200
    assert "Git history is currently unavailable" in unavailable.text
    assert "No committed history" not in unavailable.text


# ── 3. Markdown sanitization (the load-bearing security property) ─────────────
def test_memory_detail_strips_script_payload(client):
    """A <script> in the memory body must not survive as a live node."""
    payload = (
        "# Title\n\n"
        "Hello <script>alert('xss')</script> world.\n\n"
        '<img src=x onerror="alert(1)">\n\n'
        "[evil](javascript:alert(1))\n"
    )
    path = _seed_fact(client, slug="xss-attempt", content=payload)
    res = client.get(f"/ui/memory/{path.removesuffix('.md')}")
    assert res.status_code == 200
    # Isolate the rendered body fragment so the assertion only inspects
    # agent-content output, not the (trusted) page chrome.
    body = res.text
    frag = body.split('<div class="body">', 1)[1].split("</article>", 1)[0]

    # No LIVE dangerous nodes survive. Raw HTML in a memory body is escaped to
    # inert text by markdown-it (html=False); nh3 is the backstop. The presence
    # of escaped "&lt;script&gt;" proves neutralization (not silent drop).
    assert "<script" not in frag  # no live script element
    assert "<img" not in frag  # raw <img onerror> escaped, never emitted live
    assert 'href="javascript:' not in frag  # js: link scheme stripped/never linked
    assert "href='javascript:" not in frag
    # The script payload is present but inert (escaped), not executable.
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in frag or \
        "&lt;script&gt;alert('xss')&lt;/script&gt;" in frag
    # The img+onerror payload is likewise escaped, not a live attribute.
    assert "&lt;img" in frag and "onerror" in frag  # escaped text only


# ── Loopback guard ──────────────────────────────────────────────────────────
def test_ui_refuses_non_loopback_bind(tmp_path, monkeypatch):
    """With PALINODE_API_HOST set to a public address, /ui hard-refuses (403)."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setenv("PALINODE_API_HOST", "0.0.0.0")
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    # The startup bind gate would refuse 0.0.0.0 + no token outright;
    # opt out so the app boots and the UI's own loopback guard is what fires.
    monkeypatch.setenv("PALINODE_API_ALLOW_UNAUTH", "1")
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    try:
        with TestClient(srv.app, raise_server_exceptions=True) as c:
            res = c.get("/ui")
            assert res.status_code == 403
            assert "loopback-only" in res.text
            res2 = c.get("/ui/memory/anything")
            assert res2.status_code == 403
    finally:
        srv._rate_counters.clear()


# ── Helper-level unit checks (weir-reusable, store-agnostic) ──────────────────
def test_render_markdown_sanitizes_directly():
    from palinode.api.ui.render import render_markdown, sanitize_html

    out = render_markdown("**bold** and [ok](https://example.com)")
    assert "<strong>bold</strong>" in out
    assert 'href="https://example.com"' in out

    # nh3 backstop on raw HTML (the path markdown-it's html=False doesn't cover
    # if HTML ever reaches the sanitizer some other way).
    cleaned = sanitize_html(
        "<p>ok</p><script>x()</script><img src=x onerror=y()>"
        '<a href="javascript:z()">a</a>'
    )
    assert "<p>ok</p>" in cleaned
    assert "<script" not in cleaned
    assert "onerror" not in cleaned
    assert "<img" not in cleaned
    assert "javascript:" not in cleaned


def test_build_provenance_real_and_gap_rows():
    from palinode.api.ui.provenance import build_provenance

    rows = build_provenance(
        file_path="decisions/x.md",
        frontmatter={"supersedes": "decisions/old.md"},
        history=[{"commit": "abc1234def", "date": "2026-05-09 14:00"}],
        recall_count=3,
        last_recalled="2026-05-10T00:00:00Z",
    )
    by_kicker = {r.kicker: r for r in rows}
    # Real data populated.
    assert by_kicker["Saved"].mono == "abc1234"
    assert by_kicker["Saved"].state == "ok"
    assert by_kicker["Supersedes"].href == "/ui/memory/decisions/old.md"
    assert by_kicker["Recalled in"].state == "ok"
    assert "3×" in by_kicker["Recalled in"].value
    # A real recall count is verified data, NOT a gap — the G3 tag is dropped
    # on this branch (it marks the still-uncaptured session-log detail).
    assert by_kicker["Recalled in"].gap_tag is None
    # The still-uncaptured attestation fields keep their gap tags. G3 is absent
    # here because the recall row is real.
    gap_tags = {r.gap_tag for r in rows if r.gap_tag}
    assert {"G1", "G2", "R2", "R1", "G4"} <= gap_tags
    assert "G3" not in gap_tags


def test_build_provenance_zero_recall_keeps_g3():
    """The sparse/zero-recall branch still carries G3 (it IS a not-yet-captured
    gap there) — finding #4 only drops G3 when the recall count is real."""
    from palinode.api.ui.provenance import build_provenance

    rows = build_provenance(
        file_path="decisions/x.md",
        frontmatter={},
        history=[],
        recall_count=0,
    )
    by_kicker = {r.kicker: r for r in rows}
    assert by_kicker["Recalled in"].state == "gap"
    assert by_kicker["Recalled in"].gap_tag == "G3"


def test_build_provenance_broken_seal_adds_integrity_row():
    from palinode.api.ui.provenance import build_provenance

    rows = build_provenance(
        file_path="decisions/x.md",
        frontmatter={},
        history=[],
        content_hash_mismatch=True,
    )
    # Integrity row is prepended, in the tamper (warn) state.
    assert rows[0].kicker == "Integrity"
    assert rows[0].state == "warn"
    # The verified-state path does NOT add an Integrity row.
    clean = build_provenance(
        file_path="decisions/x.md",
        frontmatter={},
        history=[],
        content_hash_mismatch=False,
    )
    assert all(r.kicker != "Integrity" for r in clean)


def test_host_is_loopback_classification():
    from palinode.api.ui.router import _host_is_loopback

    assert _host_is_loopback("127.0.0.1")
    assert _host_is_loopback("localhost")
    assert _host_is_loopback("::1")
    assert _host_is_loopback("")  # unset host → defaults treated as local
    assert not _host_is_loopback("0.0.0.0")
    assert not _host_is_loopback("203.0.113.10")  # RFC 5737 documentation range


def test_fact_template_broken_seal_flips_to_oxblood(monkeypatch):
    """Render fact.html directly (dormant tamper path) and assert the seal +
    header pill flip to the broken (oxblood) styling, and the panel gains the
    broken-seal class that greys the attestation badges. Proves the tamper
    template path works now, not only at P2."""
    from palinode.api.ui.provenance import build_provenance
    from palinode.api.ui.router import templates

    rows = build_provenance(
        file_path="decisions/x.md", frontmatter={}, history=[]
    )
    env = templates.env
    # The template uses url_for (injected by TemplateResponse via the request);
    # register a stub so a direct env render resolves it.
    monkeypatch.setitem(env.globals, "url_for", lambda name, **kw: f"/{name}")
    tmpl = env.get_template("fact.html")
    base_ctx = dict(
        title="X", slug="x", category="decisions", memory_id="decisions/x",
        kicker="Decision", confidence=None, recall_count=0, extra_chips=[],
        body_html="<p>body</p>", rows=rows, total_memories=1, palinode_dir="~/p",
        api_port=6340, stale_count=0, orphaned_count=0,
    )

    broken = tmpl.render(broken_seal=True, **base_ctx)
    assert 'class="seal broken"' in broken
    assert 'class="pill broken"' in broken
    assert "broken-seal" in broken  # panel class that greys the badges
    assert "seal broken" in broken and "chain intact" not in broken

    verified = tmpl.render(broken_seal=False, **base_ctx)
    assert "chain intact" in verified
    assert 'class="seal broken"' not in verified
    assert 'class="pill broken"' not in verified
