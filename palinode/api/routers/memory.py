from __future__ import annotations
import hashlib
import logging
import os
import re
import subprocess
import time
import yaml
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from palinode.core import store, git_tools
from palinode.core.config import config
from palinode.core.scope import ScopeChain, chain_allows
from palinode.api._util import (
    _auto_summary_state, _retrieval_logger, _safe_500, _utc_now,
)
from palinode.api.path_safety import (
    _memory_base_dir, _open_memory_file_text, _resolve_memory_path,
)
from palinode.api.memory_write import (
    _TYPE_TO_CATEGORY, _apply_wiki_footer, _normalize_entities, _resolve_source,
)
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


def _normalize_sources(raw: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Validate and normalize ``sources:`` quote anchors for save (#459).

    Each entry must be a dict with non-empty ``ref`` and ``quote``. The
    ``quote_hash`` is computed when absent and validated when present — a stored
    hash that does not match its quote is a tampered/inconsistent anchor and is
    rejected. Raises ``HTTPException(400)`` on any malformed input; returns the
    normalized list of ``{ref, quote, quote_hash}`` dicts otherwise.
    """
    from palinode.core.quote_verify import quote_hash as _quote_hash

    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="sources must be a list")

    normalized: list[dict[str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=400, detail=f"sources[{i}] must be an object"
            )
        ref = entry.get("ref")
        quote = entry.get("quote")
        if not isinstance(ref, str) or not ref.strip():
            raise HTTPException(
                status_code=400, detail=f"sources[{i}] missing non-empty 'ref'"
            )
        if not isinstance(quote, str) or not quote.strip():
            raise HTTPException(
                status_code=400, detail=f"sources[{i}] missing non-empty 'quote'"
            )
        ref = ref.strip()
        computed = _quote_hash(quote)
        supplied = entry.get("quote_hash")
        if supplied is not None and str(supplied).strip():
            if str(supplied).strip() != computed:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"sources[{i}] quote_hash does not match its quote "
                        "(inconsistent anchor)"
                    ),
                )
        normalized.append({"ref": ref, "quote": quote, "quote_hash": computed})
    return normalized


def _normalize_link_refs(raw: Any, field: str) -> list[str]:
    """Validate a typed-link ref list (#533), raising HTTP 400 on malformed input.

    Thin wrapper over :func:`palinode.core.typed_links.normalize_link_refs` that
    maps the core ``TypedLinkError`` to ``HTTPException(400)`` — mirroring how
    ``_normalize_sources`` rejects malformed anchors at the save boundary.
    """
    from palinode.core.typed_links import TypedLinkError, normalize_link_refs

    try:
        return normalize_link_refs(raw, field)
    except TypedLinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _normalize_claims_or_400(raw: Any, memory_ref: str) -> list[dict[str, Any]]:
    """Validate claim-level source anchors, raising HTTP 400 on malformed input.

    Thin wrapper over :func:`palinode.core.claims.normalize_claims` that maps
    the core ``ClaimError`` to ``HTTPException(400)`` — the same boundary
    discipline as ``_normalize_sources`` and ``_normalize_link_refs``.
    """
    from palinode.core.claims import ClaimError, normalize_claims

    try:
        return normalize_claims(raw, memory_ref)
    except ClaimError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/read")
def read_api(file_path: str, meta: bool = False) -> dict[str, Any]:
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
        result = {
            "file": file_path,
            "content": content,
            "size_bytes": len(content.encode("utf-8")),
        }

        if meta:
            metadata, _ = parser.parse_markdown(content)
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
    type: str
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
    #: deferred). Validated against ``VALID_UPDATE_POLICIES``.
    update_policy: str | None = None
    #: Source-citation anchors. A list of ``{ref, quote, quote_hash}``
    #: dicts: ``ref`` is a path under the memory dir, ``quote`` is the exact
    #: cited passage, and ``quote_hash`` (optional) is the integrity hash of the
    #: quote. When omitted the hash is computed on save; when present it is
    #: validated against the quote and a mismatch is rejected (HTTP 400). Read
    #: back by the quote verifier (``palinode.core.quote_verify``). Typed as
    #: Any-value so Pydantic doesn't reject malformed input before our
    #: normalizer can return a clean 400.
    sources: list[dict[str, Any]] | None = None
    #: (ADR-018): epistemic marker — the KIND of claim this memory makes
    #: (``fact`` / ``inference`` / ``open_question`` / ``unverified``),
    #: orthogonal to ``type``.
    #: Validated against ``VALID_EPISTEMICS``. When omitted the memory is
    #: ``unmarked`` (``DEFAULT_EPISTEMIC``) — no epistemic claim, NOT a fact — and
    #: no frontmatter is written, so existing memories are byte-for-byte
    #: unaffected. Like ``status``, it may also arrive via the ``metadata`` dict;
    #: the explicit param wins.
    epistemic: str | None = None
    #: (G4): typed relationship links, orthogonal to supersession.
    #: ``contradicts`` records a conflict with no winner picked (surfaced by
    #: ``lint`` as a health signal); ``backed_by`` records an evidence/support
    #: edge to a source or fact. Both are plaintext frontmatter lists of
    #: ``category/slug`` refs. Typed as Any so Pydantic doesn't reject malformed
    #: input before ``_normalize_link_refs`` can return a clean 400.
    contradicts: Any | None = None
    backed_by: Any | None = None
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
    #: malformed input before our normalizer can return a clean 400.
    claims: Any | None = None


def collect_memory_files(
    category: str | None = None,
    core_only: bool = False,
    scope_chain: ScopeChain | None = None,
) -> list[dict[str, Any]]:
    """Enumerate memory files as /list-shaped rows, newest first.

    The shared selection path behind ``GET /list`` and the /context/prime
    endpoint (ADR-009 Layer 1). When ``scope_chain`` is given, files whose
    explicit ``scope:`` frontmatter is not on the chain are dropped
    (scoped mode); ``None`` skips scope filtering entirely (classic mode,
    the /list contract).
    """
    import glob
    from palinode.core import parser

    results = []
    base_dir = _memory_base_dir()
    search_pattern = os.path.join(base_dir, "**/*.md")

    skip_dirs = {"daily", "archive", "inbox", "logs", "prompts"}

    for filepath in glob.glob(search_pattern, recursive=True):
        try:
            if os.path.commonpath([base_dir, os.path.realpath(filepath)]) != base_dir:
                continue
        except ValueError:
            continue
        rel_path = os.path.relpath(filepath, base_dir)
        parts = rel_path.split(os.sep)

        if parts[0] in skip_dirs:
            continue

        if category and parts[0] != category:
            continue

        try:
            with open(filepath, "r") as f:
                content = f.read()
            metadata, _ = parser.parse_markdown(content)

            is_core = bool(metadata.get("core", False))
            if core_only and not is_core:
                continue

            if scope_chain is not None and not chain_allows(scope_chain, metadata):
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
def list_api(category: str | None = None, core_only: bool = False) -> list[dict[str, Any]]:
    return collect_memory_files(category=category, core_only=core_only)


@router.post("/save")
def save_api(req: SaveRequest, request: Request = None, sync: bool = False) -> dict[str, Any]:
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
    not ``body``. See #299 for the history.

    Size limit: request bodies are capped at ``PALINODE_MAX_REQUEST_BYTES``
    (default ``5242880`` = 5 MB). Saves over the limit return HTTP 413.

    Query params:
        sync: If True, runs the write-time contradiction check (tier 2a, ADR-004)
              inline and returns its result. If False (default), the check is
              enqueued for background processing and the response returns as
              soon as the file is written and git-committed.
    """
    if request:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip, "write", _RATE_LIMIT_WRITE):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if len(req.content) > _MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Content too large")
    slug = req.slug
    if slug:
        # Prevent any potential JSON escape or traversal exploits if user defines slug
        slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')

    if not slug:
        slug = re.sub(r'[^a-z0-9]+', '-', req.content.split('\n')[0].lower()[:30]).strip('-')
        if not slug:
            slug = str(int(time.time()))

    # module-level map (shared with the description-eligibility predicate
    # so the writer and the count/worklist derive from one literal).
    category = _TYPE_TO_CATEGORY.get(req.type, "inbox")

    # ADR-015 §2.1: validate the write-semantics axis. Reject an
    # unknown update_policy outright rather than silently coercing — a typo'd
    # policy ("repalce") must not quietly fall back to append and leave a
    # living document mis-declared.
    from palinode.core.parser import (
        VALID_EPISTEMICS as _VALID_EPISTEMICS,
        VALID_STATUSES as _VALID_STATUSES,
        VALID_UPDATE_POLICIES as _VALID_UPDATE_POLICIES,
    )
    # H4: update_policy may arrive via the first-class param OR the `metadata`
    # dict (which is merged verbatim into frontmatter below). Validating only
    # the param let a metadata-supplied value land unvalidated and silently arm
    # the executor replace-guard (executor.py `_is_replace_policy` reads the
    # frontmatter key). Resolve the effective value from both — the explicit
    # param wins — and validate that. `status` already does this; mirror it.
    _meta_update_policy = None
    if req.metadata and isinstance(req.metadata, dict):
        _meta_update_policy = req.metadata.get("update_policy")
    _effective_update_policy = (
        req.update_policy if req.update_policy is not None else _meta_update_policy
    )
    if (
        _effective_update_policy is not None
        and _effective_update_policy not in _VALID_UPDATE_POLICIES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid update_policy {_effective_update_policy!r}; "
                f"expected one of {list(_VALID_UPDATE_POLICIES)}"
            ),
        )

    # ADR-015 §2.2: validate a writer-supplied `status` against the
    # combined lifecycle + incident allow-set. `status` is shared with the
    # store's search-exclusion (`config.search.exclude_status`), so a typo'd
    # status that landed in frontmatter could silently mis-classify a memory
    # for recall. Reject unknown values at the surface. The status may arrive
    # via the `metadata` dict (req.metadata["status"]); validate there too.
    _req_status = None
    if req.metadata and isinstance(req.metadata, dict):
        _req_status = req.metadata.get("status")
    if _req_status is not None and _req_status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid status {_req_status!r}; "
                f"expected one of {list(_VALID_STATUSES)}"
            ),
        )

    # (ADR-018): validate the epistemic marker. Like update_policy/status it
    # may arrive via the first-class param OR the `metadata` dict (merged verbatim
    # into frontmatter below) — resolve the effective value from both (the param
    # wins) and validate that, so a metadata-supplied typo ("inferrence") can't
    # land unvalidated. The effective value is written from this single var below.
    _meta_epistemic = None
    if req.metadata and isinstance(req.metadata, dict):
        _meta_epistemic = req.metadata.get("epistemic")
    _effective_epistemic = (
        req.epistemic if req.epistemic is not None else _meta_epistemic
    )
    if (
        _effective_epistemic is not None
        and _effective_epistemic not in _VALID_EPISTEMICS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid epistemic {_effective_epistemic!r}; "
                f"expected one of {list(_VALID_EPISTEMICS)}"
            ),
        )

    # Security scan: reject prompt injection and exfiltration attempts
    is_safe, reason = store.scan_memory_content(req.content)
    if not is_safe:
        raise HTTPException(status_code=400, detail=f"Security scan failed: {reason}")

    file_path = os.path.join(config.palinode_dir, category, f"{slug}.md")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    content_hash = hashlib.sha256(req.content.encode()).hexdigest()

    # Normalize entity refs: bare strings get a category prefix.
    # e.g. "palinode" → "project/palinode", "alice" → "person/alice"
    raw_entities = list(req.entities or [])
    # ADR-010: ``project`` is sugar for the ``project/<slug>`` entity.
    if req.project:
        project_ref = req.project if "/" in req.project else f"project/{req.project}"
        if project_ref not in raw_entities:
            raw_entities.append(project_ref)
    normalized_entities = _normalize_entities(raw_entities, category)

    # Capture a single UTC timestamp for both created_at and last_updated so
    # that they are identical on first write (file must not be born stale).
    _now_iso = _utc_now().isoformat()
    # ADR-015 §2.4: preserve first-seen on existing-slug overwrite. Today
    # every save re-stamps both created_at and last_updated to now, destroying
    # first-seen for any re-saved fact and turning a living document born-again
    # on each update. When the target path already exists, carry its existing
    # created_at forward; only last_updated advances to now. A genuinely new
    # file still stamps created_at = now.
    #
    # This is deliberately NOT gated behind update_policy (that param is PR-B):
    # re-saving the same (category, slug) is the same logical memory, so its
    # birth timestamp should be preserved regardless of write policy.
    #
    # Fallback: if an existing file lacks created_at in its frontmatter, leave
    # today's behaviour (stamp now). A git-log first-commit lookup is the
    # principled fallback (ADR-015 §2.4) but is deferred to a later refinement.
    created_at = _now_iso
    # ADR-015 §2.1 / §6 Q2 (both param + sticky field): the explicit param wins;
    # otherwise carry forward the existing file's sticky update_policy so the
    # file's declared regime survives a save that omits the param. A genuinely
    # new file with no param resolves to the DEFAULT_UPDATE_POLICY (append).
    # H4: resolve from param-or-metadata (validated above); the param wins.
    update_policy = _effective_update_policy
    if os.path.exists(file_path):
        try:
            from palinode.core import parser as _parser
            with open(file_path, "r") as _existing:
                _existing_meta, _ = _parser.parse_markdown(_existing.read())
            _prior_created = _existing_meta.get("created_at")
            if _prior_created:
                created_at = str(_prior_created)
            # Sticky carry-forward: if the caller didn't supply update_policy,
            # inherit the value the file already declares.
            if update_policy is None:
                _prior_policy = _existing_meta.get("update_policy")
                if _prior_policy in _VALID_UPDATE_POLICIES:
                    update_policy = str(_prior_policy)
            # (ADR-018): epistemic is sticky for the same reason — re-saving
            # the same (category, slug) is the same logical memory, and a save
            # that omits the marker must NOT silently downgrade a deliberate
            # `open_question`/`inference` back to the `fact` default. Inherit the
            # file's existing marker when the caller didn't supply one (param or
            # metadata). The prior value was validated at its own save; the
            # membership guard makes re-validation unnecessary.
            if _effective_epistemic is None:
                _prior_epistemic = _existing_meta.get("epistemic")
                if _prior_epistemic in _VALID_EPISTEMICS:
                    _effective_epistemic = str(_prior_epistemic)
        except (OSError, ValueError) as exc:
            # Unreadable/unparseable existing file: fail open to today's
            # behaviour (stamp now) rather than block the save. The overwrite
            # itself proceeds normally below.
            logger.warning(
                "Could not read existing created_at for %r (%s); stamping now",
                file_path,
                exc,
            )
    frontmatter_dict = {
        "id": f"{category}-{slug}",
        "category": category,
        "type": req.type,
        "entities": normalized_entities,
        "content_hash": content_hash,
        # write proper timezone-aware UTC ISO-8601 (`+00:00` suffix).
        # Previously used ``time.strftime("...%Z")`` which emitted local time
        # with a ``Z`` (UTC) marker — a mismatch that made `chunks.created_at`
        # unreliable as a recency signal.
        # ADR-015 §2.4: created_at is preserved across overwrites (see above).
        "created_at": created_at,
        # populate last_updated on initial write so the file isn't born
        # stale.  The freshness checker treats a missing last_updated as stale;
        # setting it equal to created_at on first save avoids that false positive.
        # On re-saves the indexer re-reads frontmatter and this value is refreshed.
        "last_updated": _now_iso,
    }
    if req.metadata:
        # H4: don't let raw, unvalidated fields from the metadata dict land in
        # frontmatter — `update_policy`, `epistemic`, and the typed
        # link fields `contradicts`/`backed_by` are each resolved +
        # validated above/below and written from their own normalized values, so a
        # malformed value tunneled through metadata still gets a clean 400.
        _verbatim_excluded = {"update_policy", "epistemic", "contradicts", "backed_by", "claims"}
        frontmatter_dict.update(
            {k: v for k, v in req.metadata.items() if k not in _verbatim_excluded}
        )
    # ADR-015 §2.3: ephemeral TTL. A metadata-supplied `ttl` (duration)
    # resolves to an absolute `expires_at`; an explicit `expires_at` is
    # validated. Both arrive via the free-form `metadata` dict (merged above),
    # so this single normalization is inherited by every interface. The
    # `archive-expired` sweep later flips an expired memory to status: archived.
    from palinode.consolidation.ttl import normalize_expiry as _normalize_expiry
    _expiry_err = _normalize_expiry(frontmatter_dict, now_iso=_now_iso)
    if _expiry_err:
        raise HTTPException(status_code=400, detail=_expiry_err)
    if req.core is not None:
        frontmatter_dict["core"] = req.core
    if req.confidence is not None:
        frontmatter_dict["confidence"] = req.confidence
    if req.priority is not None:
        frontmatter_dict["priority"] = req.priority
    # (ADR-018): persist the epistemic marker only when one is in effect —
    # supplied now (param or metadata) OR inherited from the file's prior save
    # (sticky carry-forward above). A memory that was NEVER marked keeps clean
    # frontmatter and reads as `unmarked` (no claim — NOT fact), so files
    # predating this field are byte-for-byte unaffected; but once a marker is set
    # it survives re-saves that omit it, so a `fact`/`inference`/`open_question`
    # is never silently dropped back to unmarked. The value written here was
    # validated (caller-supplied) or membership-checked (inherited) above.
    if _effective_epistemic is not None:
        frontmatter_dict["epistemic"] = _effective_epistemic
    # ADR-015 §2.1: persist the resolved write-semantics axis as sticky
    # frontmatter so the file declares its own regime. Only written when the
    # caller declared a policy (now or on a prior save that was carried
    # forward) — saves that never opt in keep clean frontmatter and append
    # remains the implicit default. H4: a metadata-supplied `update_policy` was
    # excluded from the merge above and folded into this single resolved +
    # validated `update_policy` var, so the value written here is always one the
    # surface validated.
    if update_policy is not None:
        frontmatter_dict["update_policy"] = update_policy
    # IETF KU frontmatter alignment — auto-populate KU fields when
    # ku_compat is enabled, or when the caller explicitly provides them.
    if config.ku_compat.enabled:
        if "ku_version" not in frontmatter_dict:
            frontmatter_dict["ku_version"] = config.ku_compat.ku_version
        if "lifecycle" not in frontmatter_dict:
            raw_status = frontmatter_dict.get("status") or (req.metadata or {}).get("status", "active")
            from palinode.core.parser import VALID_LIFECYCLES
            frontmatter_dict["lifecycle"] = raw_status if raw_status in VALID_LIFECYCLES else "active"
    # external SDLC object references (free-form dict[str, str]).
    if req.external_refs is not None:
        from palinode.core.parser import parse_external_refs as _parse_ext_refs
        validated = _parse_ext_refs({"external_refs": req.external_refs})
        if validated is not None:
            frontmatter_dict["external_refs"] = validated
    # ADR-010: explicit ``title`` overrides metadata-supplied title.
    if req.title:
        frontmatter_dict["title"] = req.title
    # source-citation anchors. Only written when provided so frontmatter
    # stays clean otherwise. Validation (computes/verifies quote_hash, rejects
    # malformed entries with 400) happens here so a bad anchor is caught before
    # the file is written.
    if req.sources is not None:
        frontmatter_dict["sources"] = _normalize_sources(req.sources)

    # (G4): typed relationship links. Resolve each from param-or-metadata
    # (the explicit param wins — mirrors update_policy's H4 handling), validate,
    # and write the normalized list only when non-empty so frontmatter stays
    # clean otherwise. `_resolved_contradicts` is reused for the reciprocal
    # back-link after the file is written.
    _meta_dict = req.metadata if isinstance(req.metadata, dict) else {}
    _contradicts_in = (
        req.contradicts if req.contradicts is not None else _meta_dict.get("contradicts")
    )
    _backed_by_in = (
        req.backed_by if req.backed_by is not None else _meta_dict.get("backed_by")
    )
    _resolved_contradicts = _normalize_link_refs(_contradicts_in, "contradicts")
    _resolved_backed_by = _normalize_link_refs(_backed_by_in, "backed_by")
    if _resolved_contradicts:
        frontmatter_dict["contradicts"] = _resolved_contradicts
    if _resolved_backed_by:
        frontmatter_dict["backed_by"] = _resolved_backed_by

    # Claim-level source anchors: resolved param-or-metadata like the typed
    # links above (the explicit param wins; the metadata path was excluded from
    # the verbatim merge so a malformed entry still gets a clean 400). The
    # claim_id derivation is salted with this memory's path-relative ref, so
    # the normalizer needs the resolved category/slug. Written only when
    # non-empty so frontmatter stays clean otherwise.
    _claims_in = req.claims if req.claims is not None else _meta_dict.get("claims")
    if _claims_in is not None:
        _resolved_claims = _normalize_claims_or_400(_claims_in, f"{category}/{slug}.md")
        if _resolved_claims:
            frontmatter_dict["claims"] = _resolved_claims

    # ADR-010: explicit body field > X-Palinode-Source header > env > "api".
    frontmatter_dict["source"] = _resolve_source(req.source, request)

    # auto-description is no longer generated inline. Like auto_summary
    # the LLM description is deferred to the watcher-driven
    # /generate-summaries backfill so /save returns in embed+write time
    # regardless of model latency (the timeout/circuit-breaker still left
    # /save blocked for up to describe_timeout_seconds on a warm-but-slow model).
    # config.auto_summary.enabled is the master switch for all LLM enrichment:
    # when disabled, no description is generated and /save is fast unconditionally.
    # The response carries description_pending=True for eligible files; the
    # watcher detects the absent description field and backfills within ~30s.
    # A caller-supplied description (via metadata) is respected and not deferred.
    description_pending = False
    if config.auto_summary.enabled and not frontmatter_dict.get("description"):
        description_pending = True
        # Leave description absent in frontmatter; watcher detects the missing
        # field and triggers /generate-summaries, which fills it.

    # Layer 2 wiki contract: auto-append See also footer for any entities
    # not already referenced as [[wikilinks]] in the body.
    body_content = _apply_wiki_footer(req.content, normalized_entities)

    doc = f"---\n{yaml.safe_dump(frontmatter_dict, default_flow_style=False, allow_unicode=True)}---\n\n{body_content}\n"

    git_tools.write_memory_file(file_path, doc)

    # auto_summary is no longer generated inline. The watcher detects
    # files matching (core=true, no summary) and schedules /generate-summaries
    # on a debounce — see palinode/indexer/watcher.py::_schedule_summary_generation.
    # Inline generation was blocking /save for the full LLM first-token cost
    # against a cold or contended local model, surfacing as "palinode write
    # timeouts" on REST clients. The response carries summary_pending=True so
    # callers can distinguish "summary still missing" from "this file is not
    # eligible." Mirror the description_pending pattern
    summary_pending = False
    if config.auto_summary.enabled:
        is_core = bool(frontmatter_dict.get("core", False))
        has_summary = bool(frontmatter_dict.get("summary"))
        if is_core and not has_summary and len(req.content) >= config.auto_summary.min_content_chars:
            summary_pending = True

    # Utilize auto backup procedures explicitly. One save = one per-file commit
    # via the git_tools mutation choke point (the single staging+commit primitive
    # all memory mutations route through).
    git_committed: bool = False
    if config.git.auto_commit:
        commit_msg = f"{config.git.commit_prefix} auto-save: {category}/{slug}.md"
        git_committed = git_tools.commit_memory_file(file_path, commit_msg)
        if not git_committed:
            # exc_info-free: the choke point already logged the I/O failure with
            # a stack trace. Surface a save-path signal so the git_committed
            # contract has an operator-visible breadcrumb on this logger too.
            logger.error("Git auto-commit did not complete for %r", file_path)
        elif config.git.auto_push:
            try:
                push = subprocess.run(
                    ["git", "push"], cwd=config.palinode_dir, check=False,
                    capture_output=True, text=True,
                )
                if push.returncode != 0:
                    # check=False meant a failed push (no remote, auth, rejected)
                    # was previously invisible — surface it.
                    logger.warning(
                        "git auto-push failed op=push file_path=%r returncode=%d stderr=%r",
                        file_path, push.returncode, (push.stderr or "").strip(),
                    )
            except (subprocess.SubprocessError, OSError) as e:
                logger.error("Git push failed for %r: %s", file_path, e, exc_info=True)

    # (G4): best-effort reciprocal back-link for `contradicts`. Because the
    # relationship is symmetric (A⇄B), add this memory's ref into each target's
    # `contradicts` list so the conflict surfaces from both sides in `lint`.
    # Never raises and never blocks the save — a missing/unreadable target is
    # logged and skipped. Forward-only is acceptable per the issue if this gets
    # risky; the helper keeps it clean (idempotent, choke-point writes).
    if _resolved_contradicts:
        try:
            from palinode.core.typed_links import add_reciprocal_contradicts
            source_ref = f"{category}/{slug}"
            add_reciprocal_contradicts(
                config.palinode_dir,
                source_ref,
                _resolved_contradicts,
                commit=config.git.auto_commit,
            )
        except Exception as exc:  # noqa: BLE001 — defensive: never fail the save
            logger.warning("reciprocal contradicts back-link skipped: %s", exc)

    logger.info(
        "Saved memory op=save file_path=%s id=%s category=%s git_committed=%s",
        file_path, frontmatter_dict["id"], category, git_committed,
    )

    # embed inline so that POST /save only returns once vector + FTS
    # entries actually exist. Previously the watcher embedded out-of-band,
    # leaving a race window where /search immediately after /save returned
    # zero results. The watcher remains the indexer for filesystem-direct
    # writes; this path covers API-driven saves.
    indexed = False
    indexed_vec: bool = True
    indexed_fts: bool = True
    index_error: str | None = None
    try:
        from palinode.indexer.index_file import index_file
        outcome = index_file(file_path)
        indexed = bool(outcome.get("embedded"))
        # Surface per-index health so callers can detect silent vec0/FTS5
        # failures. Defaults to True so a missing key (old index_file
        # version) does not falsely signal failure.
        indexed_vec = bool(outcome.get("indexed_vec", True))
        indexed_fts = bool(outcome.get("indexed_fts", True))
        index_error = outcome.get("error")
    except Exception as e:
        # File is on disk; the watcher will pick it up later. exc_info so the
        # non-fatal index failure carries a stack trace, structured fields for
        # grep.
        logger.warning(
            "Inline index failed (non-fatal) op=inline_index file_path=%s error=%r",
            file_path, str(e), exc_info=True,
        )
        index_error = str(e)
        indexed_vec = False
        indexed_fts = False

    if not indexed:
        logger.warning(
            f"Saved {file_path} but inline embed did not complete "
            f"(reason: {index_error or 'unknown'}); watcher will retry."
        )

    result: dict[str, Any] = {
        "file_path": file_path,
        "id": frontmatter_dict["id"],
        "indexed": indexed,
        "embedded": indexed,
        # Per-index health flags. vec/FTS failures are non-fatal
        # but silent — surface them so callers (MCP, CLI) can warn the user.
        "indexed_vec": indexed_vec,
        "indexed_fts": indexed_fts,
        # git_committed is True only when auto_commit is enabled AND the commit
        # subprocess succeeded. False when disabled or when git errors.
        "git_committed": git_committed,
    }
    if index_error and not indexed:
        result["index_error"] = index_error
    # surface deferred description so callers know the description is not
    # yet set and the watcher will fill it in via /generate-summaries on the
    # next file event. Mirrors summary_pending.
    if description_pending:
        result["description_pending"] = True
    # surface deferred auto_summary so callers know the summary is not
    # yet set and the watcher will trigger /generate-summaries on the next
    # file event. Mirrors the description_pending pattern.
    if summary_pending:
        result["summary_pending"] = True

    # Tier 2a (ADR-004): schedule write-time contradiction check.
    # Always safe to call — returns None immediately if disabled in config.
    # Errors inside the scheduler are logged and swallowed; never propagate.
    if config.consolidation.write_time.enabled:
        try:
            from palinode.consolidation import write_time
            item = {
                "content": req.content,
                "category": category,
                "type": req.type,
                "entities": req.entities or [],
                "id": frontmatter_dict["id"],
            }
            check_result = write_time.schedule_contradiction_check(
                file_path, item, sync=sync
            )
            if sync and check_result is not None:
                result["write_time_check"] = check_result
        except Exception as e:
            # Load-bearing: save must never fail because of tier 2a. This is a
            # non-fatal opt-in feature degrading — WARNING, not ERROR, with a
            # stack trace for diagnosis (docs/logging.md DEMOTE).
            logger.warning(
                "write-time schedule failed (non-fatal) op=write_time_check file_path=%s error=%r",
                file_path, str(e), exc_info=True,
            )

    return result


@router.post("/generate-summaries")
def generate_summaries_api() -> dict[str, Any]:
    """Backfill missing auto-enrichment (descriptions + summaries) for files.

    Scans all markdown files under ``palinode_dir``:

    - **Descriptions** (#405): any file missing a ``description`` field gets one
      generated via Ollama. Descriptions are not core-gated — every memory gets
      one, mirroring the prior inline behavior that #405 moved off the /save hot
      path. Skipped entirely when ``auto_summary.enabled`` is False.
    - **Summaries** (#403): files with ``core: true`` and no ``summary`` get one.

    This endpoint is the watcher-driven backfill that lands both enrichments
    after /save returns fast (#403/#405). Despite the name, it fills both —
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
            with open(filepath) as f:
                content = f.read()
            metadata, _ = parser.parse_markdown(content)

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
