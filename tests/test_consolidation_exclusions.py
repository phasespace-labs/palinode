"""Project exclusions cover every stored-note input to automatic consolidation."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import yaml

from palinode.consolidation import runner, write_time
from palinode.core import embedder, store
from palinode.core.capture_policy import update_capture_policy
from palinode.core.config import config
from tests._store_helpers import upsert_chunks

SECRET = "sk-ant-THISISAHARMLESSNEGATIVEFIXTURE0123456789"
VECTOR = [1.0] + [0.0] * 1023


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.git, "auto_push", False)
    monkeypatch.setattr(config.consolidation.write_time, "enabled", True)
    monkeypatch.setattr(embedder, "embed", lambda *a, **kw: VECTOR)
    store.init_db()
    return tmp_path


def _note(memory, relative, body, projects=()):
    path = memory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"entities": [f"project/{p}" for p in projects],
                "last_updated": datetime.now(UTC).isoformat()}
    path.write_text("---\n" + yaml.safe_dump(metadata) + "---\n" + body)
    return path, metadata


@pytest.mark.parametrize("cadence", ["weekly", "nightly"])
@pytest.mark.parametrize("role", ["note", "body-ref", "target", "decision"])
def test_excluded_inputs_never_enter_consolidation_prompts(memory, caplog, cadence, role):
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    for project in ("public", "private"):
        _note(memory, f"projects/{project}.md",
              f"- Existing fact. <!-- fact:{project}1 -->\n", [project])
    _note(memory, f"daily/{today}-public.md", "Allowed new information.", ["public"])
    if role in {"note", "body-ref"}:
        body = SECRET + (" project/private project/public" if role == "body-ref" else "")
        # A shared note must not leak through the allowed project's group.
        _note(memory, f"daily/{today}-private.md", body,
              [] if role == "body-ref" else ["private", "public"])
    elif role == "target":
        _note(memory, "projects/public.md", f"- {SECRET} <!-- fact:public1 -->\n",
              ["private", "public"])
    else:
        _note(memory, "decisions/private.md", SECRET, ["private", "public"])
    before = {p: p.read_bytes() for p in memory.rglob("*.md")}
    update_capture_policy(excluded_projects=["private"])
    requests = []

    def model(system, user):
        requests.append(system + user)
        return "[]", "fixture"

    run = runner.run_nightly if cadence == "nightly" else runner.run_consolidation
    run(llm_fn=model)
    assert SECRET not in json.dumps(requests) + caplog.text
    if role != "target":
        assert any("Allowed new information" in prompt for prompt in requests)
    assert all(p.read_bytes() == content for p, content in before.items()
               if SECRET.encode() in content or "projects" in p.parts)


def test_path_exclusion_and_pause_do_not_block_consolidation(memory):
    _note(memory, "projects/public.md", "- Fact. <!-- fact:public1 -->\n", ["public"])
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    note, _ = _note(memory, f"daily/{today}.md", "Keep processing this stored note.", ["public"])
    update_capture_policy(excluded_paths=[str(note)], capture_paused=True, recall_paused=True)
    requests = []

    def model(system, user):
        requests.append(user)
        return "[]", "fixture"

    runner.run_consolidation(llm_fn=model)
    assert any("Keep processing this stored note" in prompt for prompt in requests)


@pytest.mark.parametrize("excluded", ["item", "candidate", "queued-file"])
def test_write_time_contradiction_check_skips_excluded_content(memory, caplog, excluded):
    candidate, meta = _note(memory, "insights/candidate.md", SECRET,
                            ["private"] if excluded == "candidate" else ["public"])
    upsert_chunks([{
        "id": "candidate", "file_path": str(candidate), "section_id": "root",
        "category": "insights", "content": SECRET, "metadata": meta,
        "embedding": VECTOR,
    }], skip_unchanged=False)
    item = {"content": "A new candidate.", "category": "insights",
            "entities": ["project/private" if excluded == "item" else "project/public"]}
    source, _ = _note(memory, "insights/new.md", item["content"],
                       ["private"] if excluded == "queued-file" else ["public"])
    if excluded != "candidate":
        item["content"] = SECRET
    update_capture_policy(excluded_projects=["private"])
    requests = []

    def model(system, user):
        requests.append(system + user)
        return '{"operation":"NOOP"}', "fixture"

    write_time.schedule_contradiction_check(str(source), item, sync=True, llm_fn=model)
    assert not requests
    assert SECRET not in caplog.text
    # Exclusion changes model processing, not the index or explicit recall.
    assert any(SECRET in row["content"] for row in store.search_fts("THISISAHARMLESSNEGATIVEFIXTURE0123456789"))


@pytest.mark.parametrize("cadence", ["weekly", "nightly"])
def test_many_note_pass_resolves_policy_once_and_refreshes(memory, cadence):
    from unittest.mock import patch
    from palinode.core import capture_policy, scope

    _note(memory, "projects/public.md", "- Fact. <!-- fact:public1 -->\n", ["public"])
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    for i in range(20):
        _note(memory, f"daily/{today}-{i}.md", f"New information {i}.", ["public"])
        _note(memory, f"decisions/{i}.md", f"Governing decision {i}.", ["public"])
    update_capture_policy(excluded_projects=["private"])
    run = runner.run_nightly if cadence == "nightly" else runner.run_consolidation
    requests = []

    def model(system, user):
        requests.append(user)
        return "[]", "fixture"

    with patch.object(capture_policy, "load_capture_policy", wraps=capture_policy.load_capture_policy) as load, \
            patch.object(scope, "alias_index", wraps=scope.alias_index) as index:
        run(llm_fn=model)
        assert requests
        assert load.call_count == 1
        assert index.call_count == 1
    update_capture_policy(excluded_projects=["public"])
    requests.clear()
    with patch.object(capture_policy, "load_capture_policy", wraps=capture_policy.load_capture_policy) as load, \
            patch.object(scope, "alias_index", wraps=scope.alias_index) as index:
        run(llm_fn=model)
        assert not requests
        assert load.call_count == 1
        assert index.call_count == 1


def test_standalone_contradiction_batch_reuses_resolution(memory, monkeypatch):
    from unittest.mock import patch
    from palinode.core import capture_policy, scope

    update_capture_policy(excluded_projects=["private"])
    monkeypatch.setattr(store, "search_internal", lambda *a, **kw: [
        {"content": SECRET, "metadata": {"entities": ["project/private"]}}
    ])
    items = [{"content": f"Public item {i}", "entities": ["project/public"]}
             for i in range(20)]
    with patch.object(capture_policy, "load_capture_policy", wraps=capture_policy.load_capture_policy) as load, \
            patch.object(scope, "alias_index", wraps=scope.alias_index) as index:
        operations = runner._check_contradictions(items, "public")
        assert len(operations) == len(items)
        assert all(op["operation"] == "ADD" for op in operations)
        assert load.call_count == 1
        assert index.call_count == 1
    update_capture_policy(excluded_projects=["public"])
    assert runner._check_contradictions(items, "public") == []
