"""``memory_modules`` backend: a real Palinode store behind the LME-V2 harness.

Registered under ``memory_type = "palinode"`` when this module is imported
(``bench.longmemeval_v2.run`` does that before handing off to the upstream
``evaluation/harness.py``). Nothing upstream is modified.

Lifecycle, as the harness drives it:

* ``insert(trajectory)`` — once per haystack trajectory, in haystack order:
  the trajectory is written as ``trajectories/<id>.md`` (see ``corpus.py``)
  and indexed through the canonical ``index_file`` pipeline (parse → dedup →
  bge-m3 embed → SQLite-vec + FTS5). LLM-free: this is the save-only row.
* ``query(question)`` — hybrid recall (BM25 + vector, RRF) over the store;
  each hit becomes one ``{"type": "text"}`` context item, headed by the
  trajectory it came from. Also LLM-free — the query latency the leaderboard
  scores is one embed call plus two SQLite queries.
* ``_save_backend`` / ``_load_backend`` — the store directory (markdown +
  ``.palinode.db``) is copied into / pointed at the harness's memory-state
  directory, so a haystack is built once and reused across runs.

One store per process: Palinode's config is module-global, and the harness
runs one domain per invocation, which on the small tier is exactly one
haystack.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from memory_modules.memory import Memory, MemoryContextItem, register_memory, require

from bench import harness
from bench.longmemeval.adapter import (
    content_words,
    dedupe_hits,
    keyword_query,
    reset_backends,
)
from bench.longmemeval_v2 import corpus

STORE_DIRNAME = "palinode_store"
META_FILENAME = "trajectories.json"

DEFAULT_PARAMS: dict[str, Any] = {
    "top_k": 10,
    "threshold": 0.0,          # per-arm floor; 0 = let RRF decide (raw-slice pool, no priors)
    "hybrid_weight": 0.5,
    "hybrid": True,            # False → BM25 only (no embed call at query time)
    "slice_max_chars": corpus.DEFAULT_SLICE_MAX_CHARS,
    "dedup_score_gap": 1e9,    # disable the ranker's per-file dedup: several states of one
                               # trajectory are legitimately the evidence for one question
    "fts_mode": "or",          # BM25 arm: "or" = any content word (this module); "and" = store.search_hybrid's
                               # implicit-AND MATCH, which returns nothing for most question-shaped queries
    "neighbor_radius": 0,      # also return states N±r around each hit state N (the upstream slice baseline
                               # uses radius 1: for "what happens after X" the next state is the evidence)
    "images": False,           # also return each hit state's screenshot as an image item (the upstream RAG
                               # baseline does; the reader is a VL model). Query-time; needs screenshots_root.
    "screenshots_root": None,  # data root holding screenshots/<trajectory>/<step>.png; default $LONGMEMEVAL_V2_DATA
    "extract": False,          # insert-time: LLM-proposed notes (specs/prompts/trajectory-extraction.md) written
                               # through save_memory as Insight files — the notes pool. Endpoint: LME_EXTRACT_*.
    "extract_max_tokens": 4000,
    "extract_workers": 2,      # extraction overlaps the next trajectory's slice embedding
    "notes_top_k": 0,          # query-time: notes returned ahead of the raw slices (0 = slices only)
    "workspace_root": None,    # where the store is built; default $LME_PALINODE_WORKSPACE or a tmp dir
}

SLICES_CATEGORY = "trajectories"   # parent dir of the raw-slice files
NOTES_CATEGORY = "insights"        # where save_memory puts Insight notes

_SECTION_STATE_RE = re.compile(r"^state-(\d+)(?:-part-\d+-of-\d+)?$")


def section_state_index(section_id: str | None) -> int | None:
    m = _SECTION_STATE_RE.match(section_id or "")
    return int(m.group(1)) if m else None


def bm25_or(question: str, *, top_k: int, category: str | None = None) -> list[dict[str, Any]]:
    """BM25 candidates for *any* content word of the question.

    FTS5 treats whitespace as implicit AND and ``sanitize_fts_query`` strips
    ``OR``, so ``store.search_fts("Where is the Login as Customer button")``
    needs every term in one chunk — a natural-language question almost always
    carries a word the evidence lacks, and the arm comes back empty. Same SQL
    and row shape as ``search_fts``; only the MATCH expression differs.
    """
    from palinode.core import store

    terms = [t for t in content_words(question).split() if t]
    if not terms:
        return []
    match = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
    sql = """SELECT c.id, c.file_path, c.section_id, c.content, c.category, c.metadata,
                    rank AS bm25_score
             FROM chunks_fts fts JOIN chunks c ON c.rowid = fts.rowid
             WHERE chunks_fts MATCH ?"""
    params: list[Any] = [match]
    if category:
        sql += " AND c.category = ?"
        params.append(category)
    sql += " ORDER BY rank LIMIT ?"
    params.append(top_k)
    db = store.get_db()
    try:
        rows = db.execute(sql, tuple(params)).fetchall()
    finally:
        db.close()
    out = []
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        raw = abs(row["bm25_score"]) if row["bm25_score"] else 0.0
        out.append({"id": row["id"], "file_path": row["file_path"], "section_id": row["section_id"],
                    "content": row["content"], "category": row["category"], "metadata": meta,
                    "score": min(raw / 25.0, 1.0)})
    return out


def hybrid_or(question: str, vec: list[float] | None, *, top_k: int, threshold: float,
              hybrid_weight: float, category: str | None = None) -> list[dict[str, Any]]:
    """``store.search_hybrid`` with the BM25 arm swapped for :func:`bm25_or`;
    fusion and re-ranking are the store's own pure ``rank_hybrid``."""
    from palinode.core import ranker, store

    vec_results = (store.search(vec, category=category, top_k=top_k * 2, threshold=0.0, record_access=False)
                   if vec else [])
    fts_results = bm25_or(question, top_k=top_k * 2, category=category)
    return ranker.rank_hybrid(vec_results, fts_results, top_k=top_k, threshold=threshold,
                              hybrid_weight=hybrid_weight if vec else 1.0,
                              priority_weight=store._PRIORITY_RANK_WEIGHT, include_daily=True)


# Params that shape the store at insert time; everything else is query-time and may
# differ when a saved store is loaded (a top-k sweep must not cost a re-index).
INSERT_TIME_PARAMS = ("slice_max_chars", "extract")


@register_memory
class PalinodeMemory(Memory):
    memory_type = "palinode"

    @classmethod
    def reconcile_loaded_memory_config(cls, saved_config, requested_config):
        require(saved_config["memory_type"] == cls.memory_type,
                f"saved memory_type {saved_config['memory_type']!r} is not {cls.memory_type!r}")
        if requested_config is None:
            return {"memory_type": cls.memory_type, "memory_params": dict(saved_config["memory_params"])}
        require(requested_config["memory_type"] == cls.memory_type,
                f"requested memory_type {requested_config['memory_type']!r} is not {cls.memory_type!r}")
        saved, req = saved_config["memory_params"], requested_config["memory_params"]
        for key in INSERT_TIME_PARAMS:
            require(saved.get(key) == req.get(key),
                    f"{key} differs from the saved store ({saved.get(key)!r} vs {req.get(key)!r}); rebuild it")
        merged = dict(req)
        merged.pop("workspace_root", None)   # the loaded store's location wins
        return {"memory_type": cls.memory_type, "memory_params": merged}

    def __init__(self, memory_params: dict[str, object]) -> None:
        unknown = set(memory_params) - set(DEFAULT_PARAMS)
        require(not unknown, f"palinode memory_params unknown keys: {sorted(unknown)}")
        # Keep the raw params as memory_params: load_memory() requires the requested
        # config to equal the saved one byte-for-byte, so defaults must not leak in.
        super().__init__(memory_params)
        params = {**DEFAULT_PARAMS, **memory_params}
        self.top_k = int(params["top_k"])
        self.threshold = float(params["threshold"])
        self.hybrid_weight = float(params["hybrid_weight"])
        self.hybrid = bool(params["hybrid"])
        self.slice_max_chars = int(params["slice_max_chars"])
        self.dedup_score_gap = float(params["dedup_score_gap"])
        self.fts_mode = str(params["fts_mode"])
        require(self.fts_mode in ("or", "and"), f"fts_mode must be 'or' or 'and', got {self.fts_mode!r}")
        self.neighbor_radius = int(params["neighbor_radius"])
        require(self.neighbor_radius >= 0, "neighbor_radius must be >= 0")
        self.images = bool(params["images"])
        self.extract = bool(params["extract"])
        self.extract_max_tokens = int(params["extract_max_tokens"])
        self.extract_workers = max(1, int(params["extract_workers"]))
        self._extract_pool = None
        self._extract_futures: list[Any] = []
        self.notes_top_k = int(params["notes_top_k"])
        self._extract_ep = None
        shots = params["screenshots_root"] or os.environ.get("LONGMEMEVAL_V2_DATA", "~/.cache/longmemeval-v2")
        self.screenshots_root = os.path.expanduser(str(shots))
        root = params["workspace_root"] or os.environ.get("LME_PALINODE_WORKSPACE")
        if not root:
            root = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lme-v2-palinode-{os.getpid()}")
        self.store_dir = os.path.join(str(root), STORE_DIRNAME)
        self._trajectories: dict[str, dict[str, Any]] = {}   # id → {goal, outcome, environment, steps}
        self._insert_stats: dict[str, Any] = {"files": 0, "chunks_written": 0, "fts_only_files": 0, "insert_seconds": 0.0,
                                              "notes": 0, "extract_errors": 0, "extract_parse_failures": 0,
                                              "extract_prompt_tokens": 0, "extract_completion_tokens": 0,
                                              "extract_seconds": 0.0}
        self._query_stats: dict[str, int] = {"queries": 0, "embed_errors": 0, "keyword_fallbacks": 0}
        self._lock = threading.Lock()
        self._hits_local = threading.local()
        self._pointed = False
        self._fresh = True

    # ------------------------------------------------------------------ store
    def _point(self, *, fresh: bool) -> None:
        """Aim Palinode's global config at the store; create the schema if new."""
        from palinode.core.config import config

        if fresh and os.path.isdir(self.store_dir):
            shutil.rmtree(self.store_dir)
        harness.point_config_at(self.store_dir)
        config.search.dedup_score_gap = self.dedup_score_gap
        if fresh:
            harness.init_store()
        self._pointed = True
        self._fresh = fresh

    def _ensure_pointed(self) -> None:
        if not self._pointed:
            self._point(fresh=True)

    # ----------------------------------------------------------------- insert
    def insert(self, trajectory: dict[str, object]) -> None:
        from palinode.indexer import reconcile as reconcile_mod
        from palinode.indexer.index_file import index_file

        self._ensure_pointed()
        tid = str(trajectory.get("id"))
        require(bool(tid) and tid != "None", "trajectory has no id")
        rel = corpus.trajectory_rel_path(tid)
        path = os.path.join(self.store_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(corpus.trajectory_markdown(trajectory, slice_max_chars=self.slice_max_chars))
        t0 = time.perf_counter()
        fts_only = 0
        try:
            res = index_file(path)
        except Exception as e:  # noqa: BLE001 - embedder failure on one file: keyword-only, never drop it
            # Same fallback the V1 adapter needed against bge-m3 NaN 500s: re-index with
            # embeds deferred so reconcile takes its FTS-only path.
            reset_backends()
            orig = reconcile_mod._embeds_deferred
            reconcile_mod._embeds_deferred = lambda client: True  # type: ignore[assignment]
            try:
                res = index_file(path)
            finally:
                reconcile_mod._embeds_deferred = orig  # type: ignore[assignment]
            fts_only = 1
            res["embed_error"] = str(e)[:200]
        dt = time.perf_counter() - t0
        with self._lock:
            self._trajectories[tid] = {
                "goal": " ".join(str(trajectory.get("goal") or "").split()),
                "outcome": trajectory.get("outcome"),
                "environment": trajectory.get("environment"),
                "steps": len(trajectory.get("states") or []),
                "screenshots": [s.get("screenshot") for s in (trajectory.get("states") or [])],
                "chunks": int(res.get("chunks_written", 0)) + int(res.get("chunks_unchanged", 0)),
                "fts_only": bool(fts_only),
            }
            self._insert_stats["files"] += 1
            self._insert_stats["chunks_written"] += int(res.get("chunks_written", 0))
            self._insert_stats["fts_only_files"] += fts_only
            self._insert_stats["insert_seconds"] += dt
        if self.extract:
            self._submit_extraction(trajectory)

    def _submit_extraction(self, trajectory: dict[str, Any]) -> None:
        """Run the LLM call on a daemon thread; the notes are written there (the
        store's connections are per-call, and save_memory serialises its own
        git/index work). ``drain_extraction`` joins before save — with a
        deadline: a request the extractor never answers (the extraction host held one
        open for 2.5 h on 2026-09-04, past every socket timeout) costs that
        trajectory its notes, not the build. Daemon threads so a stuck one
        cannot block interpreter exit either."""
        from bench.longmemeval import llm

        if self._extract_ep is None:
            self._extract_ep = llm.Endpoint.from_env("EXTRACT")
        if self._extract_pool is None:
            self._extract_pool = threading.Semaphore(self.extract_workers)
        # Bound the backlog so a slow extractor doesn't buffer the whole haystack in memory.
        while len([t for t in self._extract_futures if t.is_alive()]) >= 2 * self.extract_workers:
            time.sleep(0.5)
        tid = str(trajectory.get("id"))

        def run() -> None:
            with self._extract_pool:
                self._extract_notes(trajectory)

        t = threading.Thread(target=run, name=f"lme2-extract-{tid}", daemon=True)
        t.trajectory_id = tid  # type: ignore[attr-defined]
        t.start()
        self._extract_futures.append(t)

    def drain_extraction(self, timeout_s: float | None = None) -> None:
        """Join extraction threads, giving the whole backlog at most
        *timeout_s* (default: two extractor timeouts). Stragglers are recorded
        as ``extract_errors`` and abandoned."""
        if timeout_s is None:
            timeout_s = 2.0 * float(getattr(self._extract_ep, "timeout_s", 900.0) or 900.0)
        deadline = time.monotonic() + timeout_s
        for t in self._extract_futures:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                tid = getattr(t, "trajectory_id", "?")
                print(f"palinode: extraction for trajectory {tid} still running after {timeout_s:.0f}s — abandoned", flush=True)
                with self._lock:
                    self._insert_stats["extract_errors"] += 1
                    meta = self._trajectories.get(tid)
                    if meta is not None:
                        meta["extract_error"] = "drain timeout"
        self._extract_futures = [t for t in self._extract_futures if t.is_alive()]

    def _extract_notes(self, trajectory: dict[str, Any]) -> None:
        from bench.longmemeval_v2 import extract

        t0 = time.perf_counter()
        res = extract.extract_trajectory(self._extract_ep, trajectory, max_tokens=self.extract_max_tokens)
        try:
            paths = extract.save_notes(res, trajectory) if res.notes else []
        except Exception as e:  # noqa: BLE001 - a save failure is recorded, not fatal
            res.error = f"save: {str(e)[:200]}"
            paths = []
        with self._lock:
            self._insert_stats["notes"] += len(paths)
            self._insert_stats["extract_errors"] += int(res.error is not None)
            self._insert_stats["extract_parse_failures"] += int(not res.parse_ok and res.error is None)
            self._insert_stats["extract_prompt_tokens"] += res.usage.get("prompt_tokens", 0)
            self._insert_stats["extract_completion_tokens"] += res.usage.get("completion_tokens", 0)
            self._insert_stats["extract_seconds"] += time.perf_counter() - t0
            meta = self._trajectories.get(str(trajectory.get("id")))
            if meta is not None:
                meta["notes"] = len(paths)
                if res.error:
                    meta["extract_error"] = res.error

    # ------------------------------------------------------------------ query
    def query(self, query: str, query_image: str | None = None) -> list[MemoryContextItem]:
        from palinode.core import embedder, store

        self._ensure_pointed()
        kw = keyword_query(query)
        vec: list[float] | None = None
        embed_error: str | None = None
        if self.hybrid:
            for text in (query, content_words(query)):
                if not text:
                    continue
                try:
                    vec = embedder.embed(text)
                    break
                except Exception as e:  # noqa: BLE001 - degrade to keyword, never abort the question
                    embed_error = str(e)[:200]
        notes: list[dict[str, Any]] = []
        if self.notes_top_k:
            notes = hybrid_or(query, vec, top_k=2 * self.notes_top_k, threshold=self.threshold,
                              hybrid_weight=self.hybrid_weight, category=NOTES_CATEGORY)
            notes = dedupe_hits(notes)[0][: self.notes_top_k]
        slice_cat = SLICES_CATEGORY if self.notes_top_k else None   # no notes in the store → no filter needed
        if self.fts_mode == "or":
            hits = hybrid_or(query, vec, top_k=2 * self.top_k, threshold=self.threshold,
                             hybrid_weight=self.hybrid_weight, category=slice_cat)
        elif vec:
            hits = store.search_hybrid(kw, vec, top_k=2 * self.top_k, threshold=self.threshold,
                                       hybrid_weight=self.hybrid_weight, include_daily=True,
                                       record_access=False)
        else:
            hits = store.search_fts(kw, top_k=2 * self.top_k)
        hits, _dups = dedupe_hits(hits)
        hits = hits[: self.top_k]
        if self.neighbor_radius:
            hits = self._expand_neighbors(hits)
        hits = notes + hits
        with self._lock:
            self._query_stats["queries"] += 1
            if embed_error:
                self._query_stats["embed_errors"] += 1
            if self.hybrid and not vec:
                self._query_stats["keyword_fallbacks"] += 1
        self._hits_local.hits = hits  # read back by post_query_hook on this worker thread
        # The harness concatenates text items with no separator, so each item
        # delimits itself: blank line, numbered header, body, newline. With
        # images on, the state's screenshot follows its text.
        items: list[MemoryContextItem] = []
        for i, h in enumerate((h for h in hits if (h.get("content") or "").strip()), 1):
            items.append({"type": "text", "value": self._render(i, h)})
            shot = self._screenshot_for(h) if self.images else None
            if shot:
                items.append({"type": "image", "value": shot})
        return items

    def _render_note(self, rank: int, hit: dict[str, Any]) -> str:
        meta = hit.get("metadata") or {}
        kind = meta.get("note_kind") or "note"
        tid = meta.get("trajectory_id") or "?"
        tmeta = self._trajectories.get(str(tid), {})
        head = f"\n\n[{rank}] Note ({kind}) from trajectory {tid} — outcome: {tmeta.get('outcome') or meta.get('outcome') or '?'}"
        return f"{head}\n{hit.get('content', '').strip()}\n"

    def _expand_neighbors(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Around each hit state N, splice in states N-r…N+r of the same trajectory
        (all parts of a split state), in state order, keeping rank order between
        hits and never repeating a chunk. Neighbours carry ``score: None``."""
        from palinode.core import store

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        db = store.get_db()
        try:
            for h in hits:
                idx = section_state_index(h.get("section_id"))
                fp = h.get("file_path", "")
                if idx is None:
                    if h["id"] not in seen:
                        seen.add(h["id"])
                        out.append(h)
                    continue
                window: list[dict[str, Any]] = []
                for n in range(max(0, idx - self.neighbor_radius), idx + self.neighbor_radius + 1):
                    if n == idx:
                        window.append(h)
                        continue
                    rows = db.execute(
                        "SELECT id, file_path, section_id, content FROM chunks WHERE file_path = ? "
                        "AND (section_id = ? OR section_id LIKE ?) ORDER BY section_id",
                        (fp, f"state-{n}", f"state-{n}-part-%"),
                    ).fetchall()
                    window.extend({"id": r[0], "file_path": r[1], "section_id": r[2], "content": r[3], "score": None}
                                  for r in rows)
                for c in window:
                    if c["id"] not in seen:
                        seen.add(c["id"])
                        out.append(c)
        finally:
            db.close()
        return out

    def _screenshot_for(self, hit: dict[str, Any]) -> str | None:
        tid = corpus.hit_trajectory_id(hit.get("file_path", ""))
        idx = section_state_index(hit.get("section_id"))
        shots = (self._trajectories.get(tid or "", {}) or {}).get("screenshots") or []
        if idx is None or idx >= len(shots) or not shots[idx]:
            return None
        path = os.path.join(self.screenshots_root, shots[idx])
        return path if os.path.isfile(path) else None

    def _render(self, rank: int, hit: dict[str, Any]) -> str:
        if not corpus.hit_trajectory_id(hit.get("file_path", "")):
            return self._render_note(rank, hit)
        tid = corpus.hit_trajectory_id(hit.get("file_path", "")) or "?"
        meta = self._trajectories.get(tid, {})
        goal = meta.get("goal") or ""
        head = (f"\n\n[{rank}] Trajectory {tid} — {meta.get('environment') or ''}, "
                f"outcome: {meta.get('outcome') or '?'}, {meta.get('steps') or '?'} steps")
        if goal:
            head += f"\nGoal: {goal}"
        return f"{head}\n{hit.get('content', '').strip()}\n"

    def post_query_hook(self, *, query: str, query_image: str | None,
                        memory_context: list[MemoryContextItem]) -> dict[str, object] | None:
        hits = getattr(self._hits_local, "hits", None) or []
        self._hits_local.hits = None
        return {
            "palinode_hits": [
                {"trajectory_id": corpus.hit_trajectory_id(h.get("file_path", ""))
                                  or (h.get("metadata") or {}).get("trajectory_id"),
                 "section_id": h.get("section_id"), "score": h.get("score"),
                 "neighbor": h.get("score") is None,
                 "note": (h.get("metadata") or {}).get("note_kind")}
                for h in hits
            ],
        }

    # ------------------------------------------------------------- persistence
    def _save_backend(self, output_dir: Path) -> None:
        self._ensure_pointed()
        self.drain_extraction()
        self.check_vectors()
        dest = output_dir / STORE_DIRNAME
        if os.path.abspath(dest) != os.path.abspath(self.store_dir):
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(self.store_dir, dest, ignore=shutil.ignore_patterns("*.db-journal", "*.db-wal", "*.db-shm"))
        (output_dir / META_FILENAME).write_text(json.dumps({
            "trajectories": self._trajectories,
            "insert_stats": self._insert_stats,
            "params": self.memory_params,
            "embedder": self._embedder_label(),
        }, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    def _load_backend(self, input_dir: Path) -> None:
        self.store_dir = str(input_dir / STORE_DIRNAME)
        require(os.path.isfile(os.path.join(self.store_dir, ".palinode.db")), f"no Palinode store at {self.store_dir}")
        meta = json.loads((input_dir / META_FILENAME).read_text(encoding="utf-8"))
        self._trajectories = dict(meta.get("trajectories") or {})
        self._insert_stats = dict(meta.get("insert_stats") or self._insert_stats)
        self._point(fresh=False)
        self._relocate_paths()
        self.check_vectors()

    def _relocate_paths(self) -> None:
        """Chunks store the absolute path of the file they were indexed from — the
        build workspace. A saved ``memory_state`` is a copy, so point the rows at
        it: the store is then self-contained (freshness checks, re-indexing) and
        a leaderboard package reproduces from the directory alone. ``chunks_fts``
        is external-content over ``chunks``, so no FTS rebuild is needed."""
        from palinode.core import store

        marker = "/" + os.path.join("trajectories", "")
        db = store.get_db()
        try:
            row = db.execute("SELECT file_path FROM chunks LIMIT 1").fetchone()
            if not row:
                return
            old = row[0]
            cut = old.find(marker)
            if cut < 0:
                return
            old_root, new_root = old[:cut], self.store_dir.rstrip("/")
            if old_root == new_root:
                return
            n = db.execute("UPDATE chunks SET file_path = ? || substr(file_path, ?) WHERE file_path LIKE ? || '%'",
                           (new_root, len(old_root) + 1, old_root)).rowcount
            db.commit()
            print(f"palinode: relocated {n} chunk paths {old_root} → {new_root}", flush=True)
        finally:
            db.close()

    def stats(self) -> dict[str, Any]:
        self.drain_extraction()
        chunks, vectors = harness._counts(self.store_dir)
        return {"insert": dict(self._insert_stats), "query": dict(self._query_stats),
                "trajectories": len(self._trajectories), "chunks": chunks, "vectors": vectors,
                "embedder": self._embedder_label()}

    def check_vectors(self) -> None:
        """A hybrid store with no vectors is a silently keyword-only store: the
        embedder's circuit breaker defers embeds rather than raising. Refuse to
        proceed rather than report a hybrid number that isn't one."""
        if not self.hybrid:
            return
        chunks, vectors = harness._counts(self.store_dir)
        require(vectors > 0, f"hybrid=True but the store has 0 vectors over {chunks} chunks — embedder unreachable? ({self._embedder_label()})")
        if vectors < chunks:
            print(f"palinode: {chunks - vectors} of {chunks} chunks have no vector (keyword-only)", flush=True)

    @staticmethod
    def _embedder_label() -> str:
        from palinode.core.config import config

        emb = config.embeddings.primary
        return f"{emb.model} @ {emb.url}"
