"""Every walker applies the same "never memory" rule (issue 251).

One fixture store (``tests/_memory_rule_store.py``), real SQLite, the embedder
stubbed. Each case drives a surface's own entry point and compares the set of
files it reaches to the row for its group. The sets are written out in the
fixture module, so a walker that keeps a private skip list fails here.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from watchdog.events import FileCreatedEvent

from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from palinode.diagnostics.types import DoctorContext
from tests import _memory_rule_store as fixture

def _stems(paths) -> set[str]:
    out = set()
    for raw in paths:
        rel = os.path.relpath(raw, config.memory_dir) if os.path.isabs(raw) else raw
        rel = rel.replace(os.sep, "/")
        out.add(rel[:-3] if rel.endswith(".md") else rel)
    return out


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    memory_dir = tmp_path / "store"
    memory_dir.mkdir()
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    monkeypatch.setattr(config, "db_path", str(memory_dir / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    monkeypatch.setattr(store, "_db_checked", False)
    monkeypatch.setenv("PALINODE_DIR", str(memory_dir))
    return memory_dir


@pytest.fixture()
def embedder_stub(monkeypatch: pytest.MonkeyPatch):
    size = config.embeddings.primary.dimensions
    with mock.patch("palinode.core.embedder.embed", return_value=[0.1] * size) as stub:
        yield stub


@pytest.fixture()
def client(root: Path):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _indexed_stems() -> set[str]:
    db = store.get_db()
    try:
        rows = db.execute("SELECT DISTINCT file_path FROM chunks").fetchall()
    finally:
        db.close()
    return _stems(r[0] for r in rows)


# ── The index ────────────────────────────────────────────────────────────────


def _watcher_events(root: Path, *, write: bool = True, **_: object) -> set[str]:
    from palinode.indexer.watcher import PalinodeHandler

    if write:
        fixture.write_store(root)
    handler = PalinodeHandler()
    scheduled: list[str] = []
    handler._schedule_index = lambda path, **kw: scheduled.append(path)  # type: ignore[method-assign]
    try:
        for stem in (fixture.FILES if write else ("notes/a",)):
            handler.on_created(FileCreatedEvent(str(root / f"{stem}.md")))
    finally:
        handler.shutdown()
    return _stems(scheduled)


def _reindex(root: Path, client: TestClient, **_: object) -> set[str]:
    fixture.write_store(root)
    response = client.post("/reindex")
    assert response.status_code == 200, response.text
    return _indexed_stems()


def _search(root: Path, client: TestClient, **_: object) -> set[str]:
    fixture.write_store(root)
    assert client.post("/reindex").status_code == 200
    response = client.post(
        "/search", json={"query": "fixture body harbor", "limit": 50, "threshold": 0.0}
    )
    assert response.status_code == 200, response.text
    return _stems(hit["file_path"] for hit in response.json())


INDEX_SURFACES: dict[str, Callable[..., set[str]]] = {
    "watcher event": _watcher_events,
    "reindex": _reindex,
    "search": _search,
}


@pytest.mark.parametrize("surface", INDEX_SURFACES)
def test_the_index_holds_exactly_the_memory_files(
    surface: str, root: Path, client: TestClient, embedder_stub
) -> None:
    reached = INDEX_SURFACES[surface](root, client=client)

    assert reached == fixture.INDEX, (
        f"extra: {sorted(reached - fixture.INDEX)}, missing: {sorted(fixture.INDEX - reached)}"
    )


# ── Listing: curated current memory ──────────────────────────────────────────


def _list_endpoint(root: Path, client: TestClient) -> set[str]:
    fixture.write_store(root)
    response = client.get("/list")
    assert response.status_code == 200, response.text
    return _stems(row["file"] for row in response.json())


def _browse_ui(root: Path, client: TestClient) -> set[str]:
    from palinode.api.ui.views import scan_memory_files

    fixture.write_store(root)
    return _stems(row["path"] for row in scan_memory_files())


def _project_review(root: Path, client: TestClient) -> set[str]:
    from palinode.core.review import run_review

    fixture.write_store(root, description=None)
    return _stems(run_review("harbor")["findings"]["missing_descriptions"])


def _write_withdrawn_source(root: Path) -> None:
    source = root / "sources" / "s.md"
    source.parent.mkdir()
    source.write_text(
        "---\nid: sources-s\ntype: Decision\ncategory: sources\nstatus: archived\n---\n\nWithdrawn.\n"
    )


def _backed_by_propagation(root: Path, client: TestClient) -> set[str]:
    from palinode.consolidation.propagate import find_dependents

    fixture.write_store(root, backed_by=["sources/s"])
    return _stems(find_dependents(["sources/s"], str(root)))


def _revalidation_sweep(root: Path, client: TestClient) -> set[str]:
    from palinode.core.revalidation import sweep_revalidations

    fixture.write_store(root, backed_by=["sources/s"])
    _write_withdrawn_source(root)
    return _stems(sweep_revalidations(str(root), enqueue=False))


LISTING_SURFACES: dict[str, Callable[..., set[str]]] = {
    "GET /list": _list_endpoint,
    "browse UI": _browse_ui,
    "project review": _project_review,
    "backed_by propagation": _backed_by_propagation,
    "revalidation sweep": _revalidation_sweep,
}


@pytest.mark.parametrize("surface", LISTING_SURFACES)
def test_a_listing_shows_only_curated_current_memory(
    surface: str, root: Path, client: TestClient, embedder_stub
) -> None:
    reached = LISTING_SURFACES[surface](root, client)

    assert reached == fixture.LISTING, (
        f"extra: {sorted(reached - fixture.LISTING)}, missing: {sorted(fixture.LISTING - reached)}"
    )


# ── The session-start digest ─────────────────────────────────────────────────


def _context_digest(root: Path) -> set[str]:
    from palinode.core.context_prime import build_context_digest

    fixture.write_store(root, core=True)
    digest = build_context_digest(project="harbor")
    return _stems(entry["file"] for entry in digest["core_memories"])


def test_the_digest_reads_inbox_and_nothing_never_memory(root: Path, embedder_stub) -> None:
    reached = _context_digest(root)

    assert reached == fixture.DIGEST, (
        f"extra: {sorted(reached - fixture.DIGEST)}, missing: {sorted(fixture.DIGEST - reached)}"
    )


# ── Maintenance walks: lint, contradictions, backfill, obsidian sync ─────────


def _lint_pass(root: Path, client: TestClient) -> set[str]:
    from palinode.core.lint import run_lint_pass

    fixture.write_store(root, core=True)
    return _stems(run_lint_pass()["missing_expiry"])


def _backfill_plan(root: Path, client: TestClient) -> set[str]:
    from palinode.migration.frontmatter_backfill import plan_backfill

    fixture.write_store(root, id=None)
    plan = plan_backfill(str(root))
    walked = [p.path for p in plan.planned] + list(plan.conformant)
    walked += [rel for rel, _ in plan.excluded] + [rel for rel, _ in plan.unreadable]
    return _stems(walked)


def _obsidian_sync(root: Path, client: TestClient) -> set[str]:
    from click.testing import CliRunner

    from palinode.cli.obsidian_sync import obsidian_sync

    fixture.write_store(root)
    result = CliRunner().invoke(obsidian_sync, [])
    assert result.exit_code == 0, result.output
    prefix = "would update: "
    return _stems(
        line[len(prefix):].split(" (added:")[0]
        for line in result.output.splitlines()
        if line.startswith(prefix)
    )


LINT_SURFACES: dict[str, Callable[..., set[str]]] = {
    "lint pass": _lint_pass,
    "frontmatter backfill": _backfill_plan,
    "obsidian sync": _obsidian_sync,
}


@pytest.mark.parametrize("surface", LINT_SURFACES)
def test_a_maintenance_walk_checks_daily_and_inbox_but_not_archive(
    surface: str, root: Path, client: TestClient, embedder_stub
) -> None:
    reached = LINT_SURFACES[surface](root, client)

    assert reached == fixture.LINT, (
        f"extra: {sorted(reached - fixture.LINT)}, missing: {sorted(fixture.LINT - reached)}"
    )


def test_the_deep_contradiction_check_reads_the_same_decisions_as_lint(
    root: Path, client: TestClient, embedder_stub
) -> None:
    from palinode.lint.contradictions import run_deep_contradiction_check

    fixture.write_store(root)

    report = run_deep_contradiction_check(memory_dir=str(root), _llm_caller=lambda *a, **k: "")

    assert report["decisions_found"] == len(fixture.LINT)


# ── TTL ──────────────────────────────────────────────────────────────────────


def test_the_ttl_sweep_skips_daily_at_any_depth_and_nothing_else(
    root: Path, client: TestClient, embedder_stub
) -> None:
    from palinode.consolidation.ttl import archive_expired

    fixture.write_store(root, expires_at="2020-01-01T00:00:00Z")

    reached = _stems(archive_expired(datetime(2026, 1, 1, tzinfo=UTC), dry_run=True)["archived"])

    assert reached == fixture.TTL, (
        f"extra: {sorted(reached - fixture.TTL)}, missing: {sorted(fixture.TTL - reached)}"
    )


# ── Cross references ─────────────────────────────────────────────────────────


_SPEC_HOLES = ("specs/.drafts/d", "specs/logs/l")


def _write_spec_holes(root: Path) -> None:
    for stem in _SPEC_HOLES:
        path = root / f"{stem}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nid: {stem.replace('/', '-')}\ntype: Decision\n---\n\nfixture body harbor {stem}\n")


def test_a_cross_ref_target_is_a_curated_current_memory_or_a_hand_written_spec(root: Path) -> None:
    from palinode.core.cross_refs import build_registry

    fixture.write_store(root)
    _write_spec_holes(root)

    reached = _stems(f"{ref}.md" for ref in build_registry(str(root)))

    assert reached == fixture.CROSS_REFS, (
        f"extra: {sorted(reached - fixture.CROSS_REFS)}, missing: {sorted(fixture.CROSS_REFS - reached)}"
    )


def test_a_cross_ref_source_is_a_curated_current_memory_or_a_hand_written_spec(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from palinode.core.cross_refs import update_file_cross_refs

    monkeypatch.setattr(config.capture.cross_refs, "enabled", True)
    fixture.write_store(root)
    _write_spec_holes(root)
    stems = (*fixture.FILES, *_SPEC_HOLES)
    body = "See " + " ".join(stems) + "."

    reached = {
        stem
        for stem in stems
        if update_file_cross_refs(str(root / f"{stem}.md"), content=body)["refs"]
    }

    assert reached == fixture.CROSS_REFS, (
        f"extra: {sorted(reached - fixture.CROSS_REFS)}, missing: {sorted(fixture.CROSS_REFS - reached)}"
    )


# ── The rest of the index group: records, counts and dedup ───────────────────


def _forget_withdrawal(root: Path, client: TestClient) -> set[str]:
    from palinode.consolidation.forget import withdraw_forget_request

    fixture.write_store(root, status="archived", superseded_by="requests/r.md")
    request = root / "requests" / "r.md"
    request.parent.mkdir()
    request.write_text(
        "---\nid: requests-r\ntype: Decision\ncategory: requests\n---\n\n"
        "Please forget that I collect sneakers.\n"
    )
    plan = withdraw_forget_request("requests/r.md", dry_run=True)
    return _stems(row["file"] for row in plan["would_restore"])


def _handoff_export(root: Path, client: TestClient) -> set[str]:
    from palinode.core import aliases, handoff

    aliases.reset_cache()
    fixture.write_store(root)
    plan = handoff.plan_export("harbor", types=["Decision"])
    return _stems(
        [r["origin_path"] for r in plan["records"]] + [r["path"] for r in plan["exclusions"]]
    )


def _handoff_import(root: Path, client: TestClient) -> set[str]:
    from palinode.core import aliases, handoff_import

    aliases.reset_cache()
    fixture.write_store(root, entities=lambda stem: [f"project/at-{stem.replace('/', '-')}"])
    _, projects = handoff_import._store_view(str(root))
    return {
        stem for stem in fixture.FILES
        if f"at-{stem.replace('/', '-')}" in projects
    }


def _correction_blast_radius(root: Path, client: TestClient) -> set[str]:
    from palinode.corrections.review import referencing_records

    fixture.write_store(root, supersedes=["people/target"])
    target = root / "people" / "target.md"
    target.parent.mkdir()
    target.write_text("---\nid: people-target\ntype: Decision\ncategory: people\n---\n\nTarget.\n")
    return _stems(row["file"] for row in referencing_records("people/target.md"))


def _openclaw_dedup(root: Path, client: TestClient) -> set[str]:
    from palinode.migration.openclaw import run_sections_migration

    fixture.write_store(root)
    sections = [
        {
            "heading": stem,
            "body": f"fixture body harbor {stem}",
            "type": "decision",
            "slug": stem.replace("/", "-").replace(".", ""),
        }
        for stem in fixture.FILES
    ]
    source = root.parent / "MEMORY.md"
    source.write_text("## x\n")
    report = run_sections_migration(
        str(source), sections, source_name="openclaw", display_name="OpenClaw", dry_run=True
    )
    skipped = {os.path.basename(rel)[:-3] for rel in report["files_skipped"]}
    return {s for s in fixture.FILES if s.replace("/", "-").replace(".", "") in skipped}


INDEX_RECORD_SURFACES: dict[str, Callable[..., set[str]]] = {
    "forget withdrawal": _forget_withdrawal,
    "handoff export": _handoff_export,
    "handoff import": _handoff_import,
    "correction blast radius": _correction_blast_radius,
    "openclaw dedup": _openclaw_dedup,
}


@pytest.mark.parametrize("surface", INDEX_RECORD_SURFACES)
def test_a_record_walk_reaches_exactly_the_memory_files(
    surface: str, root: Path, client: TestClient, embedder_stub
) -> None:
    reached = INDEX_RECORD_SURFACES[surface](root, client)

    assert reached == fixture.INDEX, (
        f"extra: {sorted(reached - fixture.INDEX)}, missing: {sorted(fixture.INDEX - reached)}"
    )


# ── Counts: every number that says "how many memory files" ───────────────────


def test_retrieval_stats_counts_only_memory_files(root: Path) -> None:
    import json

    from click.testing import CliRunner

    from palinode.cli.retrieval_stats import retrieval_stats

    fixture.write_store(root)
    log = root / ".audit" / "retrievals.jsonl"
    log.parent.mkdir()
    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "file_path": str(root / "notes" / "a.md"),
        "mode": "explicit",
    }
    log.write_text(json.dumps(event) + "\n")

    result = CliRunner().invoke(retrieval_stats, ["--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["distribution"]["total_md_files"] == len(fixture.INDEX)


def test_the_first_run_check_counts_only_memory_files(root: Path) -> None:
    fixture.write_store(root)

    with pytest.raises(RuntimeError, match=rf"found {len(fixture.INDEX)} memory file\(s\)"):
        store._ensure_db()


def test_the_config_banner_counts_only_memory_files(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from palinode.core.config import load_config

    fixture.write_store(root)

    load_config()

    assert f"({len(fixture.INDEX)} files," in capsys.readouterr().err


def _doctor_context() -> DoctorContext:
    return DoctorContext(config=config)


def test_the_doctor_chunk_ratio_counts_only_memory_files(
    root: Path, client: TestClient, embedder_stub
) -> None:
    from palinode.diagnostics.checks.index_size import chunks_match_md_count

    fixture.write_store(root)
    assert client.post("/reindex").status_code == 200

    result = chunks_match_md_count(_doctor_context())

    assert f"for {len(fixture.INDEX)} markdown files" in result.message


def test_the_doctor_status_check_counts_only_memory_files(root: Path, client: TestClient) -> None:
    from palinode.diagnostics.checks.service import api_status_consistent

    fixture.write_store(root)
    status = mock.Mock(status_code=200)
    status.json.return_value = {"total_chunks": 0}

    with mock.patch("palinode.diagnostics.checks.service.httpx.get", return_value=status):
        result = api_status_consistent(_doctor_context())

    assert f"memory_dir has {len(fixture.INDEX)} markdown file(s)" in result.message


# ── Scenarios across surfaces ────────────────────────────────────────────────


def _save_through_the_watcher(path: Path) -> None:
    from palinode.indexer.watcher import PalinodeHandler

    handler = PalinodeHandler()
    handler._schedule_index = lambda p, **kw: handler._process_file(p)  # type: ignore[method-assign]
    try:
        handler.on_created(FileCreatedEvent(str(path)))
    finally:
        handler.shutdown()


def test_a_file_saved_in_a_dot_folder_is_not_indexed_and_a_reindex_changes_nothing(
    root: Path, client: TestClient, embedder_stub
) -> None:
    _write_one_note(root)
    trash = root / ".trash" / "t.md"
    trash.parent.mkdir()
    trash.write_text((root / "notes" / "a.md").read_text())
    for path in (trash, root / "notes" / "a.md"):
        _save_through_the_watcher(path)
    before = _indexed_stems()

    assert client.post("/reindex").status_code == 200

    assert before == {"notes/a"}
    assert _indexed_stems() == before


def _store_under(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *above: str) -> Path:
    memory_dir = tmp_path.joinpath(*above, "store")
    memory_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    monkeypatch.setattr(config, "db_path", str(memory_dir / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    monkeypatch.setattr(store, "_db_checked", False)
    return memory_dir


def _write_one_note(memory_dir: Path) -> None:
    note = memory_dir / "notes" / "a.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\nid: notes-a\ntype: Decision\ncategory: notes\n---\n\nfixture body harbor\n")


_ABOVE_STORE = [
    "venv", "node_modules", "__pycache__", "palinode.egg-info", "mypkg.egg-info", "logs",
    ".local", ".palinode",
]


@pytest.mark.parametrize("above_store", _ABOVE_STORE)
def test_a_store_under_build_noise_or_logs_still_indexes_through_the_watcher(
    above_store: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_dir = _store_under(tmp_path, monkeypatch, above_store)
    _write_one_note(memory_dir)

    assert _watcher_events(memory_dir, write=False) == {"notes/a"}


@pytest.mark.parametrize("above_store", _ABOVE_STORE)
def test_a_store_under_build_noise_or_logs_still_indexes_through_reindex(
    above_store: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder_stub
) -> None:
    memory_dir = _store_under(tmp_path, monkeypatch, above_store)

    with TestClient(app, raise_server_exceptions=False) as client:
        _write_one_note(memory_dir)
        assert client.post("/reindex").status_code == 200

    assert _indexed_stems() == {"notes/a"}


def test_listing_with_include_inbox_adds_inbox_and_not_processed_items(
    root: Path, client: TestClient
) -> None:
    fixture.write_store(root)

    response = client.get("/list", params={"include_inbox": "true"})

    reached = _stems(row["file"] for row in response.json())
    expected = fixture.LISTING | {"inbox/i"}
    assert reached == expected, (
        f"extra: {sorted(reached - expected)}, missing: {sorted(expected - reached)}"
    )


def test_a_history_sibling_is_still_listed_by_the_api_hidden_by_the_ui_and_indexed(
    root: Path, client: TestClient, embedder_stub
) -> None:
    from palinode.api.ui.views import scan_memory_files
    from palinode.consolidation.propagate import find_dependents

    fixture.write_store(root, backed_by=["sources/s"])
    (root / "notes" / "a-history.md").write_text(
        "---\nid: notes-a-history\nbacked_by: [sources/s]\n---\n\n# History\n\nfixture body harbor\n"
    )

    listed = _stems(row["file"] for row in client.get("/list").json())
    browsed = _stems(row["path"] for row in scan_memory_files())
    dependents = _stems(find_dependents(["sources/s"], str(root)))
    assert client.post("/reindex").status_code == 200

    assert "notes/a-history" in listed
    assert "notes/a-history" not in browsed
    assert "notes/a-history" not in dependents
    assert "notes/a-history" in _indexed_stems()


# ── The dependency graph and the correction target lookup ────────────────────


def _slug_of(stem: str) -> str:
    return "fx-" + stem.replace("/", "-").replace(".", "")


def test_the_dependency_graph_reads_only_memory_files(root: Path) -> None:
    from palinode.core.depends import find_unblocked

    fixture.write_store(root, slug=_slug_of, status="active")

    reached = _stems(row["file_path"] for row in find_unblocked(str(root)))

    assert reached == fixture.INDEX, (
        f"extra: {sorted(reached - fixture.INDEX)}, missing: {sorted(fixture.INDEX - reached)}"
    )


def test_a_dependency_on_a_never_memory_file_is_an_orphan(root: Path) -> None:
    from palinode.core.depends import traverse_depends

    fixture.write_store(
        root,
        slug=_slug_of,
        status="active",
        depends_on=lambda stem: [_slug_of("prompts/p")] if stem == "notes/a" else None,
    )

    neighbourhood = traverse_depends(_slug_of("notes/a"), str(root))

    assert neighbourhood["depends_on"] == [
        {"slug": _slug_of("prompts/p"), "status": None, "found": False}
    ]
    assert neighbourhood["orphans"] == [_slug_of("prompts/p")]


def _write_note(root: Path, stem: str) -> None:
    path = root / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: {stem.replace('/', '-')}\ntype: Decision\n---\n\nbody {stem}\n")


def test_a_bare_name_shared_with_never_memory_files_resolves_to_the_memory(root: Path) -> None:
    from palinode.corrections.review import resolve_target

    for stem in ("notes/x", "prompts/x", "specs/prompts/x", "node_modules/pkg/x", "inbox/processed/x"):
        _write_note(root, stem)

    assert resolve_target("x") == ("notes/x.md", str(root / "notes" / "x.md"))


def test_a_bare_name_that_only_a_never_memory_file_carries_is_not_found(root: Path) -> None:
    from palinode.corrections.review import TargetNotFoundError, resolve_target

    _write_note(root, "prompts/only-here")
    _write_note(root, "node_modules/pkg/only-there")

    for name in ("only-here", "only-there"):
        with pytest.raises(TargetNotFoundError):
            resolve_target(name)
