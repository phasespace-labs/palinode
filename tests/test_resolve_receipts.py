"""Resolve receipts survive the actual HTTP, MCP, CLI and shipped-hook paths."""
from __future__ import annotations

import json
import hashlib
import re
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.core.config import config
from palinode.core.retrieval_log import MAX_RECEIPT_BYTES
from tests import test_resolve_bundle as scenarios
from tests import test_resolve_hook_live as live

mem = scenarios.mem
live_api = live.live_api


def _id(text):
    return re.search(r"Receipt: ([0-9a-f]+)", text).group(1)


@pytest.mark.parametrize("seed,query", list(scenarios._SCENARIOS.values()) + [
    (scenarios.seed_unlinked_correction, "what is the orbit client retry policy?"),
    (scenarios.seed_contested_stale_backing, "what is the orbit queue depth ceiling?"),
])
@pytest.mark.parametrize("max_items", [0, 1, 10])
def test_explains_exact_delivered_receipt(mem, seed, query, max_items):
    seed(mem)
    with TestClient(app) as client:
        bundle = client.post("/resolve", json={"query": query, "max_items": max_items}).json()
    # A new client/read has no in-process delivery object to reconstruct from.
    with TestClient(app) as client:
        explanation = client.get(f"/explain/{_id(bundle['text'])}", params={"limit": 200}).json()
    assert explanation["status"] in {"explained", "none_delivered"}
    fields = ("ref", "revision", "revision_basis", "disposition")
    assert [tuple(r[k] for k in fields) for r in explanation["supplied"]] == [
        tuple(r[k] for k in fields) for r in bundle["receipt"]["supplied"]
    ]
    assert explanation["delivery"]["budget"] == bundle["budget"]
    assert explanation["delivery"]["coverage"] == bundle["coverage"]
    assert explanation["selection_path"]["steps"] == [
        "query", "full_evidence", "resolution", "budget_packing",
    ]
    assert explanation["acted_on"]["status"] == "not_captured"
    raw = (mem / ".audit/retrievals.jsonl").read_text()
    assert query not in raw
    assert all("statement" not in r and "title" not in r for r in json.loads(raw)["receipt"]["supplied"])
    from palinode.cli.retrieval_stats import _stats

    stats = _stats([json.loads(raw)])
    assert stats["total_events"] == stats["empty_searches"] == 0


@pytest.mark.asyncio
async def test_mcp_and_cli_explain_the_resolve_receipt(mem, monkeypatch):
    scenarios.seed_current(mem)
    monkeypatch.setenv("PALINODE_PROJECT", "demo")
    with TestClient(app) as client:
        async def post(path, json=None, **kwargs):
            return client.post(path, json=json)

        async def get(path, params=None, **kwargs):
            return client.get(path, params=params)

        monkeypatch.setattr(mcp, "_post", post)
        monkeypatch.setattr(mcp, "_get", get)
        resolved = await mcp._tool_resolve({"query": "endpoint production traffic"})
        bundle_id = _id(resolved[0].text)
        explained = await mcp._tool_explain({"bundle_id": bundle_id})
        from palinode.cli.explain import api_client, explain

        monkeypatch.setattr(api_client, "client", client)
        cli = CliRunner().invoke(explain, [bundle_id, "--format", "text"])
        assert cli.exit_code == 0, cli.output
        assert " ".join(cli.output.split()) == " ".join(explained[0].text.split())
        assert "decisions/endpoint-v2" in cli.output
        assert "budget_packing" in cli.output and "Budget:" in cli.output


@live.pytestmark
def test_live_hook_receipt_explains_exact_refs_and_revisions(live_api, mem, tmp_path):
    scenarios.seed_current(mem)
    # Observe the real response without replacing any store/HTTP behavior.
    from palinode.api.routers import resolve

    delivered = []
    original = resolve.build_bundle

    def observe(*args, **kwargs):
        bundle = original(*args, **kwargs)
        delivered.append(bundle)
        return bundle

    with patch.object(resolve, "build_bundle", side_effect=observe):
        context = live._run_hook(tmp_path, live_api, "endpoint production traffic", PALINODE_PROJECT="demo")
    bundle_id = _id(context)
    explanation = httpx.get(f"{live_api}/explain/{bundle_id}").json()
    receipt = next(b.receipt for b in delivered if b.receipt_ref == bundle_id)
    assert explanation["status"] == "explained"
    assert [(r["ref"], r["revision"]) for r in explanation["supplied"]] == [
        (r["ref"], r["revision"]) for r in receipt["supplied"]
    ]


@pytest.mark.parametrize("control", ["recall", "env", "config", "excluded", "invalid"])
def test_controls_prevent_receipt_persistence(mem, monkeypatch, control):
    scenarios.seed_current(mem)
    from palinode.core.capture_policy import update_capture_policy

    if control == "recall":
        update_capture_policy(recall_paused=True)
    elif control == "excluded":
        update_capture_policy(excluded_projects=["demo"])
    elif control == "invalid":
        (mem / ".capture-policy.yaml").write_text("invalid")
    elif control == "env":
        monkeypatch.setenv("PALINODE_INSTRUMENTATION_DISABLED", "1")
    else:
        monkeypatch.setattr(config.instrumentation, "capture_retrievals", False)
    with TestClient(app) as client:
        response = client.post("/resolve", json={"query": "endpoint traffic", "project": "demo"})
    assert response.status_code == (403 if control in {"recall", "invalid"} else 200)
    assert not (mem / ".audit/retrievals.jsonl").exists()


def test_receipt_write_failure_and_bound_do_not_change_delivery(mem, monkeypatch):
    scenarios.seed_current(mem)
    from palinode.core import retrieval_log

    for bound in (0, MAX_RECEIPT_BYTES):
        monkeypatch.setattr(retrieval_log, "MAX_RECEIPT_BYTES", bound)
        if bound:
            (mem / ".audit/retrievals.jsonl").mkdir()
        with TestClient(app) as client:
            response = client.post("/resolve", json={"query": "endpoint production traffic"})
        assert response.status_code == 200
        assert "decisions/endpoint-v2" in response.json()["text"]


def test_stale_file_revision_and_qualifiers_survive_lookup(mem):
    scenarios.seed_current(mem)
    path = mem / "decisions/endpoint-v2.md"
    path.write_text(path.read_text().replace("endpoint bravo", "endpoint charlie"))
    expected_revision = hashlib.sha256(path.read_bytes()).hexdigest()
    with TestClient(app) as client:
        bundle = client.post("/resolve", json={"query": "endpoint production traffic"}).json()
        explained = client.get(f"/explain/{bundle['receipt_ref']}").json()
    record = next(r for r in explained["supplied"] if r["ref"] == "decisions/endpoint-v2")
    assert record["revision"] == expected_revision
    assert record["revision_basis"] == "file_sha256"
    assert record["freshness"] == "stale"
    assert record["qualifiers"] == bundle["selected"][0]["qualifiers"]
    assert record["selection_reasons"] == bundle["selected"][0]["reasons"]
    assert explained["selection_path"]["steps"][0] == "query"
    raw = (mem / ".audit/retrievals.jsonl").read_text()
    assert "endpoint charlie" not in raw


def test_lookup_filters_private_records_qualifiers_and_caps_output(mem):
    scenarios.seed_conflict(mem)
    with TestClient(app) as client:
        bundle = client.post("/resolve", json={"query": "cache cluster region"}).json()
        capped = client.get(f"/explain/{bundle['receipt_ref']}?limit=1").json()
        assert len(capped["supplied"]) == 1
        assert capped["not_shown"]["count"] == 1
        path = mem / "insights/region-b.md"
        path.write_text(path.read_text().replace("---\n", "---\nvisibility: private\n", 1))
        explained = client.get(f"/explain/{bundle['receipt_ref']}").json()
    assert explained["redacted"]["count"] == 1
    assert "insights/region-b" not in json.dumps(explained)


def test_hidden_support_is_not_disclosed_as_a_lineage_anchor(mem):
    scenarios.seed_contested_stale_backing(mem)
    with TestClient(app) as client:
        bundle = client.post("/resolve", json={"query": "orbit queue depth ceiling"}).json()
        path = mem / "research/depth-probe-alpha.md"
        path.write_text(path.read_text().replace("---\n", "---\nvisibility: private\n", 1))
        explained = client.get(f"/explain/{bundle['receipt_ref']}").json()
    assert "research/depth-probe-alpha" not in json.dumps(explained)
    record = next(r for r in explained["supplied"] if r["ref"] == "insights/queue-depth-a")
    assert record["lineage_group"]["reason"] == "withheld_visibility"


@live.pytestmark
def test_live_hook_preserves_budget_and_stale_backing_in_explanation(live_api, mem, tmp_path):
    scenarios.seed_contested_stale_backing(mem)
    context = live._run_hook(
        tmp_path, live_api, "what is the orbit queue depth ceiling?",
        PALINODE_PROJECT="orbit", PALINODE_HOOK_RECALL_MAX_CHARS="1800",
    )
    explained = httpx.get(f"{live_api}/explain/{_id(context)}").json()
    assert explained["status"] == "explained"
    assert 0 < explained["delivery"]["budget"]["max_chars"] < 1800
    record = next(r for r in explained["supplied"] if r["ref"] == "insights/queue-depth-a")
    assert any(q.startswith("stale_backing:") for q in record["qualifiers"])
    assert record["disposition"] == "conflict_side"


def test_capture_pause_still_records_an_explainable_receipt(mem):
    """A paused capture stops memory content being recorded, not the audit of a
    delivery that happened: the hook's printed receipt must still explain."""
    scenarios.seed_current(mem)
    from palinode.core.capture_policy import update_capture_policy
    from palinode.core.explain import explain_delivery

    update_capture_policy(capture_paused=True)
    with TestClient(app) as client:
        body = client.post("/resolve", json={"query": "endpoint traffic", "project": "demo"}).json()
    assert body["receipt_ref"]
    explanation = explain_delivery(body["receipt_ref"], memory_dir=str(mem))
    assert explanation["status"] == "explained", explanation
