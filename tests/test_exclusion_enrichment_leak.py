"""Negative fixture for automatic enrichment of an already stored memory.

The note predates the exclusion. This deliberately tests subsequent model
egress, without claiming that an exclusion deletes an existing note or history.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import store as memory_store
from palinode.core.config import config
from palinode.indexer.reconcile import reconcile
from palinode.core.ollama_client import OllamaClient


SECRET = "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789"


@pytest.mark.parametrize("scope", ["project", "path", "alias", "mixed", "global", "paused", "invalid"])
def test_stored_memory_enrichment_obeys_project_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, scope: str,
) -> None:
    store = tmp_path / "store"
    note = store / "decisions" / "private-fixture.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: Decision\nentities:\n  - project/private-fixture\n"
        "core: true\n---\nFictional private context: " + SECRET + "\n",
        encoding="utf-8",
    )
    if scope == "alias":
        (store / "entity-aliases.yaml").write_text(
            "aliases:\n  project/private-fixture:\n    - project/private-alias\n", encoding="utf-8"
        )
        note.write_text(note.read_text().replace("project/private-fixture", "project/PRIVATE-ALIAS"))
    elif scope == "mixed":
        note.write_text(note.read_text().replace("core: true", "  - project/public-fixture\ncore: true"))
    elif scope == "global":
        note.write_text(note.read_text().replace("  - project/private-fixture", "  - person/fictional"))
    original = note.read_bytes()
    monkeypatch.setattr(config, "memory_dir", str(store))
    monkeypatch.setattr(config, "db_path", str(store / ".palinode.db"))
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.git, "auto_push", False)
    monkeypatch.setattr(config.auto_summary, "enabled", True)
    monkeypatch.setattr(config.auto_summary, "api", "ollama")
    monkeypatch.setattr(config.auto_summary, "llm_fallbacks", [])

    requests: list[dict] = []

    def generate(self, prompt, **kwargs):
        requests.append({"operation": "generate", "prompt": prompt, **kwargs})
        return {"response": "Fictional safe summary."}

    def chat(self, messages, **kwargs):
        requests.append({"operation": "chat", "messages": messages, **kwargs})
        return "Fictional safe summary."

    def embed(self, text, **kwargs):
        requests.append({"operation": "embed", "text": text, **kwargs})
        return [1.0] + [0.0] * 1023

    def embed_many(self, texts, **kwargs):
        requests.append({"operation": "embed_many", "texts": texts, **kwargs})
        return [[1.0] + [0.0] * 1023 for _ in texts]

    monkeypatch.setattr(OllamaClient, "generate", generate)
    monkeypatch.setattr(OllamaClient, "chat_completions", chat)
    monkeypatch.setattr(OllamaClient, "embed", embed)
    monkeypatch.setattr(OllamaClient, "embed_many", embed_many)

    with TestClient(app) as client:
        reconcile(str(note), note.read_text())
        requests.clear()
        policy = {"excluded_projects": ["private-fixture"]}
        if scope == "path":
            policy = {"excluded_paths": [str(note)]}
        elif scope == "paused":
            policy = {"capture_paused": True, "recall_paused": True}
        assert client.post("/controls", json=policy).status_code == 200
        if scope == "invalid":
            (store / ".capture-policy.yaml").write_text("invalid: policy\n")

        # This is the same backfill route called automatically by the watcher.
        response = client.post("/generate-summaries")
        assert response.status_code == 200

    assert SECRET not in response.text + caplog.text
    assert any(SECRET in row["content"] for row in
               memory_store.search_fts("THISISAHARMLESSNEGATIVEFIXTURE0123456789"))
    blocked = scope in {"project", "alias", "mixed", "invalid"}
    assert (SECRET in json.dumps(requests, default=str)) is not blocked
    if blocked:
        assert note.read_bytes() == original
        assert response.json()["descriptions_generated"] == 0
        assert response.json()["summaries_generated"] == 0
    else:
        assert response.json()["descriptions_generated"] == 1
        assert response.json()["summaries_generated"] == 1


def test_many_note_backfill_resolves_policy_once(tmp_path, monkeypatch):
    from unittest.mock import patch
    from palinode.api.routers.memory import generate_summaries_api
    from palinode.core import capture_policy, scope

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    for i in range(30):
        (tmp_path / f"note-{i}.md").write_text(
            "---\nentities: [project/public]\n---\nStored information.\n"
        )
    capture_policy.update_capture_policy(excluded_projects=["private"])
    for _ in range(2):
        with patch.object(capture_policy, "load_capture_policy", wraps=capture_policy.load_capture_policy) as load, \
                patch.object(scope, "alias_index", wraps=scope.alias_index) as index:
            generate_summaries_api()
            assert load.call_count == 1
            assert index.call_count == 1
