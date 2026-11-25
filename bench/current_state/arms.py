"""The arms: five ways of turning a question into text an agent reads.

Every arm answers the *same* question against the *same* store with the *same*
seed retrieval, and every arm is charged for what it delivered under one token
accounting (``packing.estimate_tokens``, the estimator the shipping packer
budgets with). What differs is only the semantics applied between the hit and
the text:

``baseline``
    Plain top-k search, rendered from the **raw file bytes** — the
    pre-projection reading, where a fact retired in place still sits in the
    delivered excerpt next to its replacement.
``projection``
    The same hits rendered from the **indexed** text, which is the current-text
    projection: tombstoned wordings are gone from what the reader sees.
    Resolution off.
``bounded_evidence``
    ``resolve="full"`` evidence around each hit plus the resolution policy, per
    seed, with no grouping or budget. This is the ``/search`` surface.
``bundle``
    The whole bounded-resolution operation — grouping, budgeting,
    conflict-preserving packing, coverage and a delivery receipt — under the
    per-turn default budget. Optionally delivered through the **actual** hook
    script against a live server (:func:`hook_context`), which is what a
    session really receives.
``matched_budget``
    ``baseline`` with top-k raised until its token total reaches the bundle's.
    Without it, any gain the bundle shows could be "it read more material".

The bounded-evidence arm is rendered by this module in the bundle's own section
grammar. That is deliberate and it is a limitation worth naming: it means arms
3 and 4 differ by grouping, budget and packing, not by whether the reader can
see a marker at all. Arms 1 and 2 carry no markers because the surfaces they
model carry none.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Iterator

#: Hits a per-turn recall channel injects by default (the hook's
#: ``PALINODE_HOOK_RECALL_MAX_RESULTS``). Every arm starts here so "read more
#: material" is a controlled variable, not an arm's advantage.
DEFAULT_TOP_K = 3

#: Characters of body carried per unmarked hit — the hook's ``max_chars`` for
#: a search snippet.
SNIPPET_CHARS = 300

#: Top-k values the matched-budget control climbs through.
MATCHED_LADDER: tuple[int, ...] = (3, 5, 8, 12, 20, 30, 50)

#: The arms scored side by side in the report.
ARMS: tuple[str, ...] = (
    "baseline", "projection", "bounded_evidence", "bundle", "matched_budget",
)


@dataclass
class Delivery:
    """What one arm delivered, and what it cost."""

    arm: str
    context: str
    tokens: int
    file_reads: int
    latency_ms: float
    top_k: int
    coverage: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "tokens": self.tokens,
            "file_reads": self.file_reads,
            "latency_ms": round(self.latency_ms, 3),
            "top_k": self.top_k,
            "coverage": self.coverage,
            "chars": len(self.context),
            **({"extra": self.extra} if self.extra else {}),
        }


# ── shared seed retrieval ────────────────────────────────────────────────────


def _ref_of(rel: str) -> str:
    rel = rel.replace(os.sep, "/")
    return rel[:-3] if rel.endswith(".md") else rel


def seed_rows(query: str, top_k: int) -> tuple[list[dict[str, Any]], set[str]]:
    """The seeds every arm starts from — the retrieval ``/search`` performs.

    Mirrors ``bundle._query_seeds`` (hybrid with the API threshold, BM25 when
    no embedder answers) and the ADR-009 Layer 2 visibility gate, so no arm can
    win by seeing a record another arm may not. ``record_access=False``: the
    benchmark must not inflate ``recall_count``, which is itself one of the
    invariants under test.
    """
    from palinode.core import embedder, store
    from palinode.core.config import config
    from palinode.core.visibility import filter_visible

    reasons: set[str] = set()
    try:
        vector = embedder.embed(query)
    except Exception:  # noqa: BLE001 — an unavailable embedder is a coverage fact
        vector = None
    if not vector:
        reasons.add("degraded:keyword_only")
        rows = store.search_fts(query, top_k=top_k)
    else:
        rows = store.search_hybrid(
            query_text=query,
            query_embedding=vector,
            top_k=top_k,
            threshold=config.search.api_threshold,
            hybrid_weight=config.search.hybrid_weight,
            use_fts=config.search.hybrid_enabled,
            record_access=False,
        )
    visible = filter_visible(None, rows)
    if len(visible) != len(rows):
        reasons.add("target_hidden")

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in visible:
        path = str(row.get("file_path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        deduped.append(row)
    return deduped, reasons


def _rel_of(world: Any, row: dict[str, Any]) -> str:
    return os.path.relpath(str(row.get("file_path") or ""), world.root)


def _squash(text: str, limit: int = SNIPPET_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _body_of(raw: str) -> str:
    from palinode.core.parser import split_frontmatter

    _head, body = split_frontmatter(raw)
    return body


# ── arm 1: baseline (raw text, pre-projection semantics) ─────────────────────


def baseline(world: Any, question: str, *, top_k: int = DEFAULT_TOP_K,
             arm: str = "baseline") -> Delivery:
    """Top-k hits rendered from the raw file on disk.

    Reading the file rather than the indexed row is what makes this the
    *pre-projection* arm: the index has carried projected text since the
    projection shipped, so rendering the row would quietly give the baseline
    the benefit of the thing being measured.
    """
    started = perf_counter()
    rows, reasons = seed_rows(question, top_k)
    lines: list[str] = []
    reads = 0
    for rank, row in enumerate(rows, start=1):
        rel = _rel_of(world, row)
        try:
            raw = world.read(_ref_of(rel))
            reads += 1
        except OSError:
            continue
        lines.append(
            f"- [{_ref_of(rel)}] (rank {rank}) {_squash(_body_of(raw))}"
        )
    context = "## Related memories\n" + ("\n".join(lines) if lines else "(no hits)")
    return _delivery(arm, context, reads, started, top_k, reasons)


# ── arm 2: projection (indexed current text, resolution off) ─────────────────


def projection(world: Any, question: str, *, top_k: int = DEFAULT_TOP_K) -> Delivery:
    """The same hits rendered from ``chunks.content`` — the projected text."""
    started = perf_counter()
    rows, reasons = seed_rows(question, top_k)
    lines = [
        f"- [{_ref_of(_rel_of(world, row))}] (rank {rank}) "
        f"{_squash(str(row.get('content') or ''))}"
        for rank, row in enumerate(rows, start=1)
    ]
    context = "## Related memories\n" + ("\n".join(lines) if lines else "(no hits)")
    return _delivery("projection", context, 0, started, top_k, reasons)


# ── arm 3: bounded evidence + resolution, per seed ───────────────────────────


def _statement(text: str, title: str | None) -> str:
    """The body without its own title line — what ``bundle._statement`` renders."""
    lines = [
        line for line in text.splitlines()
        if not (line.startswith("#") and title and title.lower() in line.lower())
    ]
    return _squash("\n".join(lines), SNIPPET_CHARS)


def _render_side(side: Any, view: dict[str, Any] | None, *,
                 qualifiers: bool = True) -> list[str]:
    """One side, rendered the way ``bundle._line`` renders one.

    ``qualifiers=False`` mirrors the shipped renderer's conflict section, which
    prints the group's reasons but not each side's own qualifiers. Matching it
    matters: an arm that rendered more than the surface it stands for would
    make that surface look better than it is.
    """
    title = f" {view['title']}" if view and view.get("title") else ""
    bits = [side.currency]
    if view and view.get("freshness") == "stale":
        bits.append("index stale")
    if side.effective_at:
        bits.append(side.effective_at[:10])
    excerpt = (
        f" {_statement(view['excerpt'], view.get('title'))}"
        if view and view.get("excerpt") else ""
    )
    out = [f"- [{side.ref}]{title} [{' · '.join(bits)}]{excerpt}"]
    if qualifiers and side.qualifiers:
        out.append(f"    qualifiers: {', '.join(side.qualifiers)}")
    return out


def bounded_evidence(world: Any, question: str, *, top_k: int = DEFAULT_TOP_K) -> Delivery:
    """``resolve="full"`` evidence plus the resolution policy, seed by seed."""
    from palinode.core.evidence import resolve_evidence
    from palinode.core.resolution import (
        OUTCOME_CONFLICT,
        OUTCOME_INSUFFICIENT,
        OUTCOME_SUPPORTED,
        resolve,
    )

    started = perf_counter()
    rows, reasons = seed_rows(question, top_k)
    evidence = resolve_evidence(rows, mode="full", now=world.now)
    resolutions = [resolve(seed.seed_meta, seed, now=world.now) for seed in evidence.seeds]

    views: dict[str, dict[str, Any]] = {}
    for row, seed in zip(rows, evidence.seeds, strict=True):
        if seed.seed_ref:
            views[seed.seed_ref] = {
                "title": (seed.seed_meta or {}).get("title"),
                "excerpt": str(row.get("content") or ""),
                "freshness": seed.seed_freshness,
            }
        for rec in seed.records():
            views.setdefault(rec.ref, {"title": rec.title, "excerpt": rec.excerpt,
                                       "freshness": rec.freshness})

    current: list[str] = []
    contested: list[str] = []
    unknown: list[str] = []
    for resolution in resolutions:
        if resolution.outcome == OUTCOME_SUPPORTED and resolution.current:
            current.extend(_render_side(resolution.current, views.get(resolution.current.ref)))
            if resolution.reasons:
                current.append(f"    why: {', '.join(resolution.reasons)}")
        elif resolution.outcome == OUTCOME_CONFLICT:
            for side in resolution.sides:
                contested.extend(
                    _render_side(side, views.get(side.ref), qualifiers=False)
                )
            contested.append(f"    reasons: {', '.join(resolution.reasons)}")
        elif resolution.outcome == OUTCOME_INSUFFICIENT:
            ref = resolution.sides[0].ref if resolution.sides else None
            unknown.append(f"- [{ref}] — {', '.join(resolution.reasons)}")

    out = ["### Resolved from memory (current state)", f'Question: "{question}"']
    if current:
        out += ["", f"Current ({_units(current)}):", *current]
    if contested:
        out += ["", f"Contested ({_units(contested)}) — no winner; every visible side is shown:", *contested]
    if unknown:
        out += ["", f"Unknown ({len(unknown)}):", *unknown]
    if not (current or contested or unknown):
        out += ["", "Nothing in memory answers this."]
    folded = evidence.coverage()
    folded["reasons"] = sorted(set(folded.get("reasons", [])) | reasons)
    folded["status"] = "partial" if folded["reasons"] else "complete"
    out += ["", f"Coverage: {folded['status']}"
            + (f" ({', '.join(folded['reasons'])})" if folded["reasons"] else "")]

    return _delivery(
        "bounded_evidence", "\n".join(out), int(evidence.stats.get("files_read", 0)),
        started, top_k, set(folded["reasons"]),
        extra={"seeds": len(rows), "edges": evidence.stats.get("edges_followed", 0)},
    )


def _units(lines: list[str]) -> int:
    return sum(1 for line in lines if not line.startswith("    "))


# ── arm 4: the bundle ────────────────────────────────────────────────────────


@contextlib.contextmanager
def _counting_evidence() -> Iterator[dict[str, int]]:
    """Count the evidence layer's file reads for a ``build_bundle`` call.

    ``bundle`` binds ``resolve_evidence`` at import, so the wrapper goes on the
    bundle module's own name. The alternative — wrapping ``open`` — would also
    count the harness's reads and make the number meaningless.
    """
    from palinode.core import bundle as bundle_mod

    stats = {"files_read": 0, "edges_followed": 0}
    original = bundle_mod.resolve_evidence

    def counting(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        stats["files_read"] += int(result.stats.get("files_read", 0))
        stats["edges_followed"] += int(result.stats.get("edges_followed", 0))
        return result

    bundle_mod.resolve_evidence = counting  # type: ignore[assignment]
    try:
        yield stats
    finally:
        bundle_mod.resolve_evidence = original  # type: ignore[assignment]


def bundle(world: Any, question: str, *, top_k: int = DEFAULT_TOP_K,
           budget: dict[str, int] | None = None) -> Delivery:
    """The bounded-resolution operation under the per-turn default budget."""
    from palinode.core.bundle import BundleBudget, BundleRequest, build_bundle, render_bundle

    limits = budget or {}
    request = BundleRequest(
        query=question,
        budget=BundleBudget(
            max_items=int(limits.get("max_items", top_k)),
            max_chars=int(limits.get("max_chars", 2000)),
            max_tokens=limits.get("max_tokens"),
        ),
    )
    started = perf_counter()
    with _counting_evidence() as stats:
        result = build_bundle(request, chain=None, now=world.now)
    context = render_bundle(result)
    # Every ref the *structured* payload carries, including the support and
    # discovery refs the text renderer does not print. Scored separately from
    # detection-in-text, because "the bundle found it" and "the agent could see
    # it" are different claims and the gap between them is a finding.
    payload_refs: list[str] = []
    for item in result.selected:
        payload_refs.append(item.assertion.ref or "")
        for group in item.refs.values():
            payload_refs.extend(group)
        payload_refs.extend(str(alt.get("ref") or "") for alt in item.alternatives)
    payload_refs.extend(r.assertion.ref or "" for r in result.replaced)
    payload_refs.extend(s.ref or "" for g in result.conflicts for s in g.sides)
    payload_refs.extend(i.ref or "" for i in result.insufficient)

    return _delivery(
        "bundle", context, stats["files_read"], started, top_k,
        set(result.coverage.get("reasons", [])),
        extra={
            "payload_refs": sorted({r for r in payload_refs if r}),
            "receipt_ref": result.receipt_ref,
            "omitted_conflicts": result.omitted_conflicts,
            "budget": result.budget,
            "source_revisions": dict(result.source_revisions),
            "selected": [s.assertion.ref for s in result.selected],
            "replaced": [r.assertion.ref for r in result.replaced],
            "conflicts": [list(g.refs) for g in result.conflicts],
            "insufficient": [i.ref for i in result.insufficient],
            "receipt": result.receipt,
        },
    )


# ── arm 5: matched-budget larger-top-k control ───────────────────────────────


def matched_budget(world: Any, question: str, *, target_tokens: int) -> Delivery:
    """``baseline`` with top-k raised until it costs what the bundle cost.

    Stops at the first rung that reaches ``target_tokens``, or at the last rung
    that still grew the payload — a store with fewer matching records than the
    ladder asks for cannot be made to spend more, and the delivery records how
    close it got so the report never claims a match it did not achieve.
    """
    best: Delivery | None = None
    for k in MATCHED_LADDER:
        delivery = baseline(world, question, top_k=k, arm="matched_budget")
        plateaued = best is not None and delivery.tokens == best.tokens
        best = delivery
        if delivery.tokens >= target_tokens or plateaued:
            break
    assert best is not None
    best.extra["target_tokens"] = target_tokens
    best.extra["matched"] = best.tokens >= target_tokens
    return best


# ── the real delivery path: a live server + the actual hook script ───────────


@contextlib.contextmanager
def live_api(port: int = 0) -> Iterator[str]:
    """Serve the real FastAPI app on an ephemeral port for the duration.

    In-process on purpose: the app reads the same module-level ``config`` the
    world just pointed at a throwaway store, so the server and the corpus agree
    without an environment dance.
    """
    import uvicorn

    from palinode.api import server as api_server

    config = uvicorn.Config(api_server.app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not srv.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not srv.started:
            raise RuntimeError("the benchmark API server did not start in time")
        bound = srv.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{bound}"
    finally:
        srv.should_exit = True
        thread.join(timeout=15)


def hook_script_path(dest_dir: str) -> str:
    """Write the shipped per-turn hook to *dest_dir* and return its path.

    Taken from ``palinode.cli.init``, which is the string ``palinode init``
    installs — so the benchmark runs the script a user actually gets, not a
    copy of it that could drift.
    """
    from palinode.hooks import script as _hook_script
    USER_PROMPT_SUBMIT_HOOK_SCRIPT = _hook_script("palinode-user-prompt-submit.sh")

    path = os.path.join(dest_dir, "palinode-user-prompt-submit.sh")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(USER_PROMPT_SUBMIT_HOOK_SCRIPT)
    os.chmod(path, 0o755)
    return path


def hook_context(question: str, *, api_url: str, script: str,
                 env: dict[str, str] | None = None, timeout: int = 30) -> Delivery:
    """Run the actual hook against a live server and return what it injected.

    This is the delivery path a session really has: the script calls
    ``POST /resolve`` under its own millisecond deadline, falls back to
    ``/search`` with an explicit marker when the deadline passes, frames the
    result, and trims at a unit boundary. Nothing here reimplements any of it.
    """
    started = perf_counter()
    environ = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.path.dirname(script),
        "PALINODE_API_URL": api_url,
    }
    environ.update(env or {})
    payload = json.dumps({"prompt": question, "session_id": "bench", "cwd": "/"})
    proc = subprocess.run(
        ["/bin/bash", script], input=payload, capture_output=True, text=True,
        env=environ, timeout=timeout,
    )
    context = ""
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError):
            context = ""
    delivery = _delivery("bundle_via_hook", context, 0, started, DEFAULT_TOP_K, set())
    delivery.extra["hook_exit"] = proc.returncode
    delivery.extra["hook_stderr"] = proc.stderr[-400:]
    return delivery


# ── shared construction ──────────────────────────────────────────────────────


def _delivery(arm: str, context: str, file_reads: int, started: float,
              top_k: int, reasons: set[str],
              extra: dict[str, Any] | None = None) -> Delivery:
    from palinode.core.packing import estimate_tokens

    return Delivery(
        arm=arm,
        context=context,
        tokens=estimate_tokens(context),
        file_reads=file_reads,
        latency_ms=(perf_counter() - started) * 1000.0,
        top_k=top_k,
        coverage={
            "status": "partial" if reasons else "complete",
            "reasons": sorted(reasons),
        },
        extra=extra or {},
    )


__all__ = [
    "ARMS",
    "DEFAULT_TOP_K",
    "MATCHED_LADDER",
    "Delivery",
    "baseline",
    "bounded_evidence",
    "bundle",
    "hook_context",
    "hook_script_path",
    "live_api",
    "matched_budget",
    "projection",
    "seed_rows",
]
