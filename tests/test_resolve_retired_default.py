"""Automatic delivery follows search: retired records stay out by default.

``/search`` has always left archived records out of default recall. The
per-turn hook's ``POST /resolve`` did not: a retired record reached the
bundle — as an unlinked discovery beside a current record on the same
subject, as a ``Replaced`` / ``Unknown`` entry for a retired seed, or as a
``replaces:`` ref — labelled, but carrying its text. A label does not stop an
agent answering with the value, so the default now matches search: a retired
record is not delivered unless the request asks for history
(``include_retired``) or names the record itself (``ref`` / ``context``).

Real SQLite and real git under ``tmp_path``; records are retired through the
shipping archive path, and "restart" is a full rebuild of the index from the
files, so nothing here depends on state a live process was holding.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.core import store
from palinode.core.bundle import BundleRequest, build_bundle
from palinode.core.config import config
from palinode.indexer import reconcile
from tests import test_resolve_bundle as scenarios
from tests import test_resolve_hook_live as live

mem = scenarios.mem
live_api = live.live_api
_write = scenarios._write

#: The retired value. Distinctive so a substring check cannot pass by accident.
RETIRED_VALUE = "vireo-kestrel-q"
QUESTION = "Which queue does orbitkit use?"


def seed_forgotten_queue(mem) -> None:
    """Smoke scenario 9.4's shape: a current fact, and a second one forgotten.

    Both share the subject entity, which is how the retired one used to reach
    a bundle seeded by the current one: the evidence layer's entity-lookup
    discovery found it and the renderer printed its statement.
    """
    from palinode.consolidation.archive import archive_memory

    _write(mem, "decisions/orbitkit-cache.md",
           "# Orbitkit cache\n\nOrbitkit uses valkey-ember for its cache and its queue workers.",
           type="Decision", status="active", date="2026-09-20",
           entities=["project/orbitkit"])
    _write(mem, f"decisions/orbitkit-uses-{RETIRED_VALUE}-for-its-queue.md",
           f"# Orbitkit queue\n\nOrbitkit uses {RETIRED_VALUE} for its queue.",
           type="Decision", status="active", date="2026-09-21",
           entities=["project/orbitkit"])
    result = archive_memory(
        f"decisions/orbitkit-uses-{RETIRED_VALUE}-for-its-queue.md",
        reason="forgotten on request",
    )
    assert result["status"] == "archived", result


def rebuild_index(mem) -> None:
    """The "restart": drop the index and rebuild it from the files alone."""
    for name in (".palinode.db", ".palinode.db-wal", ".palinode.db-shm"):
        try:
            os.remove(mem / name)
        except FileNotFoundError:
            pass
    store.init_db()
    for root, dirs, files in os.walk(mem):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.endswith(".md"):
                path = os.path.join(root, fn)
                with open(path, encoding="utf-8") as fh:
                    reconcile.reconcile(path, fh.read())


@pytest.fixture(autouse=True)
def _client_in_orbitkit(monkeypatch):
    """The client works in orbitkit, the project every record here is about.

    Pinned so the CLI and MCP surfaces do not scope to whatever checkout the
    suite runs in: a project-scoped request leaves other projects' records out.
    """
    monkeypatch.setenv("PALINODE_PROJECT", "orbitkit")


def _retired_ref() -> str:
    return f"decisions/orbitkit-uses-{RETIRED_VALUE}-for-its-queue"


def test_the_store_under_test_is_the_tmp_one(mem):
    """No test here may touch a real store."""
    assert config.memory_dir == str(mem)
    assert str(config.db_path).startswith(str(mem))


def test_default_resolve_does_not_carry_the_retired_value(mem):
    seed_forgotten_queue(mem)
    rebuild_index(mem)

    data = build_bundle(BundleRequest(query=QUESTION)).to_dict()

    assert RETIRED_VALUE not in json.dumps(data), data["text"]
    assert data["replaced"] == []
    assert all(i["ref"] != _retired_ref() for i in data["insufficient"])
    # The current record on the same subject is still delivered.
    assert "decisions/orbitkit-cache" in {s["ref"] for s in data["selected"]}
    assert data["history_withheld"] >= 1


def test_include_retired_delivers_it_labelled(mem):
    seed_forgotten_queue(mem)
    rebuild_index(mem)

    data = build_bundle(BundleRequest(query=QUESTION, include_retired=True)).to_dict()

    assert RETIRED_VALUE in data["text"]
    line = next(
        line for line in data["text"].splitlines() if RETIRED_VALUE in line
    )
    assert "[retired]" in line or "retired_no_successor" in line, line
    assert data["history_withheld"] == 0


def test_a_retired_seed_is_withheld_by_default_and_shown_on_request(mem):
    """A retired record reached as a seed (not via search's status filter:
    a ``superseded_by`` with no visible successor keeps ``status: active``)."""
    _write(mem, "decisions/ledger-format.md",
           "# Ledger format\n\nThe ledger is written as quillstone-csv.",
           type="Decision", status="active", date="2026-09-01",
           entities=["project/ledger"], superseded_by="decisions/not-there")
    rebuild_index(mem)

    default = build_bundle(BundleRequest(query="ledger format written")).to_dict()
    assert "quillstone" not in json.dumps(default)
    assert default["insufficient"] == [] and default["replaced"] == []
    assert default["history_withheld"] >= 1

    history = build_bundle(
        BundleRequest(query="ledger format written", include_retired=True)
    ).to_dict()
    assert "decisions/ledger-format" in {r["ref"] for r in history["replaced"]}
    assert "decisions/ledger-format" in {i["ref"] for i in history["insufficient"]}


def test_supersession_delivers_the_current_record_and_withholds_the_old_ref(mem):
    """B replaced A: B is delivered, with a note that it replaced an earlier
    record — without A's ref or text, unless history is asked for.

    A is retired by a hand-written ``superseded_by`` and still claims
    ``status: active``, which is the shape search's status filter lets
    through — so the query reaches A as a seed and the chain leads to B.
    """
    _write(mem, "decisions/endpoint-alpha-host.md",
           "# Endpoint\n\nProduction serves traffic from endpoint alpha via the "
           "legacy gateway.",
           type="Decision", status="active", date="2026-08-01",
           entities=["project/demo"], superseded_by="decisions/endpoint-v2")
    _write(mem, "decisions/endpoint-v2.md",
           "# Endpoint v2\n\nProduction serves traffic from endpoint bravo.",
           type="Decision", status="active", date="2026-09-01",
           entities=["project/demo"])
    rebuild_index(mem)

    data = build_bundle(BundleRequest(query="legacy gateway")).to_dict()
    selected = {s["ref"]: s for s in data["selected"]}
    assert "decisions/endpoint-v2" in selected, data["text"]
    assert "bravo" in data["text"]
    assert "alpha" not in json.dumps(data), data["text"]
    assert data["replaced"] == []
    assert selected["decisions/endpoint-v2"]["refs"]["replaces"] == []
    assert "replaces: 1 earlier record (retired; withheld)" in data["text"]
    assert data["history_withheld"] >= 1

    history = build_bundle(
        BundleRequest(query="legacy gateway", include_retired=True)
    ).to_dict()
    assert "decisions/endpoint-alpha-host" in {r["ref"] for r in history["replaced"]}
    assert "replaces: decisions/endpoint-alpha-host" in history["text"]
    assert history["history_withheld"] == 0


def test_a_named_ref_is_still_reported_replaced(mem):
    """``ref`` / ``context`` name a record the caller already holds; telling
    them it was replaced is the point, so it is not withheld."""
    scenarios.seed_current(mem)
    data = build_bundle(BundleRequest(ref="decisions/endpoint")).to_dict()
    assert [r["ref"] for r in data["replaced"]] == ["decisions/endpoint"]
    assert [s["ref"] for s in data["selected"]] == ["decisions/endpoint-v2"]


def test_rest_default_and_explicit(mem):
    seed_forgotten_queue(mem)
    rebuild_index(mem)
    with TestClient(app) as client:
        default = client.post("/resolve", json={"query": QUESTION})
        assert default.status_code == 200, default.text
        assert RETIRED_VALUE not in default.text

        history = client.post("/resolve", json={"query": QUESTION, "include_retired": True})
        assert history.status_code == 200, history.text
        assert RETIRED_VALUE in history.json()["text"]


@pytest.mark.asyncio
async def test_mcp_forwards_include_retired(mem, monkeypatch):
    seed_forgotten_queue(mem)
    rebuild_index(mem)
    with TestClient(app) as client:
        sent: list[dict] = []

        class _Resp:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        async def _fake_post(path, json=None, timeout=30.0):
            sent.append(json)
            return _Resp(client.post(path, json=json).json())

        monkeypatch.setattr(mcp, "_post", _fake_post)
        default = await mcp._tool_resolve({"query": QUESTION})
        history = await mcp._tool_resolve({"query": QUESTION, "include_retired": True})

    assert "include_retired" not in sent[0]
    assert sent[1]["include_retired"] is True
    assert RETIRED_VALUE not in default[0].text
    assert RETIRED_VALUE in history[0].text


def test_cli_include_retired_flag(mem, monkeypatch):
    from click.testing import CliRunner

    from palinode.cli.resolve import api_client
    from palinode.cli.resolve import resolve as cli_resolve

    seed_forgotten_queue(mem)
    rebuild_index(mem)
    with TestClient(app) as client:
        sent: list[dict] = []

        def _fake(**kw):
            body = {k: v for k, v in kw.items() if v not in (None, False)}
            sent.append(body)
            return client.post("/resolve", json=body).json()

        monkeypatch.setattr(api_client, "resolve", _fake)
        default = CliRunner().invoke(cli_resolve, [QUESTION, "--format", "text"])
        history = CliRunner().invoke(
            cli_resolve, [QUESTION, "--include-retired", "--format", "text"]
        )
    assert default.exit_code == 0 and history.exit_code == 0, (default.output, history.output)
    assert RETIRED_VALUE not in default.output
    assert RETIRED_VALUE in history.output


def test_context_prime_does_not_carry_the_retired_value(mem):
    seed_forgotten_queue(mem)
    rebuild_index(mem)
    with TestClient(app) as client:
        resp = client.post("/context/prime", json={"cwd": str(mem), "session_id": "s1"})
        assert resp.status_code == 200, resp.text
        assert RETIRED_VALUE not in resp.text


@pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("jq")),
    reason="the hook needs curl and jq on PATH",
)
def test_the_shipped_hook_does_not_carry_the_retired_value(live_api, mem, tmp_path):
    """The exact request the shipped per-turn hook sends, end to end."""
    seed_forgotten_queue(mem)
    rebuild_index(mem)
    context = live._run_hook(tmp_path, live_api, QUESTION, PALINODE_PROJECT="orbitkit")
    assert RETIRED_VALUE not in context, context
    assert "resolution unavailable" not in context


def test_the_hook_sends_no_history_option():
    """The hook asks for the default; history is never an automatic request."""
    from palinode.cli.init import USER_PROMPT_SUBMIT_HOOK_SCRIPT

    assert "include_retired" not in USER_PROMPT_SUBMIT_HOOK_SCRIPT
    hook = os.path.join(
        os.path.dirname(__file__), "..", "examples", "hooks",
        "palinode-user-prompt-submit.sh",
    )
    with open(hook, encoding="utf-8") as fh:
        assert "include_retired" not in fh.read()
    # And the resolve payload it builds is the query, the client's scope and
    # the budget, with no history option.
    out = subprocess.run(
        ["grep", "-n", "{query: $q} + $scope + {max_items: $items, max_chars: $chars}", hook],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, "the hook's /resolve payload shape changed"


# ── search's evidence layer follows the same default ─────────────────────────


def _evidence_text(hits: list[dict]) -> str:
    return json.dumps([
        {k: h.get(k) for k in ("evidence", "resolution")} for h in hits
    ])


def _search_hits(client, **extra) -> list[dict]:
    body = {"query": "orbitkit cache", "limit": 3, "threshold": 0.0,
            "resolve": "full", **extra}
    resp = client.post("/search", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["results"] if isinstance(data, dict) else data


@pytest.mark.parametrize("mode", ["linked", "full"])
def test_search_evidence_withholds_retired_by_default(mem, mode):
    """An agent that asks search for evidence is not asking for history."""
    seed_forgotten_queue(mem)
    rebuild_index(mem)
    with TestClient(app) as client:
        hits = _search_hits(client, resolve=mode)
    assert hits, "the current record is still found"
    assert RETIRED_VALUE not in _evidence_text(hits)
    assert all("history_withheld" in h["evidence"] for h in hits)


def test_search_full_discovery_withheld_then_shown_with_include_retired(mem):
    seed_forgotten_queue(mem)
    rebuild_index(mem)
    with TestClient(app) as client:
        default = _search_hits(client)
        history = _search_hits(client, include_retired=True)

    assert RETIRED_VALUE not in _evidence_text(default)
    assert sum(h["evidence"]["history_withheld"] for h in default) >= 1

    shown = [
        rec for h in history for rec in h["evidence"]["discovered"]
        if RETIRED_VALUE in (rec.get("excerpt") or "")
    ]
    assert shown, _evidence_text(history)
    assert all(rec["currency"] == "retired" for rec in shown)
    assert all("history_withheld" not in h["evidence"] for h in history)


@pytest.mark.asyncio
async def test_mcp_search_withholds_by_default_and_forwards_include_retired(mem, monkeypatch):
    seed_forgotten_queue(mem)
    rebuild_index(mem)
    with TestClient(app) as client:
        sent: list[dict] = []

        class _Resp:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        async def _fake_post(path, json=None, timeout=30.0):
            sent.append(json)
            return _Resp(client.post(path, json=json).json())

        monkeypatch.setattr(mcp, "_post", _fake_post)
        default = await mcp._tool_search(
            {"query": "orbitkit cache", "resolve": "full", "threshold": 0.0}
        )
        history = await mcp._tool_search(
            {"query": "orbitkit cache", "resolve": "full", "threshold": 0.0,
             "include_retired": True}
        )

    assert "include_retired" not in sent[0]
    assert sent[1]["include_retired"] is True
    assert RETIRED_VALUE not in default[0].text
    line = next(
        (ln for ln in history[0].text.splitlines() if RETIRED_VALUE in ln), ""
    )
    assert "discovered via" in line and "⚠ retired" in line, history[0].text


def test_cli_search_forwards_include_retired(monkeypatch):
    from click.testing import CliRunner

    from palinode.cli.search import api_client
    from palinode.cli.search import search as cli_search

    sent: dict = {}

    def _fake(query, **kw):
        sent.update(kw)
        return [], None

    monkeypatch.setattr(api_client, "search", _fake)
    result = CliRunner().invoke(
        cli_search, ["q", "--resolve", "full", "--include-retired", "--no-context"]
    )
    assert result.exit_code == 0, result.output
    assert sent["include_retired"] is True
