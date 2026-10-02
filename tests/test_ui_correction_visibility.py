"""The inspector's visibility rule, and the read-only correction section.

The rule, decided for the inspector and implemented here:

1. a direct ``/ui/memory/<file>`` or ``/ui/history/<file>`` read of a record
   the listing hides **stays allowed** — the inspector is loopback-only and its
   user is the local operator, who already owns the files;
2. both pages **label** that record's visibility, so it is never mistaken for a
   default-visible one;
3. **no mutation is offered or performed** from the inspector for such a
   record: the correction section is replaced by a refusal pointing at the CLI
   and API, where the caller's authority is explicit.

``ui_memory`` and ``ui_history`` follow it identically; each has a
``restricted`` fixture below.

Plus the properties the rest of the inspector already holds: the page is
read-only (no form posts anything), and hostile content in a memory is escaped
rather than rendered.
"""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.services.api, "host", "127.0.0.1")
    for key in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE", "PALINODE_API_HOST"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "decisions").mkdir(parents=True, exist_ok=True)

    server = importlib.reload(importlib.import_module("palinode.api.server"))
    server._rate_counters.clear()
    with TestClient(server.app, raise_server_exceptions=True) as test_client:
        yield test_client
    server._rate_counters.clear()


def _memory(rel: str, frontmatter: str, body: str) -> str:
    path = Path(config.memory_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return rel


def _visible(rel: str = "decisions/visible.md") -> str:
    return _memory(
        rel,
        "id: decisions-visible\ntype: Decision\ncategory: decisions\n"
        "title: Visible decision\nstatus: active",
        "Use the local file store.",
    )


def _restricted(rel: str = "decisions/restricted.md") -> str:
    return _memory(
        rel,
        "id: decisions-restricted\ntype: Decision\ncategory: decisions\n"
        "title: Restricted decision\nstatus: active\nvisibility: restricted\n"
        "access:\n- person/alice",
        "The restricted storage decision.",
    )


def _private(rel: str = "decisions/private.md") -> str:
    return _memory(
        rel,
        "id: decisions-private\ntype: Decision\ncategory: decisions\n"
        "title: Private decision\nstatus: active\nvisibility: private",
        "The private storage decision.",
    )


# ── discovery still hides them ──────────────────────────────────────────────
def test_discovery_hides_what_the_listing_hides(client) -> None:
    _visible()
    _restricted()
    _private()

    listing = client.get("/ui/memory")
    assert listing.status_code == 200
    assert "decisions/visible.md" in listing.text
    assert "decisions/restricted.md" not in listing.text
    assert "decisions/private.md" not in listing.text


# ── rule 1 + 2: a direct read is served, and labelled ───────────────────────
@pytest.mark.parametrize("seed,rel", [(_restricted, "decisions/restricted"),
                                      (_private, "decisions/private")])
def test_ui_memory_serves_a_hidden_record_and_labels_it(client, seed, rel) -> None:
    seed()
    response = client.get(f"/ui/memory/{rel}")
    assert response.status_code == 200, "a direct read of a hidden record stays allowed"
    assert "Hidden from discovery" in response.text
    assert "withheld from" in response.text


@pytest.mark.parametrize("seed,rel", [(_restricted, "decisions/restricted"),
                                      (_private, "decisions/private")])
def test_ui_history_follows_the_same_rule_identically(client, seed, rel) -> None:
    seed()
    response = client.get(f"/ui/history/{rel}")
    assert response.status_code == 200
    assert "Hidden from discovery" in response.text
    assert "withheld from" in response.text


def test_a_visible_record_carries_no_hidden_label(client) -> None:
    _visible()
    for path in ("/ui/memory/decisions/visible", "/ui/history/decisions/visible"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Hidden from discovery" not in response.text


# ── rule 3: no mutation is offered for a hidden record ──────────────────────
def test_the_correction_section_is_refused_for_a_hidden_record(client) -> None:
    _restricted()
    response = client.get("/ui/memory/decisions/restricted")
    assert response.status_code == 200
    assert "Correct or retire this" in response.text
    assert "No correction or retirement is offered from the inspector" in response.text
    # The refusal points at the surfaces where authority is explicit — and it
    # offers no ready-to-run apply, only where to go.
    assert "palinode corrections preview --target decisions/restricted.md" in response.text
    assert "corrections apply" not in response.text
    assert "--confirm" not in response.text


def test_the_history_page_says_no_mutation_is_offered_either(client) -> None:
    _restricted()
    flat = " ".join(client.get("/ui/history/decisions/restricted").text.split())
    assert "No correction, retirement or restore is offered from the inspector" in flat


def test_a_visible_record_gets_the_read_only_correction_context(client) -> None:
    from palinode.corrections.review import file_revision

    _visible()
    _memory(
        "decisions/rollout.md",
        "id: decisions-rollout\ntype: Decision\ncategory: decisions\n"
        "title: Rollout\nbacked_by:\n- decisions/visible",
        "The rollout depends on the storage decision.",
    )
    response = client.get("/ui/memory/decisions/visible")
    assert response.status_code == 200
    html = response.text

    assert "Correct or retire this" in html
    # The context a correction needs, available without any input.
    revision = file_revision(str(Path(config.memory_dir) / "decisions/visible.md"))
    assert revision[:12] in html
    assert "decisions/rollout.md" in html, "records referencing this one are shown"
    # The exact, copy-pasteable commands.
    assert "palinode corrections preview --target decisions/visible.md" in html
    assert f"--expect-revision {revision} --confirm" in html
    assert "palinode corrections undo --target decisions/visible.md" in html
    assert "palinode history decisions/visible.md --detail full" in html


def test_the_inspector_offers_no_form_that_posts(client) -> None:
    """Read-only in this release: no in-browser mutation, by construction."""
    _visible()
    html = client.get("/ui/memory/decisions/visible").text.lower()
    assert "<form" not in html
    assert "method=\"post\"" not in html
    assert "fetch(" not in html


def test_pending_candidates_naming_the_record_are_shown_as_proposals(client) -> None:
    from palinode.corrections.queue import CorrectionCandidate, append_candidates

    _visible()
    append_candidates([
        CorrectionCandidate(
            harness="claude-code",
            session_id="session-ui",
            turn_index=3,
            turn_uuid=None,
            span="no, use the hosted store",
            span_hash="hash-ui",
            grep_family="correction",
            matched_rules=("no-thats-wrong",),
            classification="explicit_decision_change",
            classifier={"decided": False, "model": None, "config_role": None, "reason": "n/a"},
            window_turns=(1, 5),
            occurred_at="2026-09-01T10:00:00Z",
            detected_at="2026-09-02T10:00:00Z",
            project="harbor-notes",
            rationale="the user said so",
            replaced="Use the local file store.",
            replacement="Use the hosted store.",
        )
    ])

    html = client.get("/ui/memory/decisions/visible").text
    assert "Pending correction candidates naming this record" in html
    assert "no, use the hosted store" in html
    assert "Proposals — nothing is applied" in html


# ── hostile content ─────────────────────────────────────────────────────────
def test_hostile_content_is_escaped_not_executed(client) -> None:
    """A memory is data. Nothing in it reaches the page as markup or as a command."""
    _memory(
        "decisions/hostile.md",
        "id: decisions-hostile\ntype: Decision\ncategory: decisions\n"
        "title: \"</h1><script>alert('title')</script>\"\nstatus: active",
        "<script>alert('body')</script>\n\n"
        "Injected: `; rm -rf ~ #` and <img src=x onerror=alert('img')>",
    )
    response = client.get("/ui/memory/decisions/hostile")
    assert response.status_code == 200
    html = response.text

    assert "<script>alert('title')</script>" not in html
    assert "<script>alert('body')</script>" not in html
    assert "<img src=x" not in html, "no attacker-controlled element reaches the DOM"
    assert "&lt;script&gt;" in html, "it is shown, escaped, rather than dropped"
    assert "&lt;img src=x onerror=alert(" in html, (
        "the payload is visible as escaped text, which is what a reviewer needs"
    )


def test_a_hostile_filename_cannot_forge_the_correction_command(client) -> None:
    """The CLI commands the page prints are escaped like everything else."""
    _memory(
        "decisions/quote\"-and-<tag>.md",
        "id: decisions-hostile-name\ntype: Decision\ncategory: decisions\n"
        "title: Hostile name\nstatus: active",
        "A decision with an awkward filename.",
    )
    response = client.get("/ui/memory/decisions/quote\"-and-<tag>")
    assert response.status_code == 200
    html = response.text
    assert "<tag>" not in html
    assert "&lt;tag&gt;" in html


def test_traversal_is_still_refused_by_the_path_guard(client) -> None:
    assert client.get("/ui/memory/../../etc/passwd").status_code in (400, 403, 404)
    assert client.get("/ui/history/../../etc/passwd").status_code in (400, 403, 404)


def test_the_ui_is_loopback_only_even_for_a_hidden_record(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bind guard runs before anything else, correction section included."""
    _restricted()
    monkeypatch.setenv("PALINODE_API_HOST", "0.0.0.0")
    assert client.get("/ui/memory/decisions/restricted").status_code == 403
    assert client.get("/ui/history/decisions/restricted").status_code == 403
