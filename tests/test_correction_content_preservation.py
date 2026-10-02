"""Neighboring facts survive a reviewed correction through shipped surfaces."""
from __future__ import annotations

import asyncio
import importlib
import json
import shlex
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient
import frontmatter
import pytest

from palinode.api.server import app
from palinode.cli import main
from palinode.core.bundle import BundleRequest, build_bundle
from palinode.corrections import review
from palinode.corrections.content import content_loss
from tests import test_correction_review as fixtures

store = fixtures.store
_decision = fixtures._decision
_embedded = fixtures._embedded

OLD = "The notifier transport is carrier-old"
NEW = "The notifier transport is carrier-new"
NEIGHBOR = "failed notifications go to the quartz dead-letter queue."


@pytest.mark.parametrize("body", [
    f"{OLD}; {NEIGHBOR}",
    f"{OLD}. {NEIGHBOR}",
    f"- {OLD} <!-- fact:transport -->\n- {NEIGHBOR} <!-- fact:queue -->",
])
def test_preview_and_apply_refuse_neighbor_loss_without_writing(store: Path, body: str) -> None:
    rel = _decision(store, "notifier", body)
    before = (store / rel).read_bytes()
    with TestClient(app) as client:
        payload = {"target": rel, "replacement": NEW}
        preview = client.post("/corrections/preview", json=payload).json()
        assert preview["refused"]
        assert preview["confirm"]["command"] is None
        assert NEIGHBOR in preview["content_loss"]["removed_text"]
        response = client.post("/corrections/apply", json={
            **payload, "confirm": True,
            "expect_revision": preview["confirm"]["expect_revision"],
        })
    assert response.status_code == 409
    assert NEIGHBOR in response.json()["detail"]["detail"]
    assert (store / rel).read_bytes() == before
    assert not (store / "decisions/notifier-corrected.md").exists()


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_cli_preview_names_text_that_would_stop_being_delivered(
    store: Path, monkeypatch: pytest.MonkeyPatch, fmt: str,
) -> None:
    rel = _decision(store, "notifier", f"{OLD}; {NEIGHBOR}")
    module = importlib.import_module("palinode.cli.corrections")
    monkeypatch.setattr(module.api_client, "correction_review", lambda *a: (_ for _ in ()).throw(
        module.RequestError("offline")))
    result = CliRunner().invoke(main, [
        "corrections", "preview", "--target", rel, "--replacement", NEW, "--format", fmt,
    ])
    assert result.exit_code == 1, result.output
    assert NEIGHBOR in " ".join(result.output.split())
    assert "--allow-content-loss" in result.output


def test_cli_correction_keeps_neighbor_in_search_and_bundle(
    store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from palinode.core.store import search_fts
    from palinode.indexer.reconcile import reconcile

    rel = _decision(store, "notifier", f"{OLD}; {NEIGHBOR}")
    _embedded(reconcile, str(store / rel), (store / rel).read_text())
    module = importlib.import_module("palinode.cli.corrections")
    monkeypatch.setattr(module.api_client, "correction_review", lambda *a: (_ for _ in ()).throw(
        module.RequestError("offline")))
    preview = CliRunner().invoke(main, [
        "corrections", "preview", "--target", rel,
        "--replacement", f"{NEW}; {NEIGHBOR}", "--format", "json",
    ])
    assert preview.exit_code == 0, preview.output
    command = json.loads(preview.output)["confirm"]["command"]
    applied = _embedded(CliRunner().invoke, main, shlex.split(command)[1:] + ["--format", "json"])
    assert applied.exit_code == 0, applied.output
    assert frontmatter.load(store / rel)["status"] == "archived"
    replacement = store / "decisions/notifier-corrected.md"
    assert NEW in frontmatter.load(replacement).content
    assert NEIGHBOR in frontmatter.load(replacement).content

    # Reconcile from disk again: delivery cannot depend on cached pre-archive text.
    for path in (store / "decisions").glob("*.md"):
        _embedded(reconcile, str(path), path.read_text())
    hits = search_fts("quartz", top_k=10)
    assert any(NEIGHBOR in hit["content"] and NEW in hit["content"] for hit in hits)
    assert all(OLD not in hit["content"] for hit in hits)
    bundle = _embedded(build_bundle, BundleRequest(query="notifier quartz queue")).to_dict()
    assert NEIGHBOR in bundle["text"], bundle
    assert NEW in bundle["text"]
    assert OLD not in bundle["text"]


def test_explicit_content_loss_is_carried_by_confirmation_command(store: Path) -> None:
    rel = _decision(store, "notifier", f"{OLD}; {NEIGHBOR}")
    preview = review.preview_correction(target=rel, replacement=NEW, allow_content_loss=True)
    assert "refused" not in preview
    assert NEIGHBOR in preview["content_loss"]["removed_text"]
    assert "--allow-content-loss" in preview["confirm"]["command"]
    applied = _embedded(review.apply_correction, target=rel, replacement=NEW,
                        allow_content_loss=True, confirm=True,
                        expect_revision=preview["confirm"]["expect_revision"])
    assert applied["content_loss"]["allowed"] is True
    assert NEIGHBOR not in (store / "decisions/notifier-corrected.md").read_text()


def test_mcp_forwards_explicit_content_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    import palinode.mcp as mcp

    seen = []

    class Response:
        status_code = 200

        def json(self):
            return {"content_loss": {"removed_text": [NEIGHBOR]}}

    async def post(path, json, **kwargs):
        seen.append((path, json))
        return Response()

    monkeypatch.setattr(mcp, "_post", post)
    for phase in ("preview", "apply"):
        result = asyncio.run(mcp._dispatch_tool(f"palinode_correction_{phase}", {
            "target": "decisions/notifier", "replacement": NEW,
            "allow_content_loss": True, "confirm": True, "expect_revision": "revision",
        }))
        assert NEIGHBOR in result[0].text
        assert seen[-1][1]["allow_content_loss"] is True


def test_structural_comparison_ignores_markers_navigation_and_punctuation() -> None:
    body = f"# Notifier\n\n- {OLD}. <!-- fact:one -->\n- {NEIGHBOR} <!-- fact:two -->"
    body += "\n## See also\n<!-- palinode-auto-footer -->\n- [[notifier]] <!-- fact:nav -->"
    loss = content_loss(body, f"{NEW}; {NEIGHBOR.upper()}")
    assert loss == {"removed_text": [f"{OLD}."], "requires_confirmation": False}


def test_claim_selection_preserves_other_fact_lines(store: Path) -> None:
    rel = _decision(store, "notifier", f"- {OLD} <!-- fact:transport -->\n- {NEIGHBOR} <!-- fact:queue -->")
    preview = review.preview_correction(target=rel, claim_id="transport", replacement=NEW)
    assert "refused" not in preview
    _embedded(review.apply_correction, target=rel, claim_id="transport", replacement=NEW,
              confirm=True, expect_revision=preview["confirm"]["expect_revision"])
    body = frontmatter.load(store / rel).content
    assert NEIGHBOR in body
    assert NEW in body


def test_soft_wrapping_a_single_claim_does_not_require_content_loss() -> None:
    loss = content_loss("The notifier transport uses\na persistent connection.", NEW)
    assert loss["requires_confirmation"] is False


def test_retiring_a_document_names_all_text_without_an_extra_flag(store: Path) -> None:
    rel = _decision(store, "notifier", f"{OLD}; {NEIGHBOR}")
    preview = review.preview_correction(target=rel, action="retire", replacement=NEIGHBOR)
    assert "refused" not in preview
    assert NEIGHBOR in preview["content_loss"]["removed_text"]
    assert preview["content_loss"]["requires_confirmation"] is False
