from __future__ import annotations
import logging
import os
from typing import Any, Iterable, Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from palinode.core import store
from palinode.core.config import config
from palinode.core.parity import CATEGORIES, MEMORY_TYPES, TIERS
from palinode.core.tiers import apply_tier
from palinode.core.parser import VALID_EPISTEMICS, VALID_UPDATE_POLICIES
from palinode.core.scope import ScopeChain
from palinode.core.visibility import is_visible
from palinode.api._util import (
    _auto_summary_state, _retrieval_logger, _safe_500, _utc_now,
)
from palinode.api.path_safety import (
    _memory_base_dir, _open_memory_file_text, _resolve_memory_path,
)
from palinode.api.memory_write import _resolve_source
from palinode.core.save import SaveValidationError, save_memory
from palinode.api.rate_limit import (
    _MAX_REQUEST_BYTES, _RATE_LIMIT_WRITE, _check_rate_limit,
)
# _is_description_eligible / _generate_description / _generate_summary /
# _DESCRIPTION_DEFERRED / _fallback_state are reached via the server module
# (`_srv.<name>`) inside generate_summaries_api so test monkeypatches on
# palinode.api.server are honored — see that handler.
from palinode.api.enrichment import _inject_description, _inject_summary
logger = logging.getLogger("palinode.api")
router = APIRouter()


@router.get("/read")
def read_api(
    file_path: str,
    meta: bool = False,
    tier: Literal[*TIERS] | None = None,
) -> dict[str, Any]:
    from palinode.core import parser

    candidates = [file_path]
    if not file_path.endswith(".md"):
        candidates.append(f"{file_path}.md")

    # L5: open candidates directly with O_NOFOLLOW (POSIX) so a symlink swap
    # within memory_dir between the existence check and the open cannot
    # redirect us to a sensitive file. _resolve_memory_path already keeps
    # us inside memory_dir; this closes the residual symlink-swap window.
    # Falls back to a try-open for non-POSIX platforms.
    resolved = ""
    content = ""
    for candidate in candidates:
        _, resolved_candidate = _resolve_memory_path(candidate)
        try:
            content = _open_memory_file_text(resolved_candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _safe_500(exc, "File read failed")
        file_path = candidate
        resolved = resolved_candidate
        break

    if not resolved:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        # `meta` keeps parse_markdown as its source so the frontmatter shape
        # callers already depend on is untouched; the abstract tier only needs
        # the raw frontmatter, so it uses the cheaper splitter when `meta` is
        # off rather than paying for chunking.
        metadata: dict[str, Any] | None = None
        if meta:
            metadata, _ = parser.parse_markdown(content)
        elif tier == "abstract":
            metadata, _ = parser.parse_frontmatter(content)

        # `size_bytes` stays the size of the FILE, not of the tiered view —
        # a caller asking for an abstract still wants to know what opening
        # the full record would cost.
        result = {
            "file": file_path,
            "content": apply_tier(tier, content, metadata),
            "size_bytes": len(content.encode("utf-8")),
        }
        if tier is not None:
            result["tier"] = tier

        if meta:
            result["frontmatter"] = metadata

        # Issue emit retrieval event (explicit — direct /read call).
        _retrieval_logger.record_file_read(
            file_path,
            source="palinode_read",
            mode="explicit",
        )

        # ADR-006/007: persist access metadata for the read file's chunks.
        # Resilient by contract — record_recall_for_paths never raises.
        # Use the resolved (absolute) path: index_file stores absolute paths in
        # chunks.file_path, so a relative-path lookup silently matches nothing.
        store.record_recall_for_paths([resolved])

        return result
    except HTTPException:
        # Path / 404 errors should propagate untouched — they are not 500s.
        raise
    except (ValueError, KeyError) as e:
        # Frontmatter parser failures are 500s with a safe message.
        raise _safe_500(e, "File read failed")


class SaveRequest(BaseModel):
    content: str
    type: Literal[*MEMORY_TYPES]
    slug: str | None = None
    entities: list[str] | None = None
    metadata: Any | None = None
    core: bool | None = None
    source: str | None = None
    confidence: float | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    #: Optional human-readable title.  When set, it's stored in frontmatter
    #: and used for display in lists/search results. ADR-010.
    title: str | None = None
    #: Sugar: ``project="foo"`` is equivalent to appending ``"project/foo"``
    #: to ``entities``. ADR-010. If both are given and there's a
    #: mismatch, both values land — same as supplying ``entities=["project/a",
    #: "project/b"]`` directly.
    project: str | None = None
    #: Optional dict of SDLC object references (GitLab MR/issue/pipeline,
    #: GitHub PR, Linear, Jira, etc.).  Free-form key/value pairs — recognised
    #: keys get pretty rendering; others pass through unchanged.
    #: Typed as Any-value so Pydantic doesn't reject nested values before
    #: our parser helper can soft-warn and drop them.
    external_refs: dict[str, Any] | None = None
    #: ADR-015 §2.1: write-semantics axis, orthogonal to ``type``.
    #: ``append`` (default) keeps today's episodic behaviour; ``replace`` marks
    #: the memory as a living/current-state document (consolidation must never
    #: SUPERSEDE/ARCHIVE-into-history it). Persisted as sticky frontmatter so the
    #: file declares its own regime. Does NOT change append's clobber behaviour
    #: in this PR — a same-slug save still overwrites in place (§2.6 guard
    #: deferred). Validated against ``VALID_UPDATE_POLICIES`` in the handler —
    #: see the note on ``epistemic`` for why this is ``str`` and not a
    #: ``Literal``; ``json_schema_extra`` advertises the enum for parity.
    update_policy: str | None = Field(
        default=None, json_schema_extra={"enum": list(VALID_UPDATE_POLICIES)}
    )
    #: Source-citation anchors. A list of ``{ref, quote, quote_hash}``
    #: dicts: ``ref`` is a path under the memory dir, ``quote`` is the exact
    #: cited passage, and ``quote_hash`` (optional) is the integrity hash of the
    #: quote. When omitted the hash is computed on save; when present it is
    #: validated against the quote and a mismatch is rejected (HTTP 400). Read
    #: back by the quote verifier (``palinode.core.quote_verify``). Typed as
    #: Any so Pydantic doesn't reject malformed input before our normalizer can
    #: return a clean 400 — the outer list included. It used to be annotated
    #: ``list[dict[str, Any]]``, which honoured that intent for the *values*
    #: inside each anchor but not for the list itself: a non-list ``sources``
    #: was rejected by Pydantic with a 422 before ``_normalize_sources`` could
    #: say "sources must be a list", so the identical input produced a clean
    #: message through ``core.save.save_memory`` and a validation blob over
    #: HTTP. ``json_schema_extra`` keeps the real shape in the OpenAPI document,
    #: the same way ``epistemic`` below advertises its enum while staying
    #: loosely typed for the sake of the error message.
    sources: Any | None = Field(
        default=None,
        json_schema_extra={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "quote": {"type": "string"},
                    "quote_hash": {"type": "string"},
                },
                "required": ["ref", "quote"],
            },
        },
    )
    #: (ADR-018): epistemic marker — the KIND of claim this memory makes
    #: (``fact`` / ``inference`` / ``open_question`` / ``unverified``),
    #: orthogonal to ``type``.
    #: Validated against ``VALID_EPISTEMICS``. When omitted the memory is
    #: ``unmarked`` (``DEFAULT_EPISTEMIC``) — no epistemic claim, NOT a fact — and
    #: no frontmatter is written, so existing memories are byte-for-byte
    #: unaffected. Like ``status``, it may also arrive via the ``metadata`` dict;
    #: the explicit param wins.
    #: Typed ``str`` rather than ``Literal[*VALID_EPISTEMICS]`` deliberately: a
    #: bad value must reach the handler so it returns a 400 with an actionable
    #: message, not Pydantic's 422. ``json_schema_extra`` advertises the enum in
    #: the OpenAPI document anyway, so the ADR-010 parity contract can assert
    #: this surface declares the same values as CLI/MCP/plugin.
    epistemic: str | None = Field(
        default=None, json_schema_extra={"enum": list(VALID_EPISTEMICS)}
    )
    #: (G4): typed relationship links, orthogonal to supersession.
    #: ``contradicts`` records a conflict with no winner picked (surfaced by
    #: ``lint`` as a health signal); ``backed_by`` records an evidence/support
    #: edge to a source or fact. Both are plaintext frontmatter lists of
    #: ``category/slug`` refs. Typed as Any so Pydantic doesn't reject malformed
    #: input before ``_normalize_link_refs`` can return a clean 400.
    #: ``json_schema_extra`` carries the real shape into the OpenAPI document,
    #: so the loose annotation costs the schema nothing. A bare string is
    #: accepted as one-ref sugar (``normalize_link_refs`` coerces it), which is
    #: why the schema advertises both forms rather than array-only.
    contradicts: Any | None = Field(
        default=None,
        json_schema_extra={
            "oneOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "string"},
            ]
        },
    )
    backed_by: Any | None = Field(
        default=None,
        json_schema_extra={
            "oneOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "string"},
            ]
        },
    )
    #: Claim-level source anchors — the unsigned claim_id layer (public
    #: issue Q1). A list of ``{claim_id?, text, source_id, span, anchor_id?}``
    #: dicts binding a claim *inside* this memory to the source span that
    #: justifies it: ``text`` is the claim as stated, ``source_id`` is a
    #: sources[].ref-style path under the memory dir, and ``span`` reuses the
    #: ``{quote, quote_hash}`` anchor verbatim (hash computed/verified on
    #: save). ``claim_id`` is content-addressed (derived from the memory ref +
    #: normalized claim text) — derived when omitted, verified when supplied.
    #: Composes with (does not replace) file-level identity and the
    #: ``sources:`` integrity anchors. Typed as Any so Pydantic doesn't reject
    #: malformed input before our normalizer can return a clean 400;
    #: ``json_schema_extra`` keeps the shape in the OpenAPI document.
    claims: Any | None = Field(
        default=None,
        json_schema_extra={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "text": {"type": "string"},
                    "source_id": {"type": "string"},
                    "span": {"type": "object"},
                    "anchor_id": {"type": "string"},
                },
                "required": ["text", "source_id", "span"],
            },
        },
    )


_DEFAULT_LIST_SKIP_DIRS = frozenset({"daily", "archive", "inbox", "logs", "prompts"})


def collect_memory_files(
    category: str | None = None,
    core_only: bool = False,
    scope_chain: ScopeChain | None = None,
    *,
    skip_dirs: Iterable[str] | None = None,
    include_history: bool = True,
) -> list[dict[str, Any]]:
    """Enumerate memory files as /list-shaped rows, newest first.

    The shared selection path behind ``GET /list``, the /context/prime
    endpoint (ADR-009 Layer 1/2), and the provenance UI's memory list, sidebar
    count, and Quality queues. Every row goes through the visibility choke
    point (:func:`palinode.core.visibility.is_visible`):

    - With a ``scope_chain`` carrying an identity level, off-chain explicit
      ``scope:``, off-chain-owned ``private``, and non-intersecting
      ``restricted`` memories are all dropped (scoped mode).
    - With ``scope_chain=None`` — the ``GET /list`` contract — scope
      isolation does not apply, but **access control still does**:
      ``private`` and ``restricted`` memories are never listed.
      That gate is load-bearing, not decorative: the shipped SessionStart
      hook injects from ``GET /list?core_only=true``, so without it a
      ``core: true`` memory marked ``private`` would be auto-injected into
      every session on the machine. A second, gate-free walk would reopen the
      same hole for whichever caller wrote it — every caller belongs here.

    Frontmatter parsed here is passed to the choke point directly — it is
    live (just read from disk), so no second read is needed.

    ``skip_dirs`` overrides the default top-level skip-dir set (``daily``,
    ``archive``, ``inbox``, ``logs``, ``prompts``) — the provenance UI adds
    ``.obsidian``. ``include_history`` controls whether ``-history.md``
    consolidation-audit siblings are included; the default (``True``)
    preserves the classic ``GET /list`` contract, and the UI passes ``False``
    since those siblings aren't browsable memories.
    """
    import glob
    from palinode.core import parser

    results = []
    base_dir = _memory_base_dir()
    search_pattern = os.path.join(base_dir, "**/*.md")

    effective_skip_dirs = set(skip_dirs) if skip_dirs is not None else _DEFAULT_LIST_SKIP_DIRS

    for filepath in glob.glob(search_pattern, recursive=True):
        try:
            if os.path.commonpath([base_dir, os.path.realpath(filepath)]) != base_dir:
                continue
        except ValueError:
            continue
        rel_path = os.path.relpath(filepath, base_dir)
        parts = rel_path.split(os.sep)

        if parts[0] in effective_skip_dirs:
            continue

        if not include_history and parts[-1].endswith("-history.md"):
            continue

        if category and parts[0] != category:
            continue

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            metadata, _ = parser.parse_frontmatter(content)

            is_core = bool(metadata.get("core", False))
            if core_only and not is_core:
                continue

            if not is_visible(scope_chain, filepath, metadata=metadata):
                continue

            raw_scope = metadata.get("scope")
            explicit_scope = (
                raw_scope.strip()
                if isinstance(raw_scope, str) and raw_scope.strip()
                else None
            )

            results.append({
                "file": rel_path,
                "name": metadata.get("name") or parts[-1].replace('.md', ''),
                "title": metadata.get("title"),
                "type": metadata.get("type"),
                "category": metadata.get("category", parts[0]),
                "core": is_core,
                "scope": explicit_scope,
                "summary": metadata.get("summary", ""),
                "last_updated": metadata.get("last_updated", ""),
                "entities": metadata.get("entities", []),
                "size_bytes": os.path.getsize(filepath)
            })
        except Exception:
            pass

    # Sort newest first so listing surfaces recent activity.
    # `last_updated` may be a string (typical) or a datetime (yaml auto-converts
    # ISO timestamps without quotes); stringify in the key so mixed types don't
    # raise. Empty string sorts last in descending order — correct for files
    # with missing or malformed frontmatter.
    results.sort(key=lambda r: str(r.get("last_updated") or ""), reverse=True)
    return results


@router.get("/list")
def list_api(
    category: Literal[*CATEGORIES] | None = None, core_only: bool = False
) -> list[dict[str, Any]]:
    """Browse memories, newest first.

    Never scope-filters (no session chain here — that's /context/prime's job),
    but ``private`` and ``restricted`` memories are always withheld: this is
    the endpoint the SessionStart hook injects from, so access control cannot
    be optional here.
    """
    return collect_memory_files(category=category, core_only=core_only)


@router.post("/save")
def save_api(
    req: SaveRequest, request: Request = None, sync: bool = False,
    push: bool | None = None,
) -> dict[str, Any]:
    """Create a typed memory file and commit it to git.

    Request body (see ``SaveRequest`` model for full schema):

    .. code-block:: json

        {
          "content": "Markdown body of the memory.",
          "type": "Decision",
          "slug": "optional-url-safe-name",
          "entities": ["person/alice", "project/my-app"],
          "title": "Optional human-readable title"
        }

    Required fields are ``content`` and ``type``. The ``type`` value selects
    the destination directory (``Decision`` → ``decisions/``, ``Insight`` →
    ``insights/``, etc.). The ``category`` field is **not** part of this
    schema — it is *derived* from ``type``. The body field is ``content``,
    not ``body``.

    Size limit: request bodies are capped at ``PALINODE_MAX_REQUEST_BYTES``
    (default ``5242880`` = 5 MB). Saves over the limit return HTTP 413.

    Query params:
        sync: If True, runs the write-time contradiction check (tier 2a, ADR-004)
              inline and returns its result. If False (default), the check is
              enqueued for background processing and the response returns as
              soon as the file is written and git-committed.
        push: Per-call override for the auto-push decision below. ``None``
              (default) defers to ``config.git.auto_push``, unchanged from
              before this parameter existed. An explicit ``False`` suppresses
              this save's auto-push even when ``config.git.auto_push`` is on
              — the override session-end's own ``push=False`` now threads
              through to the individual-file save it makes internally, which
              previously ran this same auto-push independently and un-
              suppressibly (the bug the write choke-point work surfaced: the
              raw ``subprocess.run`` this auto-push used to call was invisible
              to any test asserting on ``git_tools.push``, so a session-end
              caller's explicit ``push=False`` silently did not cover it).
              ``True`` is accepted but redundant with a caller (like
              session-end) that already pushes explicitly afterward.

    The capability itself is :func:`palinode.core.save.save_memory`; what
    remains here is the transport: rate limiting, the request-size cap, the
    ``X-Palinode-Source`` header lookup, and the mapping of the capability's
    ``SaveValidationError`` onto HTTP 400.
    """
    if request:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip, "write", _RATE_LIMIT_WRITE):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if len(req.content) > _MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Content too large")

    payload = req.model_dump()
    # ADR-010 levels 1-2 (explicit field, then header) are transport-resolved;
    # the capability applies levels 3-4 to whatever it is handed.
    payload["source"] = _resolve_source(req.source, request)
    try:
        return save_memory(**payload, sync=sync, push=push)
    except SaveValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))



@router.post("/generate-summaries")
def generate_summaries_api() -> dict[str, Any]:
    """Backfill missing auto-enrichment (descriptions + summaries) for files.

    Scans all markdown files under ``palinode_dir``:

    - **Descriptions**: any file missing a ``description`` field gets one
      generated via Ollama. Descriptions are not core-gated — every memory gets
      one, mirroring the prior inline behavior that moved off the /save hot
      path. Skipped entirely when ``auto_summary.enabled`` is False.
    - **Summaries**: files with ``core: true`` and no ``summary`` get one.

    This endpoint is the watcher-driven backfill that lands both enrichments
    after /save returns fast. Despite the name, it fills both —
    the name is kept for API/MCP/CLI parity with the shipped surface.

    Populates _auto_summary_state for /status and /health/auto-summary
    observability. Errors are counted but never raised — a stalled Ollama
    produces non-zero error counts / last_error, not an HTTP failure, so the
    watcher debounce keeps working.
    """
    import glob
    import time as _time
    from palinode.core import parser
    # Late lookup on the server module so tests that
    # `patch("palinode.api.server._generate_description" / "_generate_summary"
    # / "_is_description_eligible")` intercept these calls. The names are
    # re-exported from palinode.api.server; a bare local binding (the import at
    # module top) would not see a monkeypatch applied to the server module.
    # Deferred import avoids the server↔routers import cycle at module load.
    import palinode.api.server as _srv

    started = _time.monotonic()
    count = 0
    errors = 0
    desc_count = 0
    desc_errors = 0
    last_error: str | None = None
    describe_enabled = config.auto_summary.enabled
    # reset the CHAT-fallback budget for this backfill run. Bounds how many
    # deferred files may escalate to the OpenAI-compat shim in a single walk so a
    # chronically-down local chat host can't fan the whole backlog out to
    # Anthropic. No-op unless auto_summary.llm_fallbacks is configured.
    # Reset (and the _srv._generate_* calls below) target the server module's
    # binding so the CHAT-fallback budget reset and its consumption share one
    # _fallback_state, and a test that reads server._fallback_state after calling
    # this endpoint observes the reset.
    _srv._fallback_state["remaining"] = config.auto_summary.llm_fallback_max_per_run
    # Use palinode_dir since that's generally where memories are kept
    for filepath in glob.glob(os.path.join(config.palinode_dir, "**/*.md"), recursive=True):
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
            metadata, _ = parser.parse_frontmatter(content)

            # backfill the deferred auto-description. Not core-gated —
            # every *eligible* memory file gets a description, matching the
            # inline behavior moved async. gate on
            # _is_description_eligible so structural / non-memory files
            # (daily/, archive/, specs/, top-level docs) — whose write-back is a
            # no-op — aren't reprocessed every run forever. _generate_description
            # never raises: it returns the _DESCRIPTION_DEFERRED sentinel when
            # Ollama is slow / circuit-open (count as a transient error; the
            # watcher retries) or a string (LLM result or first-line fallback).
            _rel = os.path.relpath(filepath, config.palinode_dir)
            if (
                describe_enabled
                and not metadata.get("description")
                and _srv._is_description_eligible(_rel)
            ):
                desc = _srv._generate_description(content)
                # Compare against the server module's sentinel: _generate_description
                # is reached via _srv (so a test patch is honored), and when
                # unpatched it returns server's _DESCRIPTION_DEFERRED identity.
                if desc is _srv._DESCRIPTION_DEFERRED:
                    desc_errors += 1
                    last_error = f"description deferred (ollama slow) for {os.path.basename(filepath)}"
                elif desc:
                    _inject_description(filepath, desc)
                    desc_count += 1
                    logger.info(f"Generated description for {filepath}")
                else:
                    desc_errors += 1
                    last_error = f"empty description for {os.path.basename(filepath)}"

            if not metadata.get("core"):
                continue
            if metadata.get("summary"):
                continue  # Already has summary

            summary = _srv._generate_summary(content)
            if summary:
                _inject_summary(filepath, summary)
                count += 1
                logger.info(f"Generated summary for {filepath}")
            else:
                # _generate_summary returns "" on LLM failure (logged inside).
                # Track it as an error for observability without re-raising.
                errors += 1
                last_error = f"empty summary for {os.path.basename(filepath)}"
        except Exception as e:
            errors += 1
            last_error = f"{type(e).__name__}: {e}"[:200]
            logger.warning(f"Enrichment generation failed for {filepath}: {e}")

    duration_ms = int((_time.monotonic() - started) * 1000)
    _auto_summary_state["last_run_at"] = _utc_now().isoformat().replace("+00:00", "Z")
    _auto_summary_state["last_run_duration_ms"] = duration_ms
    _auto_summary_state["last_run_count"] = count
    _auto_summary_state["last_run_errors"] = errors
    _auto_summary_state["last_run_descriptions"] = desc_count
    _auto_summary_state["last_run_description_errors"] = desc_errors
    if last_error is not None:
        _auto_summary_state["last_error"] = last_error
    _auto_summary_state["total_runs"] += 1
    _auto_summary_state["total_errors"] += errors + desc_errors

    return {
        "status": "success",
        "summaries_generated": count,
        "errors": errors,
        "descriptions_generated": desc_count,
        "description_errors": desc_errors,
        "duration_ms": duration_ms,
    }
