"""The erasure runbook, executed step by step on a throwaway fixture.

``docs/DATA-LIFECYCLE.md`` describes a seven-step procedure for getting a
subject's data out of a git-backed store. This file *runs* it, on a store it
builds and destroys inside ``tmp_path``, and records for each location whether
removal is **automatic**, **manual** or **unsupported**, and how absence is
checked. The inventory the documentation carries is generated from the same
constants this module asserts against, so the two cannot drift silently.

The probe is a sentinel: one nonsense token planted in every place a memory's
text can come to rest, and grepped for afterwards across the entire fixture
tree — loose objects, packfiles and commit messages included, via
``git cat-file --batch-all-objects`` rather than a byte scan, because a packed
blob is compressed and a naive ``grep`` of ``.git`` would report a comforting
false negative.

SAFETY. Every destructive call in this file goes through :func:`_inside`,
which refuses any path that is not under the test's own ``tmp_path``. Nothing
here touches a configured store, a real remote, or this repository. The
"controlled clone" is a second bare repository created beside the fixture.

Scope note: this is engineering evidence about what the supported operations
do and do not reach. It is not a compliance claim, and nothing here asserts
that any procedure satisfies any regulation.
"""
from __future__ import annotations

import glob
import hashlib
import importlib
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from palinode.core.config import config

EMBED_DIM = 1024

#: The probe. Nonsense on purpose: it cannot occur in the codebase, in git's
#: own metadata, or in any library's output, so one hit anywhere is one
#: retained copy of the erased text.
SENTINEL = "Brambleshot-Verity-7Q4-quartzline"

#: A *revealing identifier* that is not the text: the subject's slug, which
#: appears in file paths, commit subjects and history-sibling names. Erasing
#: the content without erasing this still tells a reader who the record was
#: about, which is why the runbook has to reach commit messages too.
SUBJECT_SLUG = "brambleshot-verity"

SUBJECT_FILE = f"people/{SUBJECT_SLUG}.md"
QUOTING_FILE = "insights/ledger-readout.md"
NEIGHBOUR_FILE = "insights/pavilion-cadence.md"

SUBJECT_TEXT = (
    f"Verity Brambleshot's standing settlement instruction is {SENTINEL}, "
    "used for every quarry reconciliation."
)
QUOTING_TEXT = (
    "Ledger readout for the finance track. The standing instruction was "
    f"confirmed verbatim: “{SUBJECT_TEXT}” Everything else in this "
    "note is unrelated: pavilion rosters, pallet counts, and the volunteer "
    "board refresh."
)
NEIGHBOUR_TEXT = (
    "Pavilion staffing rosters are reviewed every fortnight and published to "
    "the volunteer board."
)


def _hash_embed(text: str, backend: str = "local") -> list[float]:
    vec = [0.0] * EMBED_DIM
    for tok in set(text.lower().split()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ── safety ───────────────────────────────────────────────────────────────────


def _inside(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    """Return ``path`` iff it is inside ``root``; raise otherwise.

    Every ``rm``, every history rewrite and every log truncation in this file
    is wrapped in this. A refactor that accidentally points one of them at a
    configured store fails here rather than deleting something.
    """
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    if os.path.commonpath([real_root, real_path]) != real_root:
        raise AssertionError(
            f"refusing to operate on {real_path!r}: outside the fixture root "
            f"{real_root!r}"
        )
    return str(path)


def _git(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    _inside(root, root)
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "FILTER_BRANCH_SQUELCH_WARNING": "1"},
    )


#: Every module that holds the API's retrieval logger *by value*
#: (``from palinode.api._util import _retrieval_logger``). Patching only
#: ``_util`` would leave each of these pointing at the original object.
_RETRIEVAL_LOGGER_HOLDERS = (
    "palinode.api._util",
    "palinode.api.server",
    "palinode.api.routers.memory",
    "palinode.api.routers.search",
    "palinode.api.routers.git_history",
)


def _redirect_retrieval_logger(monkeypatch, store_dir: str) -> None:
    """Point the API's retrieval log inside the fixture, and prove it.

    ``palinode.api._util._retrieval_logger`` is constructed **at import time**
    from the process config, so it binds to whichever store was configured when
    the first test module imported the API — for a full-suite run that is the
    developer's real store, because collection imports modules before any
    fixture patches ``memory_dir``. Patching ``config.memory_dir`` afterwards
    does not move it. (Same class as the ``db_path`` hazard
    ``conftest._isolate_db_path`` exists for; tracked separately.)

    That matters more here than anywhere else in the suite: this module's whole
    method is to plant a sentinel standing for erased text and then prove no
    copy survived. Sending that sentinel through ``POST /search`` writes it to
    the retrieval log's ``query`` field — and without this redirection, into a
    log outside the fixture, which is both a leak and a false negative (the
    sweep would never look there).

    The assertion is the point, not the patch: if a new holder of the name
    appears, this fails rather than quietly leaking again.
    """
    import importlib

    from palinode.core.retrieval_log import RetrievalLogger

    logger = RetrievalLogger(store_dir)
    _inside(logger.log_path, store_dir)
    for name in _RETRIEVAL_LOGGER_HOLDERS:
        module = importlib.import_module(name)
        if hasattr(module, "_retrieval_logger"):
            monkeypatch.setattr(module, "_retrieval_logger", logger)

    from palinode.api.routers import search as search_router

    assert search_router._retrieval_logger.log_path is not None
    _inside(search_router._retrieval_logger.log_path, store_dir)


def _quiesce_watchers() -> None:
    """Stop every armed watcher debounce timer and join its thread.

    ``POST /reindex`` constructs a ``PalinodeHandler``, which arms
    ``threading.Timer``s for the index pass, the summary pass and the
    deferred-description retry. Those fire *later*, on their own threads,
    against whatever ``config.memory_dir`` points at by then — so a timer armed
    by one test can be writing frontmatter into this test's store while
    ``git filter-branch`` is walking it, and filter-branch refuses to rewrite a
    dirty tree. The failure that produces is intermittent and reads like a git
    problem, which is the worst kind.

    ``shutdown_handlers`` is the watcher module's own idempotent stop (the same
    call ``conftest.pytest_sessionfinish`` makes at session end). Called before
    anything that needs a quiet tree, and again in this module's fixture
    teardown so these tests cannot leak a timer into somebody else's.
    """
    from palinode.indexer import watcher

    watcher.shutdown_handlers()


# ── the sentinel sweep ───────────────────────────────────────────────────────


def _working_tree_hits(root: str) -> list[str]:
    """Files under ``root`` (outside ``.git``) whose bytes contain the sentinel."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                blob = Path(full).read_bytes()
            except OSError:
                continue
            if SENTINEL.encode() in blob:
                hits.append(os.path.relpath(full, root))
    return sorted(hits)


def _git_object_hits(root: str) -> list[str]:
    """Object ids in ``root``'s repository — reachable or not — holding the sentinel.

    ``--batch-all-objects`` walks loose *and* packed objects, so a compressed
    blob is decompressed before the match instead of hiding from a byte grep.

    Works on bare repositories too — a bare repo has no ``.git`` directory,
    and a sweep that quietly returned "clean" for the one copy an operator is
    most likely to forget would be the worst possible false negative.
    """
    if not (
        os.path.isdir(os.path.join(root, ".git"))
        or os.path.isdir(os.path.join(root, "objects"))
    ):
        return []
    listing = _git(root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    hits = []
    for line in listing.stdout.splitlines():
        if not line.strip():
            continue
        oid, otype = line.split()
        if otype not in ("blob", "commit", "tag"):
            continue
        body = subprocess.run(
            ["git", "-C", root, "cat-file", otype, oid],
            capture_output=True, check=True,
        ).stdout
        if SENTINEL.encode() in body:
            hits.append(f"{otype} {oid}")
    return hits


def _commit_message_hits(root: str, needle: str) -> list[str]:
    log = _git(root, "log", "--all", "--format=%H %s%n%b")
    return [line for line in log.stdout.splitlines() if needle in line]


#: The index and everything SQLite keeps beside it. The write-ahead log and the
#: shared-memory sidecar hold recently-written page images, so a row that has
#: been deleted can still be readable in ``-wal`` — and a freed page inside the
#: main file keeps its old bytes until SQLite reuses it either way. This is why
#: the index is *rebuilt* automatically but only *deletion* makes the old bytes
#: go, and why the absence check here is a byte grep rather than a search.
_DB_GLOB = ".palinode.db*"


def _database_file_hits(root: str) -> list[str]:
    """Database files under ``root`` whose raw bytes contain the sentinel.

    Deliberately separate from :func:`_working_tree_hits`, which would cover
    them incidentally: these are the one location a caller is tempted to check
    with a *query* instead of a grep, and a query cannot see a freed page or a
    ``-wal`` frame.
    """
    return sorted(
        os.path.relpath(p, root)
        for p in glob.glob(os.path.join(root, _DB_GLOB))
        if SENTINEL.encode() in Path(p).read_bytes()
    )


def _delete_database(store_dir: str) -> list[str]:
    """Runbook step 6: remove the index *and* its sidecars, guarded.

    ``.palinode.db`` alone is not the index: dropping it while ``-wal`` and
    ``-shm`` survive leaves page images of the erased rows on disk beside a
    freshly rebuilt database that reports them gone.
    """
    removed = []
    for path in sorted(glob.glob(os.path.join(store_dir, _DB_GLOB))):
        os.remove(_inside(path, store_dir))
        removed.append(os.path.relpath(path, store_dir))
    return removed


def _sentinel_everywhere(root: str) -> dict[str, list[str]]:
    """The whole-fixture sweep, split by where the hits are."""
    return {
        "working_tree": _working_tree_hits(root),
        "database_files": _database_file_hits(root),
        "git_objects": _git_object_hits(root),
        "commit_messages": _commit_message_hits(root, SENTINEL),
    }


# ── fixture ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def erasure_fixture(tmp_path, monkeypatch):
    """A disposable store seeded into every location a memory's text reaches.

    Returns ``(client, paths)`` where ``paths`` names the store, a bare
    "controlled remote", a clone of it, and a filesystem backup taken before
    any erasure. All four live under ``tmp_path``.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(store_dir)], check=True)
    _git(str(store_dir), "config", "user.email", "t@t.test")
    _git(str(store_dir), "config", "user.name", "test")
    # Palinode itself only ever commits the files a mutation named, so the
    # operational directories are untracked in a real store. The runbook's
    # `git add -A` is an *operator's* command, though, and without this they
    # would be swept into history — which is the one way the logs stop being
    # a manual purge and become a history rewrite. Pinned in the fixture so
    # the runbook under test is the one the documentation describes.
    (store_dir / ".gitignore").write_text(
        ".palinode.db*\n.palinode/\n.audit/\n", encoding="utf-8"
    )

    monkeypatch.setattr(config, "memory_dir", str(store_dir))
    monkeypatch.setattr(config, "db_path", str(store_dir / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    for key in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)

    import palinode.api.server as srv

    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    _redirect_retrieval_logger(monkeypatch, str(store_dir))
    from fastapi.testclient import TestClient

    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", side_effect=_hash_embed),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as client:
            _seed(client, str(store_dir))

            bare = tmp_path / "controlled-remote.git"
            subprocess.run(
                ["git", "clone", "--quiet", "--bare", str(store_dir), str(bare)],
                check=True,
            )
            clone = tmp_path / "controlled-clone"
            subprocess.run(
                ["git", "clone", "--quiet", str(bare), str(clone)], check=True
            )
            _git(str(store_dir), "remote", "add", "origin", str(bare))

            backup = tmp_path / "backup-before-erasure"
            shutil.copytree(store_dir, backup)

            try:
                yield client, {
                    "root": str(tmp_path),
                    "store": str(store_dir),
                    "remote": str(bare),
                    "clone": str(clone),
                    "backup": str(backup),
                }
            finally:
                # A `/reindex` in the test body armed watcher timers that would
                # otherwise fire during a later test, against a `memory_dir`
                # that has moved on. Stop them while this store still exists.
                _quiesce_watchers()
    srv._rate_counters.clear()


def _save(client, content: str, mtype: str, slug: str, **extra: Any) -> dict:
    r = client.post(
        "/save", json={"content": content, "type": mtype, "slug": slug, **extra}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _seed(client, store_dir: str) -> None:
    """Put the sentinel in every location the runbook has to sweep."""
    # 1 + 2. The subject's own memory and a second memory quoting it verbatim.
    _save(client, SUBJECT_TEXT, "PersonMemory", SUBJECT_SLUG)
    _save(
        client,
        QUOTING_TEXT,
        "Insight",
        "ledger-readout",
        sources=[{"ref": f"people/{SUBJECT_SLUG}", "quote": SUBJECT_TEXT}],
    )
    _save(client, NEIGHBOUR_TEXT, "Insight", "pavilion-cadence")

    # 3. Git history depth: a second version of the subject's memory, so the
    #    old text survives the file's own later edits.
    _save(
        client,
        f"{SUBJECT_TEXT} Reconfirmed at the fortnightly review.",
        "PersonMemory",
        SUBJECT_SLUG,
    )

    # 4. A retirement whose *reason* restates the text, landing it in the
    #    history sibling and the commit subject. This is the shape the forget
    #    path produces (`reason=f'forget request: "{pref}"'`).
    r = client.post(
        "/archive",
        json={"file_path": SUBJECT_FILE, "reason": f"subject asked: {SENTINEL}"},
    )
    assert r.status_code == 200, r.text

    # 5. The retrieval log's `query` field. Constructed directly against the
    #    fixture rather than driven through the endpoint: `_retrieval_logger`
    #    binds its path at *import* time from the process config, so the
    #    endpoint's own writes do not follow a later `memory_dir` patch.
    from palinode.core.retrieval_log import RetrievalLogger

    retrievals = RetrievalLogger(store_dir)
    _inside(retrievals.log_path, store_dir)
    retrievals.record_search_results(
        [], query=f"what is {SENTINEL}", source="palinode_search", mode="explicit"
    )

    # 6. The MCP audit log's truncated argument capture. The config's own
    #    `audit.log_path` is *absolutised at load*, exactly like `db_path`, so
    #    handing this logger `config.audit` would write to whatever store the
    #    process was configured for rather than to the fixture. Build the
    #    relative default explicitly and assert where it landed.
    from palinode.core.audit import AuditLogger
    from palinode.core.config import AuditConfig

    audit = AuditLogger(
        store_dir, AuditConfig(enabled=True, log_path=".audit/mcp-calls.jsonl")
    )
    _inside(audit.log_path, store_dir)
    audit.log_call(
        tool_name="palinode_save",
        arguments={"content": SUBJECT_TEXT},
        duration_ms=1.0,
        status="ok",
    )

    # 7. The correction-candidate queue's quoted span.
    from palinode.corrections.queue import (
        CorrectionCandidate,
        append_candidates,
        utc_now_iso,
    )

    append_candidates(
        [
            CorrectionCandidate(
                harness="claude-code",
                session_id="s-1459",
                turn_index=3,
                turn_uuid=None,
                span=f"no, it is {SENTINEL}",
                span_hash="deadbeef",
                grep_family="correction",
                matched_rules=("no,",),
                classification="needs_review",
                classifier={"by": "rules"},
                window_turns=(2, 4),
                occurred_at=utc_now_iso(),
                detected_at=utc_now_iso(),
                project=None,
            )
        ],
        store_dir,
    )


# ── the inventory the documentation carries ──────────────────────────────────
#
# location → (removed_by, how absence is checked). Asserted below against the
# fixture, and mirrored in docs/DATA-LIFECYCLE.md.

MANUAL = "manual"
UNSUPPORTED = "unsupported"

#: The index only. Its *contents* need no per-record sweep — a rebuild from
#: clean files produces clean rows — but the old bytes go when the files are
#: deleted, not when the rows are. Deliberately not spelled ``automatic``: that
#: word is what makes a reader skip the deletion and check with a search.
AUTOMATIC_REBUILD = "automatic to rebuild, manual to remove"

EXPECTED_INVENTORY: dict[str, str] = {
    "markdown source file": MANUAL,
    "quoted copy in another memory": MANUAL,
    "history sibling (-history.md)": MANUAL,
    "git object history": MANUAL,
    "git commit messages": MANUAL,
    "SQLite chunks + FTS + vec (.palinode.db and -wal/-shm)": AUTOMATIC_REBUILD,
    "retrieval log (.audit/retrievals.jsonl)": MANUAL,
    "MCP audit log (.audit/mcp-calls.jsonl)": MANUAL,
    "correction queue (.palinode/correction-candidates.jsonl)": MANUAL,
    "controlled remote / clone": MANUAL,
    "filesystem backup": MANUAL,
    "uncontrolled clone or backup": UNSUPPORTED,
}


# ═════════════════════════════════════════════════════════════════════════════
# What `rm` alone actually achieves — the honest starting point
# ═════════════════════════════════════════════════════════════════════════════


def test_deleting_the_file_leaves_the_text_in_nine_other_places(erasure_fixture):
    """Step 2 on its own is not erasure, and this is the list of what is left."""
    client, paths = erasure_fixture
    store_dir = paths["store"]

    os.remove(_inside(os.path.join(store_dir, SUBJECT_FILE), store_dir))

    remaining = _working_tree_hits(store_dir)
    # The quoting memory, the history sibling written by the archive, and all
    # three operational logs still restate the text.
    assert QUOTING_FILE in remaining
    assert f"people/{SUBJECT_SLUG}-history.md" in remaining
    assert ".audit/retrievals.jsonl" in remaining
    assert ".audit/mcp-calls.jsonl" in remaining
    assert ".palinode/correction-candidates.jsonl" in remaining
    # …and every past version is still in the repository, with the subject's
    # slug in every commit subject that ever touched the file.
    assert _git_object_hits(store_dir)
    assert _commit_message_hits(store_dir, SUBJECT_SLUG)
    # …and in the controlled remote, the clone, and the backup.
    assert _git_object_hits(paths["clone"])
    assert _working_tree_hits(paths["backup"])


def test_the_index_is_the_one_location_rebuilt_automatically(erasure_fixture):
    """SQLite/FTS/vec is derived: it follows the files with no manual step.

    "Rebuilt automatically" and "the old bytes are gone" are two claims, and
    only the first one is free. A rebuild removes the *rows*; the bytes go when
    the database file and its sidecars are deleted, which is why this test
    checks both — a query for the sentinel, and a grep of the raw files.
    """
    client, paths = erasure_fixture
    store_dir = paths["store"]

    from palinode.core import store as store_mod

    def _indexed() -> int:
        db = store_mod.get_db()
        try:
            return db.execute(
                "SELECT COUNT(*) FROM chunks WHERE content LIKE ?",
                (f"%{SENTINEL}%",),
            ).fetchone()[0]
        finally:
            db.close()

    assert _indexed() > 0

    os.remove(_inside(os.path.join(store_dir, SUBJECT_FILE), store_dir))
    os.remove(
        _inside(os.path.join(store_dir, f"people/{SUBJECT_SLUG}-history.md"), store_dir)
    )
    _redact_quoting_file(store_dir)

    # The main file is not the whole index: delete the sidecars with it.
    removed = _delete_database(store_dir)
    assert ".palinode.db" in removed
    store_mod.init_db()
    assert client.post("/reindex").status_code == 200

    assert _indexed() == 0, "the rebuilt index still carries the erased text"
    # FTS is rebuilt in lockstep, not left holding the terms.
    db = store_mod.get_db()
    try:
        fts = db.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
            (SENTINEL.replace("-", " "),),
        ).fetchone()[0]
    finally:
        db.close()
    assert fts == 0
    # And the check that a query cannot make: no database file — main, `-wal`
    # or `-shm` — still holds the bytes.
    assert _database_file_hits(store_dir) == []


def test_deleting_the_index_has_to_cover_its_write_ahead_sidecars(erasure_fixture):
    """Why step 6 says "and its ``-wal`` / ``-shm``", demonstrated.

    The store runs SQLite in WAL mode, so a committed row lives in
    ``.palinode.db-wal`` until a checkpoint folds it into the main file. A
    clean close checkpoints and removes the sidecars, which is why they are
    usually invisible — but a *running* daemon holds the connection open, and
    anything that captures the directory at that moment (a backup, an rsync, a
    crash) freezes all three files together.

    This test takes exactly that snapshot and shows the consequence: the
    sentinel is in the sidecar, removing only ``.palinode.db`` leaves it there,
    and no query could have told you — the rows it would read are gone.
    """
    client, paths = erasure_fixture
    store_dir = paths["store"]

    from palinode.core import store as store_mod

    # Hold a connection open with a freshly committed row, so the WAL has
    # frames that have not been checkpointed into the main database yet.
    db = store_mod.get_db()
    snapshot = os.path.join(_inside(paths["root"], paths["root"]), "live-snapshot")
    try:
        db.execute(
            "INSERT INTO chunks (id, file_path, section_id, content, category) "
            "VALUES (?, ?, ?, ?, ?)",
            ("wal-probe", SUBJECT_FILE, "root", SUBJECT_TEXT, "people"),
        )
        db.commit()
        assert os.path.exists(os.path.join(store_dir, ".palinode.db-wal"))
        # Capture the directory mid-flight, exactly as a backup would.
        shutil.copytree(store_dir, snapshot)
    finally:
        db.close()

    sidecar = os.path.join(snapshot, ".palinode.db-wal")
    assert os.path.exists(sidecar)
    assert SENTINEL.encode() in Path(sidecar).read_bytes(), (
        "the fixture failed to leave an un-checkpointed row in the WAL, so "
        "this test would prove nothing"
    )

    # Removing only the main file: the sidecar still holds the bytes.
    os.remove(_inside(os.path.join(snapshot, ".palinode.db"), snapshot))
    assert _database_file_hits(snapshot) == [".palinode.db-wal"]

    # The step as documented removes every one of them.
    removed = _delete_database(snapshot)
    assert ".palinode.db-wal" in removed
    assert _database_file_hits(snapshot) == []
    assert glob.glob(os.path.join(snapshot, _DB_GLOB)) == []


def _redact_quoting_file(store_dir: str) -> None:
    """In-place redaction of the memory that merely *quotes* the subject."""
    target = Path(_inside(os.path.join(store_dir, QUOTING_FILE), store_dir))
    target.write_text(
        target.read_text(encoding="utf-8").replace(SENTINEL, "[erased]"),
        encoding="utf-8",
    )


# ═════════════════════════════════════════════════════════════════════════════
# The runbook, start to finish
# ═════════════════════════════════════════════════════════════════════════════


def _rewrite_history(store_dir: str) -> None:
    """Runbook step 4: remove the paths *and* the text *and* the identifiers.

    ``git-filter-repo`` is the documented tool and is not a stock-git command,
    so the fixture uses ``filter-branch``, which ships with git everywhere and
    has the same three knobs this step needs: a tree filter for the blobs, a
    message filter for the subjects, and ``--all`` for every ref.
    """
    _inside(store_dir, store_dir)
    # Nothing may be writing into the tree while it is being rewritten.
    _quiesce_watchers()
    dirty = _git(store_dir, "status", "--porcelain").stdout.strip()
    assert not dirty, (
        "the working tree is dirty going into the history rewrite, which "
        f"filter-branch refuses — a leaked writer, not a git problem:\n{dirty}"
    )
    tree_filter = (
        f"rm -f 'people/{SUBJECT_SLUG}.md' 'people/{SUBJECT_SLUG}-history.md'; "
        f"if [ -f '{QUOTING_FILE}' ]; then "
        f"sed -i.bak 's/{SENTINEL}/[erased]/g' '{QUOTING_FILE}' && "
        f"rm -f '{QUOTING_FILE}.bak'; fi"
    )
    msg_filter = f"sed 's/{SENTINEL}/[erased]/g; s/{SUBJECT_SLUG}/[erased]/g'"
    _git(
        store_dir,
        "filter-branch", "-f",
        "--tree-filter", tree_filter,
        "--msg-filter", msg_filter,
        "--", "--all",
    )
    # Unreachable objects survive a rewrite until the reflog and the original
    # refs let go of them — which is why "the rewrite ran" is not the check.
    shutil.rmtree(
        _inside(os.path.join(store_dir, ".git", "refs", "original"), store_dir),
        ignore_errors=True,
    )
    _git(store_dir, "reflog", "expire", "--expire=now", "--all")
    _git(store_dir, "gc", "--prune=now", "--quiet")


def _purge_logs(store_dir: str) -> list[str]:
    """Runbook step 7: rewrite the operational logs without the erased lines."""
    purged = []
    for rel in (
        ".audit/retrievals.jsonl",
        ".audit/mcp-calls.jsonl",
        ".palinode/correction-candidates.jsonl",
    ):
        path = Path(_inside(os.path.join(store_dir, rel), store_dir))
        if not path.exists():
            continue
        kept = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if SENTINEL not in line
        ]
        path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
        purged.append(rel)
    return purged


def test_the_full_runbook_leaves_no_copy_in_the_store(erasure_fixture):
    """Every step, in order, then the whole-tree sweep including ``.git``."""
    client, paths = erasure_fixture
    store_dir = paths["store"]

    # Step 1 — identify the blast radius. Search is the supported instrument;
    # it finds the subject's own memory and the memory that quotes it.
    found = client.post(
        "/search",
        json={"query": SENTINEL, "limit": 20, "threshold": 0.0, "hybrid": True},
    )
    assert found.status_code == 200, found.text
    radius = _working_tree_hits(store_dir)
    assert SUBJECT_FILE in radius and QUOTING_FILE in radius

    # Step 2 — delete whole-file, redact in-place.
    os.remove(_inside(os.path.join(store_dir, SUBJECT_FILE), store_dir))
    os.remove(
        _inside(os.path.join(store_dir, f"people/{SUBJECT_SLUG}-history.md"), store_dir)
    )
    _redact_quoting_file(store_dir)
    _git(store_dir, "add", "-A")
    _git(store_dir, "commit", "-q", "-m", "erasure: remove and redact")

    # Step 3 — the tombstone: the act, never the content.
    tombstone = Path(_inside(os.path.join(store_dir, "erasures.md"), store_dir))
    tombstone.write_text(
        "---\ntype: Insight\nstatus: active\n---\n\n"
        "# Erasure ledger\n\n- 2026-09-21 — one subject record and one quoted "
        "span removed on request REQ-118. Scope: 1 memory, 1 quoting memory, "
        "3 operational logs, git history, 1 controlled remote.\n",
        encoding="utf-8",
    )
    assert SENTINEL not in tombstone.read_text(encoding="utf-8")
    assert SUBJECT_SLUG not in tombstone.read_text(encoding="utf-8")
    _git(store_dir, "add", "-A")
    _git(store_dir, "commit", "-q", "-m", "erasure: record the act")

    # Step 4 — rewrite history, blobs and messages both.
    _rewrite_history(store_dir)

    # Step 5 — the remote. A force-push moves the refs and leaves every old
    # object behind: the remote holds them until *it* is pruned, so "we
    # force-pushed" is not an erasure claim about the remote. The instruction
    # that actually holds is to replace the repository.
    _git(store_dir, "push", "--force", "--all", "origin")
    assert _git_object_hits(paths["remote"]), (
        "the fixture expected a force-push to leave the old objects on the "
        "remote; if git changed that, the runbook step can be simplified"
    )
    shutil.rmtree(_inside(paths["remote"], paths["root"]))
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", store_dir, paths["remote"]], check=True
    )
    fresh_clone = os.path.join(_inside(paths["root"], paths["root"]), "re-clone")
    subprocess.run(
        ["git", "clone", "--quiet", paths["remote"], fresh_clone], check=True
    )
    shutil.rmtree(_inside(paths["clone"], paths["root"]))

    # Step 6 — delete the index *and its sidecars*, then rebuild from the
    # now-clean files. Deleting `.palinode.db` alone would leave `-wal` and
    # `-shm` page images of the erased rows beside a database that reports
    # them gone.
    from palinode.core import store as store_mod

    assert _delete_database(store_dir)
    store_mod.init_db()
    assert client.post("/reindex").status_code == 200

    # Step 7 — purge the logs and expire the backup.
    assert _purge_logs(store_dir)
    shutil.rmtree(_inside(paths["backup"], paths["root"]))

    # The sweep. Nothing in the working tree, nothing in any database file or
    # its sidecars, nothing in any git object (reachable or not), nothing in
    # any commit message.
    sweep = _sentinel_everywhere(store_dir)
    assert sweep == {
        "working_tree": [],
        "database_files": [],
        "git_objects": [],
        "commit_messages": [],
    }, sweep
    # Named separately as well, because "no sidecar survives holding it" is the
    # assertion a dict comparison makes easy to read past.
    assert _database_file_hits(store_dir) == []
    assert not [
        p for p in glob.glob(os.path.join(store_dir, _DB_GLOB))
        if p.endswith(("-wal", "-shm")) and SENTINEL.encode() in Path(p).read_bytes()
    ]
    # The revealing identifier is gone from the subjects too.
    assert _commit_message_hits(store_dir, SUBJECT_SLUG) == []
    # The replaced remote and the fresh clone are clean.
    assert _git_object_hits(paths["remote"]) == []
    assert _git_object_hits(fresh_clone) == []
    assert _working_tree_hits(fresh_clone) == []
    # The neighbour that was never in scope is untouched.
    assert os.path.exists(os.path.join(store_dir, NEIGHBOUR_FILE))


def test_a_path_only_rewrite_leaves_the_subject_in_commit_messages(erasure_fixture):
    """Why the runbook needs a message filter, demonstrated rather than asserted.

    Removing the *paths* from every commit is the step the documentation used
    to describe. It does not touch commit subjects — and Palinode writes the
    memory's path into every one of them ("palinode: archive
    people/<subject>.md"). A reader of the rewritten history still learns who
    the record was about, from a history with no trace of the record.

    The content is safe here for a reason worth keeping: the archive commit
    subject names the file and the successor but **not** the ``reason``, so a
    forget request's pref text never reaches a commit subject. It does reach
    the ``-history.md`` sibling, which is a file and therefore covered by the
    tree filter.
    """
    client, paths = erasure_fixture
    store_dir = paths["store"]

    os.remove(_inside(os.path.join(store_dir, SUBJECT_FILE), store_dir))
    os.remove(
        _inside(os.path.join(store_dir, f"people/{SUBJECT_SLUG}-history.md"), store_dir)
    )
    _redact_quoting_file(store_dir)
    _git(store_dir, "add", "-A")
    _git(store_dir, "commit", "-q", "-m", "erasure: remove and redact")

    _inside(store_dir, store_dir)
    _git(
        store_dir,
        "filter-branch", "-f",
        "--index-filter",
        f"git rm -r --cached --ignore-unmatch 'people/{SUBJECT_SLUG}.md' "
        f"'people/{SUBJECT_SLUG}-history.md'",
        "--", "--all",
    )

    assert _commit_message_hits(store_dir, SUBJECT_SLUG), (
        "expected the path-only rewrite to leave the subject slug in subjects"
    )
    # The content itself is not in a commit subject — that is the archive op's
    # doing, and it is worth pinning so a future message change cannot quietly
    # start putting retired text into history.
    assert _commit_message_hits(store_dir, SENTINEL) == []


def test_a_clone_you_do_not_control_keeps_everything(erasure_fixture):
    """The limit the documentation has to state: an uncontrolled copy is not reachable."""
    client, paths = erasure_fixture
    store_dir = paths["store"]

    # Somebody cloned before the request arrived and took the copy away.
    uncontrolled = os.path.join(
        _inside(paths["root"], paths["root"]), "uncontrolled-clone"
    )
    subprocess.run(
        ["git", "clone", "--quiet", paths["remote"], uncontrolled], check=True
    )

    os.remove(_inside(os.path.join(store_dir, SUBJECT_FILE), store_dir))
    os.remove(
        _inside(os.path.join(store_dir, f"people/{SUBJECT_SLUG}-history.md"), store_dir)
    )
    _redact_quoting_file(store_dir)
    _git(store_dir, "add", "-A")
    _git(store_dir, "commit", "-q", "-m", "erasure: remove and redact")
    _rewrite_history(store_dir)
    _git(store_dir, "push", "--force", "--all", "origin")
    shutil.rmtree(_inside(paths["remote"], paths["root"]))
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", store_dir, paths["remote"]], check=True
    )

    # The store and its replaced remote are clean; the clone that was never
    # re-pulled is not, and no Palinode operation can reach it.
    assert _git_object_hits(store_dir) == []
    assert _git_object_hits(paths["remote"]) == []
    assert _git_object_hits(uncontrolled), (
        "the fixture's uncontrolled clone was unexpectedly clean"
    )
    assert _working_tree_hits(uncontrolled)


def test_force_pushing_a_rewrite_does_not_clean_the_remote(erasure_fixture):
    """"We force-pushed" is not an erasure claim about the remote.

    A force-push moves refs. The objects the old refs pointed at stay in the
    remote's object database — reachable by hash, and copied wholesale into
    the next clone made over a local path, which hardlinks the object
    directory rather than negotiating a reachable set. The runbook therefore
    says *replace* the remote repository, not *force-push to* it.
    """
    client, paths = erasure_fixture
    store_dir = paths["store"]

    os.remove(_inside(os.path.join(store_dir, SUBJECT_FILE), store_dir))
    os.remove(
        _inside(os.path.join(store_dir, f"people/{SUBJECT_SLUG}-history.md"), store_dir)
    )
    _redact_quoting_file(store_dir)
    _git(store_dir, "add", "-A")
    _git(store_dir, "commit", "-q", "-m", "erasure: remove and redact")
    _rewrite_history(store_dir)
    assert _git_object_hits(store_dir) == []

    _git(store_dir, "push", "--force", "--all", "origin")
    assert _git_object_hits(paths["remote"]), (
        "force-push alone left the remote clean — if git now prunes on "
        "receive, the runbook's 'replace the remote' step can be relaxed"
    )

    # And a clone taken from that remote inherits them.
    inherited = os.path.join(_inside(paths["root"], paths["root"]), "inherited-clone")
    subprocess.run(
        ["git", "clone", "--quiet", paths["remote"], inherited], check=True
    )
    assert _git_object_hits(inherited)


# ═════════════════════════════════════════════════════════════════════════════
# Tombstones and diagnostics must not keep what was erased
# ═════════════════════════════════════════════════════════════════════════════


def test_retraction_markers_are_opaque(erasure_fixture):
    """The one tombstone Palinode writes automatically carries no erased text."""
    client, paths = erasure_fixture
    store_dir = paths["store"]

    from palinode.consolidation.retract import retract_mentions

    out = retract_mentions(QUOTING_FILE, f"my settlement instruction {SENTINEL}")
    assert out["status"] == "retracted"

    body = Path(os.path.join(store_dir, QUOTING_FILE)).read_text(encoding="utf-8")
    import re

    markers = re.findall(r"\[RETRACTED [^\]]*\]\.", body)
    assert markers, "nothing was struck, so the marker claim is untested"
    assert all(SENTINEL not in m for m in markers)
    assert all(SUBJECT_SLUG not in m for m in markers)


def test_doctor_diagnostics_do_not_echo_memory_text(erasure_fixture):
    """A diagnostics run over the store must not restate what it found."""
    client, paths = erasure_fixture

    r = client.get("/doctor")
    assert r.status_code == 200, r.text
    assert SENTINEL not in json.dumps(r.json())


# ═════════════════════════════════════════════════════════════════════════════
# Recovery, and what it is incompatible with
# ═════════════════════════════════════════════════════════════════════════════


def test_restoring_a_pre_erasure_backup_undoes_the_erasure(erasure_fixture):
    """Stated plainly because operators do this by reflex.

    A backup taken before an erasure *is* a copy of the erased data. Restoring
    it is not a recovery from a mistake; it is an undo of the erasure, and the
    two are the same filesystem operation. This is why step 7 says "expire the
    backups" and why the runbook is incompatible with any retention policy
    that keeps a pre-erasure snapshot.
    """
    client, paths = erasure_fixture
    store_dir = paths["store"]

    os.remove(_inside(os.path.join(store_dir, SUBJECT_FILE), store_dir))
    _redact_quoting_file(store_dir)
    assert SUBJECT_FILE not in _working_tree_hits(store_dir)

    # The backup was taken before any of that.
    assert _working_tree_hits(paths["backup"])
    restored = os.path.join(_inside(paths["root"], paths["root"]), "restored")
    shutil.copytree(paths["backup"], restored)
    assert SUBJECT_FILE in _working_tree_hits(restored)


def test_rollback_cannot_recover_a_record_whose_history_was_rewritten(
    erasure_fixture,
):
    """Which recovery operation is incompatible with a completed erasure, shown.

    ``/rollback`` recovers a mistaken change by reading the file back out of
    git. A completed erasure removes exactly that — so after step 4 the undo
    path for the erased record does not fail *silently*, it reports that the
    file is not there. That is the intended incompatibility, and it is the
    reason the runbook's order matters: verify before you rewrite, because
    afterwards there is nothing to roll back to.
    """
    client, paths = erasure_fixture
    store_dir = paths["store"]

    os.remove(_inside(os.path.join(store_dir, SUBJECT_FILE), store_dir))
    os.remove(
        _inside(os.path.join(store_dir, f"people/{SUBJECT_SLUG}-history.md"), store_dir)
    )
    _redact_quoting_file(store_dir)
    _git(store_dir, "add", "-A")
    _git(store_dir, "commit", "-q", "-m", "erasure: remove and redact")
    _rewrite_history(store_dir)

    r = client.post(
        "/rollback", params={"file_path": SUBJECT_FILE, "dry_run": False}
    )
    assert r.status_code == 200, r.text
    assert "not found" in r.json()["result"].lower()
    assert not os.path.exists(os.path.join(store_dir, SUBJECT_FILE))
    assert _git_object_hits(store_dir) == []


def test_a_post_retirement_backup_restores_without_resurrecting(erasure_fixture):
    """The supported recovery: a backup taken *after* a retirement keeps it.

    Retirement is frontmatter in the file, and the file is what a backup
    holds — so restoring one does not put a retired claim back into recall,
    which is the property the issue asks to be demonstrated. (Erasure has no
    such property: see the test above. The two recoveries are different.)
    """
    client, paths = erasure_fixture
    store_dir = paths["store"]

    # The fixture already archived the subject memory. Take the backup now.
    snapshot = os.path.join(_inside(paths["root"], paths["root"]), "backup-after")
    shutil.copytree(store_dir, snapshot)

    # Lose the store and restore it from that snapshot.
    restored = os.path.join(_inside(paths["root"], paths["root"]), "restored-after")
    shutil.copytree(snapshot, restored)

    import frontmatter

    with open(os.path.join(restored, SUBJECT_FILE), encoding="utf-8") as fh:
        meta = frontmatter.load(fh).metadata
    assert meta["status"] == "archived"

    from palinode.core.lifecycle import eligibility

    assert eligibility(dict(meta), path=SUBJECT_FILE).retired


def test_the_documented_inventory_matches_what_the_fixture_shows(erasure_fixture):
    """The table in the docs is the one this suite exercises, key for key."""
    client, paths = erasure_fixture
    store_dir = paths["store"]

    rebuilt = {k for k, v in EXPECTED_INVENTORY.items() if v == AUTOMATIC_REBUILD}
    assert rebuilt == {"SQLite chunks + FTS + vec (.palinode.db and -wal/-shm)"}, (
        "exactly one location rebuilds its contents without an operator step; "
        "if that changes, the documentation's inventory changes with it"
    )
    # Nothing in the table is *removed* for free. The index is the closest
    # thing, and even it needs its files deleted — so no row may say the bare
    # word, which is what makes a reader skip step 6 and check with a search.
    assert "automatic" not in EXPECTED_INVENTORY.values()
    assert EXPECTED_INVENTORY["uncontrolled clone or backup"] == UNSUPPORTED

    # Every manual location named in the table exists in the fixture, so the
    # table cannot list something the runbook never has to touch.
    for rel in (
        SUBJECT_FILE,
        QUOTING_FILE,
        f"people/{SUBJECT_SLUG}-history.md",
        ".audit/retrievals.jsonl",
        ".audit/mcp-calls.jsonl",
        ".palinode/correction-candidates.jsonl",
    ):
        assert os.path.exists(os.path.join(store_dir, rel)), rel
