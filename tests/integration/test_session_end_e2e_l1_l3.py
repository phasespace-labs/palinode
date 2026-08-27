"""End-to-end integration test for the `/wrap` save-and-resume loop (issue the
integration test work).

Covers layers 1-3 of the four-layer validation model documented in
`docs/VALIDATION-STRATEGY.md`:

  L1 - Tool fired: `palinode session-end` returns success.
  L2 - Data on disk: daily note, project-status file, and individual indexed
       file all exist with the expected frontmatter.
  L3 - Retrievable: a fresh client (separate from the writer) issues
       `POST /search` against the on-disk SQLite DB and surfaces the record.

Layer 4 (LLM-in-the-loop behavioural test) is deferred — see
`docs/L4-BEHAVIORAL-TESTING-DESIGN.md` for the design sketch and tradeoffs, and issue
the integration tests for slash command prompts work for the LLM-prompt-fidelity
counterpart.

The test uses `tmp_path` for the memory directory and FastAPI's `TestClient` so it
neither touches the user's real `~/palinode` nor needs Ollama running. The CLI is
invoked with Click's `CliRunner`; the singleton ``PalinodeAPI`` client
(``palinode.cli._api.api_client``) has its underlying ``httpx.Client`` swapped for one
with an ``ASGITransport`` so its requests dispatch in-process to the same FastAPI app
the test uses for ``TestClient``. The M0 dual-write (daily append + indexed individual
file) thus round-trips end-to-end without touching the network. (Test-fixture fix for
the session_end CLI rewrite regression after the ADR-010 follow-up bundle's ADR-010
rewrite moved the CLI off the module-level ``httpx.post`` the previous patch hooked.)
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from unittest import mock

import httpx
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from palinode.core.config import config


EMBED_DIM = 1024


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    """Deterministic fake embedder so tests don't need Ollama."""
    return [0.1] * EMBED_DIM


def _today() -> str:
    """The same UTC date stamp the CLI uses to name daily / individual files."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _expected_individual_path(memory_dir: str, summary: str, project: str | None) -> str:
    """Recreate the slug logic from `palinode session-end` to find the
    individual file the CLI's API dual-write produced."""
    today = _today()
    short_hash = hashlib.sha256(summary.encode()).hexdigest()[:8]
    if project:
        slug = f"session-end-{today}-{project}-{short_hash}"
        category = "projects"
    else:
        slug = f"session-end-{today}-{short_hash}"
        category = "insights"
    return os.path.join(memory_dir, category, f"{slug}.md")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_memory(tmp_path, monkeypatch):
    """Point the global config at a fresh tmp memory dir and init real SQLite.

    Mirrors the fixture used by `tests/integration/test_api_roundtrip.py` but
    is request-scoped (not autouse) so individual tests can opt in explicitly.
    """
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")

    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)

    for d in ("people", "projects", "decisions", "insights", "research", "inbox", "daily"):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)

    from palinode.core import store
    store.init_db()

    with (
        mock.patch("palinode.core.embedder.embed", side_effect=_fake_embed),
        mock.patch("palinode.api.server._generate_description", return_value="Test description"),
        mock.patch("palinode.api.server._generate_summary", return_value=""),
    ):
        yield memory_dir


@pytest.fixture()
def api_client():
    """Fresh TestClient against the FastAPI app."""
    from palinode.api.server import app, _rate_counters
    _rate_counters.clear()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def cli_with_api_redirect(api_client):
    """Route the CLI's API singleton through the in-process FastAPI app.

    Post-the ADR-010 follow-up bundle (ADR-010), ``palinode session-end`` no longer calls
    ``httpx.post`` directly — it goes through the ``PalinodeAPI`` singleton
    in ``palinode.cli._api`` whose long-lived ``httpx.Client`` carries the
    ``X-Palinode-Source: cli`` header. The previous fixture patched
    ``httpx.post`` (the module-level function), which the new code path
    doesn't use, so the CLI tried to reach a real localhost:6340 and failed
    with ``ECONNREFUSED``.

    The fix: swap ``api_client.client`` for an ``httpx.Client`` whose
    transport delegates each request to the in-process FastAPI ``TestClient``
    (the same one this test's assertions use). httpx's built-in
    ``ASGITransport`` is async-only and can't drive a sync ``httpx.Client``;
    ``MockTransport`` lets us return responses synchronously, so we use it
    as a thin shim over ``TestClient.request`` (which already handles the
    portal/threadpool dance for the ASGI app).

    The CLI's ``self.client.post(...)`` thus dispatches in-process to the
    same app the test uses for ``TestClient`` — no network, no patching of
    individual call sites, and the real ``PalinodeAPI`` body-shaping /
    error-translation code path is exercised end-to-end.

    Post-the injected-httpx-client refactor: ``PalinodeAPI`` now accepts an injected
    ``httpx.Client`` so the fixture constructs a fresh ``PalinodeAPI(client=...)`` to
    build the test transport, then sets it on the existing singleton so CLI commands
    that hold a direct reference to the singleton object pick it up. On teardown a new
    default client is constructed via ``PalinodeAPI()`` rather than snapshotting and
    restoring the old one. """
    from palinode.cli._api import PalinodeAPI, api_client as cli_api_client
    from palinode.core.defaults import SAVE_SOURCE_HEADER

    def _handler(request: httpx.Request) -> httpx.Response:
        # Delegate to the in-process TestClient so the FastAPI app handles
        # the request the same way it would in production. Forward method,
        # path+query, headers, and body; let TestClient propagate the
        # response (status, headers, body) back as an httpx.Response.
        path = request.url.raw_path.decode("ascii")
        tc_response = api_client.request(
            request.method,
            path,
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(
            status_code=tc_response.status_code,
            headers=tc_response.headers.multi_items(),
            content=tc_response.content,
            request=request,
        )

    # Use the new injection-capable constructor to build a fresh PalinodeAPI
    # whose .client is the test transport. Assign it to the singleton's
    # .client attribute so CLI commands that imported the singleton directly
    # pick up the new transport without any module-level name swap.
    test_api = PalinodeAPI(
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url="http://testserver",
            timeout=30.0,
            headers={SAVE_SOURCE_HEADER: "cli"},
        )
    )
    cli_api_client.client = test_api.client
    try:
        yield
    finally:
        cli_api_client.client.close()
        # Restore a fresh default client — no snapshot needed.
        cli_api_client.client = PalinodeAPI().client


def _index_file(file_path: str) -> None:
    """Manually drive the watcher's per-file indexing path.

    The integration test does not run the watcher daemon — invoking
    `PalinodeHandler._process_file` directly is the same code that `on_created`
    would have run, minus the OS event plumbing.
    """
    from palinode.indexer.watcher import PalinodeHandler
    handler = PalinodeHandler()
    handler._process_file(file_path)


# ---------------------------------------------------------------------------
# L1 — Tool fired
# ---------------------------------------------------------------------------


def test_l1_cli_session_end_returns_success(isolated_memory, cli_with_api_redirect, api_client):
    """`palinode session-end "<summary>"` exits 0 and the CLI's primary
    side effect (a daily note with today's stamp) lands on disk.

    L1 is the smallest layer: did the tool fire without crashing? We check
    the exit code and confirm the CLI's primary write path produced output
    on disk. L2 dives deeper into content; L3 dives deeper into retrieval.
    """
    from palinode.cli.session_end import session_end

    runner = CliRunner()
    summary = "Implemented L1-L3 integration test for issue #139"
    result = runner.invoke(
        session_end,
        [
            summary,
            "-d", "Use TestClient + httpx redirect for dual-write",
            "-b", "L4 deferred to design doc",
            "-p", "palinode",
            "--source", "test",
        ],
    )

    assert result.exit_code == 0, f"CLI failed: {result.output}\n{result.exception}"
    # The CLI prints a confirmation that names the daily file path
    assert "daily/" in result.output
    # And the daily file actually exists on disk
    daily_path = os.path.join(isolated_memory, "daily", f"{_today()}.md")
    assert os.path.exists(daily_path), f"daily note not at {daily_path}"


# ---------------------------------------------------------------------------
# L2 — Data on disk
# ---------------------------------------------------------------------------


def test_l2_daily_note_and_project_status_and_individual_file(
    isolated_memory, cli_with_api_redirect, api_client
):
    """Daily note, project-status, and individual indexed file all exist
    with the right shape after a `palinode session-end -p <project>` call."""
    memory_dir = isolated_memory

    # Pre-create the project status file so the CLI's append path runs
    status_path = os.path.join(memory_dir, "projects", "palinode-status.md")
    with open(status_path, "w", encoding="utf-8") as f:
        f.write("# palinode status\n")

    summary = "L2 test — wrote daily, status, and indexed individual file"

    from palinode.cli.session_end import session_end
    runner = CliRunner()
    result = runner.invoke(
        session_end,
        [
            summary,
            "-d", "Daily append remains the primary path",
            "-d", "Individual file enables fine-grained recall",
            "-b", "Watcher is mocked here; daemon path is covered by other tests",
            "-p", "palinode",
            "--source", "test",
        ],
    )
    assert result.exit_code == 0, result.output

    # --- Daily note ------------------------------------------------------
    daily_path = os.path.join(memory_dir, "daily", f"{_today()}.md")
    assert os.path.exists(daily_path), f"daily note not found at {daily_path}"
    daily_text = open(daily_path, encoding="utf-8").read()
    assert "## Session End" in daily_text
    assert summary in daily_text
    assert "Daily append remains the primary path" in daily_text
    assert "Individual file enables fine-grained recall" in daily_text
    assert "Watcher is mocked here" in daily_text
    assert "**Source:** test" in daily_text

    # --- Project status --------------------------------------------------
    status_text = open(status_path, encoding="utf-8").read()
    assert summary[:80] in status_text, "project-status file did not get the new entry"
    assert f"[{_today()}]" in status_text, "status entry must be date-stamped"

    # --- Individual indexed file ----------------------------------------
    individual_path = _expected_individual_path(memory_dir, summary, project="palinode")
    assert os.path.exists(individual_path), (
        f"individual file not at {individual_path}; "
        f"projects/ has {os.listdir(os.path.join(memory_dir, 'projects'))}"
    )
    ind_text = open(individual_path, encoding="utf-8").read()
    # Frontmatter must round-trip type, project entity, and the source
    parts = ind_text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["type"] == "ProjectSnapshot", fm
    assert "project/palinode" in fm["entities"], fm
    assert fm["category"] == "projects"
    assert fm["source"] == "test"
    # The indexed body must carry the full session-end entry, not just the summary
    assert summary in ind_text


def test_l2_no_project_lands_in_insights(isolated_memory, cli_with_api_redirect, api_client):
    """Without -p, the individual file is an Insight in `insights/` and there
    is no project-status side effect."""
    memory_dir = isolated_memory
    summary = "Quick session — no project specified"

    from palinode.cli.session_end import session_end
    runner = CliRunner()
    result = runner.invoke(
        session_end,
        [summary, "--source", "test"],
    )
    assert result.exit_code == 0, result.output

    individual_path = _expected_individual_path(memory_dir, summary, project=None)
    assert os.path.exists(individual_path), (
        f"individual file not at {individual_path}; "
        f"insights/ has {os.listdir(os.path.join(memory_dir, 'insights'))}"
    )
    ind_text = open(individual_path, encoding="utf-8").read()
    fm = yaml.safe_load(ind_text.split("---", 2)[1])
    assert fm["type"] == "Insight"
    assert fm["category"] == "insights"
    # No project ⇒ no project entity ref
    assert all(not str(e).startswith("project/") for e in fm.get("entities", []))


# ---------------------------------------------------------------------------
# L3 — Retrievable
# ---------------------------------------------------------------------------


def test_l3_search_finds_session_end_record_after_indexing(
    isolated_memory, cli_with_api_redirect, api_client
):
    """After `palinode session-end` writes, indexing the new file makes the
    record retrievable via `POST /search`. This proves the indexer + search
    surface picks up session-end captures end-to-end.

    A *fresh* TestClient (one that did not handle any of the writes) is the
    one issuing the search — that simulates the cross-session-boundary
    recall path the four-layer model exists to validate.
    """
    memory_dir = isolated_memory
    distinctive_phrase = "marmot-quokka-zither"  # unlikely to collide with anything
    summary = f"L3 retrieval test — distinctive token {distinctive_phrase}"

    from palinode.cli.session_end import session_end
    runner = CliRunner()
    result = runner.invoke(
        session_end,
        [
            summary,
            "-d", f"Search must surface the {distinctive_phrase} token",
            "-p", "palinode",
            "--source", "test",
        ],
    )
    assert result.exit_code == 0, result.output

    individual_path = _expected_individual_path(memory_dir, summary, project="palinode")
    assert os.path.exists(individual_path)

    # Drive the watcher's per-file indexing path explicitly. In production
    # the watchdog daemon does this automatically on file create; here we
    # invoke `_process_file` directly so the test is hermetic and fast.
    _index_file(individual_path)

    # Sanity: chunks landed in the DB
    from palinode.core import store
    stats = store.get_stats()
    total = stats.get("total_chunks") or stats.get("chunks") or 0
    assert total >= 1, f"expected at least one indexed chunk, got {stats}"

    # ---- The L3 assertion -------------------------------------------------
    # A fresh TestClient (separate from the one that fired the writes)
    # issues POST /search. It reads the SQLite DB on disk, so a hit here
    # proves the indexed record is retrievable across "session boundaries".
    from palinode.api.server import app, _rate_counters
    _rate_counters.clear()
    fresh_client = TestClient(app, raise_server_exceptions=False)

    resp = fresh_client.post(
        "/search",
        json={"query": distinctive_phrase, "threshold": 0.0, "limit": 5},
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert len(results) >= 1, "search returned no results — index missed the new file"

    # The hit must reference the individual file we just wrote
    matched = [
        r for r in results
        if individual_path in r.get("file_path", "") or individual_path in r.get("file", "")
    ]
    assert matched, (
        f"none of the {len(results)} hits matched {individual_path}: "
        f"{[r.get('file_path') or r.get('file') for r in results]}"
    )


def test_l3_keyword_search_via_fts_surfaces_session_end(
    isolated_memory, cli_with_api_redirect, api_client
):
    """A second L3 angle: hybrid search (BM25 + vector) finds the record by
    a keyword that appears in the session-end summary but not the surrounding
    boilerplate. Guards against the vector-only path masking an FTS gap."""
    memory_dir = isolated_memory
    keyword = "axolotl"  # uncommon token — appears only in our summary
    summary = f"Session covered the {keyword} migration plan"

    from palinode.cli.session_end import session_end
    runner = CliRunner()
    result = runner.invoke(
        session_end,
        [summary, "-p", "palinode", "--source", "test"],
    )
    assert result.exit_code == 0, result.output

    individual_path = _expected_individual_path(memory_dir, summary, project="palinode")
    assert os.path.exists(individual_path)
    _index_file(individual_path)

    from palinode.api.server import app, _rate_counters
    _rate_counters.clear()
    fresh_client = TestClient(app, raise_server_exceptions=False)
    resp = fresh_client.post(
        "/search",
        json={"query": keyword, "threshold": 0.0, "limit": 5, "hybrid": True},
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()
    assert any(
        keyword in (r.get("content") or "") for r in results
    ), f"keyword {keyword!r} not surfaced in {results}"
