"""The save capability: validate, write, commit, index, and enrich one memory.

Extracted from ``api/routers/memory.py::save_api``, which had grown to ~616
lines of capability logic inside a FastAPI route function — the save path was
reachable only over HTTP, and testable only through ``TestClient``. This is the
same shape ``core/lint.py``, ``core/review.py``, ``core/trace.py`` and
``consolidation/archive.py`` already use: a deep module in ``core/`` with the
router reduced to delegation plus an error map.

The extraction is behaviour-preserving. What stayed behind in the router is
exactly what belongs to the transport:

* rate limiting (HTTP 429) — a per-client-IP concern,
* the request-size cap (HTTP 413) — a limit on the *request*, not the memory,
* ``_resolve_source``'s header lookup — this module takes the already-resolved
  ``source`` string and falls back to :func:`default_source` when given none.

Everything else — envelope rejection, the ADR-009/010/015/018 validation ladder,
frontmatter assembly, the choke-point write and commit, reciprocal back-links,
inline indexing, and the tier-2a / forget hooks — lives here.

Errors: every input rejection raises :class:`SaveValidationError`, which the
router maps to HTTP 400. That is the whole error contract; the module raises
nothing else by design (index, push, tier-2a and forget failures are all
non-fatal and logged, exactly as before).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import time
from datetime import UTC, datetime
from typing import Any

import yaml

from palinode.core import git_tools, store
from palinode.core.config import config
from palinode.core.defaults import SAVE_SOURCE_API_DEFAULT
from palinode.core.envelope import envelope_complaint
from palinode.core.memory_write import (
    _TYPE_TO_CATEGORY,
    _apply_wiki_footer,
    _normalize_entities,
    compose_appended_body,
)
from palinode.core.path_guard import (
    PathTraversalError,
    memory_base_dir,
    resolve_memory_path,
    to_rel_path,
)
from palinode.core.skip_dirs import ALWAYS_SKIP
from palinode.core.typed_links import is_well_formed_ref

logger = logging.getLogger("palinode.core.save")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Defined locally, as ``core/store.py``, ``core/git_tools.py``,
    ``consolidation/ttl.py`` and the rest of the write path each do — reaching
    into ``api/_util.py`` for it would make ``core`` import ``api``.
    """
    return datetime.now(UTC)


class SaveValidationError(ValueError):
    """A save was rejected at the validation boundary.

    Carries the human-readable reason as its message. The API layer maps this
    to ``HTTPException(400, detail=str(exc))``; CLI and MCP surfaces render the
    message directly. A ``ValueError`` subclass so callers that already catch
    ``ValueError`` around a write keep working.
    """


def default_source() -> str:
    """The source attribution used when a caller supplies none.

    Levels 3 and 4 of the ADR-010 precedence chain (``PALINODE_SOURCE``
    environment override, then the ``"api"`` default). Levels 1 and 2 — the
    explicit field and the ``X-Palinode-Source`` header — are resolved by the
    transport before it calls :func:`save_memory`; see
    ``api/memory_write.py::_resolve_source``, which defers to this function so
    the two halves of the chain cannot drift.
    """
    return os.environ.get("PALINODE_SOURCE", SAVE_SOURCE_API_DEFAULT)


def _normalize_sources(raw: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Validate and normalize ``sources:`` quote anchors for save.

    Each entry must be a dict with non-empty ``ref`` and ``quote``. The
    ``quote_hash`` is computed when absent and validated when present — a stored
    hash that does not match its quote is a tampered/inconsistent anchor and is
    rejected. Raises :class:`SaveValidationError` on any malformed input;
    returns the normalized list of ``{ref, quote, quote_hash}`` dicts otherwise.
    """
    from palinode.core.quote_verify import UnsupportedHashAlgorithm
    from palinode.core.quote_verify import quote_hash as _quote_hash
    from palinode.core.quote_verify import quote_hash_matches as _quote_hash_matches

    if not isinstance(raw, list):
        raise SaveValidationError("sources must be a list")

    normalized: list[dict[str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SaveValidationError(f"sources[{i}] must be an object")
        ref = entry.get("ref")
        quote = entry.get("quote")
        if not isinstance(ref, str) or not ref.strip():
            raise SaveValidationError(f"sources[{i}] missing non-empty 'ref'")
        if not isinstance(quote, str) or not quote.strip():
            raise SaveValidationError(f"sources[{i}] missing non-empty 'quote'")
        ref = ref.strip()
        computed = _quote_hash(quote)
        supplied = entry.get("quote_hash")
        if supplied is not None and str(supplied).strip():
            # Validate under the supplied hash's OWN algorithm so legacy bare-MD5
            # anchors still round-trip; `computed` (canonical prefixed form) is
            # what gets stored, upgrading the anchor in place.
            try:
                if not _quote_hash_matches(quote, str(supplied)):
                    raise SaveValidationError(
                        f"sources[{i}] quote_hash does not match its quote "
                        "(inconsistent anchor)"
                    )
            except UnsupportedHashAlgorithm as exc:
                raise SaveValidationError(f"sources[{i}] {exc}") from exc
        normalized.append({"ref": ref, "quote": quote, "quote_hash": computed})
    return normalized


def _normalize_link_refs(raw: Any, field: str) -> list[str]:
    """Validate a typed-link ref list, raising :class:`SaveValidationError`.

    Thin wrapper over :func:`palinode.core.typed_links.normalize_link_refs` that
    re-raises the core ``TypedLinkError`` as the save boundary's own error type
    — mirroring how ``_normalize_sources`` rejects malformed anchors.
    """
    from palinode.core.typed_links import TypedLinkError, normalize_link_refs

    try:
        return normalize_link_refs(raw, field)
    except TypedLinkError as exc:
        raise SaveValidationError(str(exc))


def _normalize_claims_or_reject(raw: Any, memory_ref: str) -> list[dict[str, Any]]:
    """Validate claim-level source anchors, raising :class:`SaveValidationError`.

    Thin wrapper over :func:`palinode.core.claims.normalize_claims` that maps
    the core ``ClaimError`` to the save boundary's error type — the same
    discipline as ``_normalize_sources`` and ``_normalize_link_refs``.
    """
    from palinode.core.claims import ClaimError, normalize_claims

    try:
        return normalize_claims(raw, memory_ref)
    except ClaimError as exc:
        raise SaveValidationError(str(exc))


# Derived on every save, or carried by a rule of their own; never copied by the carry.
_NEVER_CARRIED_FRONTMATTER = frozenset({
    "id", "category", "type", "content_hash", "last_updated", "auditable_memory",
    "source", "created_at", "update_policy", "epistemic", "ttl",
})
_REPLACE_BODY_BOUND = frozenset({
    "description", "description_pending", "summary", "summary_pending",
    "claims", "sources",
    # A re-save is the "re-verified against its sources" act that clears a
    # retired-source flag (consolidation/propagate.py), so it never carries.
    "stale_backing",
})


def _carry_prior_frontmatter(
    frontmatter_dict: dict[str, Any], prior: dict[str, Any], *,
    has_entities: bool, extra_never_carried: frozenset[str] = frozenset(),
) -> None:
    never_carried = set(_NEVER_CARRIED_FRONTMATTER | extra_never_carried)
    if config.ku_compat.enabled:
        # ku_compat derives these; a carried copy would block the re-derivation.
        never_carried.add("ku_version")
        if "status" in frontmatter_dict:
            never_carried.add("lifecycle")
    prior_entities = prior.get("entities")
    if not has_entities and isinstance(prior_entities, list):
        # `project` is sugar for one entity, so it joins the carried list.
        frontmatter_dict["entities"] = prior_entities + [
            e for e in frontmatter_dict["entities"] if e not in prior_entities
        ]
    for key, value in prior.items():
        if key not in never_carried and key not in frontmatter_dict:
            frontmatter_dict[key] = value


def _holds_this_content(path: str, incoming_hash: str) -> bool:
    """True when *path* already holds exactly this content.

    Compares against the ``content_hash`` recorded in the file's frontmatter
    rather than re-deriving a body from it: ``parse_markdown`` returns chunks,
    not the original text.

    A file that cannot be read or parsed answers False, so the caller suffixes
    away from it instead of overwriting something it could not inspect.
    """
    from palinode.core import parser as _parser

    try:
        with open(path, "r", encoding="utf-8") as existing:
            existing_meta, _ = _parser.parse_markdown(existing.read())
    except (OSError, ValueError, yaml.YAMLError):
        return False
    return str(existing_meta.get("content_hash", "")) == incoming_hash


#: The longest file name, in bytes, that APFS, ext4 and NTFS all accept.
_NAME_MAX_BYTES = 255


def _parse_save_path(path: str) -> str:
    """The POSIX form of a caller-chosen save ``path``, relative to the memory dir.

    Raises:
        SaveValidationError: naming the one condition the path breaks.
    """
    written = path.replace("\\", "/")
    if "\x00" in written:
        raise SaveValidationError(f"path {path!r} holds a NUL byte")
    if written.startswith("/"):
        raise SaveValidationError(
            f"path {path!r} is absolute; give it relative to the memory dir"
        )
    if ":" in written:
        raise SaveValidationError(
            f"path {path!r} holds a ':', which Windows reads as a drive or a data stream"
        )
    parts = [part for part in written.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise SaveValidationError(
            f"path {path!r} has a '..' segment; it must stay inside the memory dir"
        )
    reserved = _find_reserved_part(parts)
    if reserved:
        raise SaveValidationError(f"path {path!r} lies under {reserved!r}, a reserved folder")
    if written.endswith("/") or not parts or not parts[-1].endswith(".md") or parts[-1] == ".md":
        raise SaveValidationError(f"path {path!r} must name a file ending in .md")
    normalised = "/".join(parts)
    if not is_well_formed_ref(normalised.removesuffix(".md")):
        raise SaveValidationError(
            f"path {path!r} could not be cited as a link: a ref starts with a letter or digit, "
            "holds only letters, digits, '.', '_', '-' and '/', and never '..'"
        )
    _assert_save_path_is_writable_in_store(path, normalised)
    return normalised


def _assert_save_path_is_writable_in_store(path: str, normalised: str) -> None:
    """Refuse a save ``path`` that cannot be written inside the store as it stands.

    Raises:
        SaveValidationError: naming the condition the caller's path ran into.
    """
    base_dir = memory_base_dir()
    parts = normalised.split("/")
    escape_message = f"path {path!r} resolves outside the memory dir through a link"
    # The write goes through a temp file beside the file, whose name is longer.
    file_name_max = _NAME_MAX_BYTES - git_tools.WRITE_TEMP_EXTRA_BYTES
    if any(len(part.encode()) > _NAME_MAX_BYTES for part in parts[:-1]) or (
        len(parts[-1].encode()) > file_name_max
    ):
        raise SaveValidationError(
            f"path {path!r} has a name too long: the limit is {_NAME_MAX_BYTES} bytes "
            f"for a folder and {file_name_max} for the file"
        )
    path_max = _get_path_max_bytes(config.palinode_dir) - git_tools.WRITE_TEMP_EXTRA_BYTES
    # The kernel counts both the path as written and the one a link in the store's root expands to.
    longest_form = max(len(os.path.join(root, *parts).encode()) for root in (config.palinode_dir, base_dir))
    if longest_form >= path_max:
        raise SaveValidationError(
            f"path {path!r} is too long: the full path must stay under {path_max} bytes"
        )
    for depth in range(1, len(parts) + 1):
        ref = "/".join(parts[:depth])
        entry = os.path.join(base_dir, *parts[:depth])
        if os.path.islink(entry):
            raise SaveValidationError(f"path {path!r} runs through {ref!r}, a symbolic link")
        # Checked before anything is said about the entry, so no answer describes a target outside.
        if not _is_inside(os.path.realpath(entry), base_dir):
            raise SaveValidationError(escape_message)
        try:
            is_folder = stat.S_ISDIR(os.stat(entry).st_mode)
        except FileNotFoundError:
            break
        is_last = depth == len(parts)
        if is_last and is_folder:
            raise SaveValidationError(f"path {path!r} names a folder, not a file")
        if not is_last and not is_folder:
            raise SaveValidationError(f"path {path!r} uses the file {ref!r} as a folder")
    try:
        _, resolved = resolve_memory_path(normalised)
    except PathTraversalError as exc:
        raise SaveValidationError(escape_message) from exc
    reserved = _find_reserved_part(os.path.relpath(resolved, base_dir).split(os.sep))
    if reserved:
        raise SaveValidationError(f"path {path!r} resolves under {reserved!r}, a reserved folder")


def _is_inside(real_path: str, base_dir: str) -> bool:
    try:
        return os.path.commonpath([real_path, base_dir]) == base_dir
    except ValueError:
        # Windows: the two lie on different drives.
        return False


def _get_path_max_bytes(memory_dir: str) -> int:
    try:
        path_max = os.pathconf(memory_dir, "PC_PATH_MAX")
    except AttributeError:
        # Windows has no pathconf; MAX_PATH is its classic limit.
        return 260
    except OSError:
        # A store not made yet; macOS's limit is the smallest of the POSIX ones.
        return 1024
    return path_max if path_max > 0 else 1024


def _find_reserved_part(parts: list[str]) -> str | None:
    # Case-insensitive filesystems, and Windows' trimming of trailing dots and spaces,
    # map ".GIT" and ".git." onto ".git".
    for part in parts:
        name = part.rstrip(". ").casefold()
        if name in ALWAYS_SKIP:
            return name
    return None


def _disambiguate_derived_slug(
    slug: str, file_path: str, content: str
) -> tuple[str, str]:
    """Give a content-derived slug its own path when one is already taken.

    Two saves whose opening lines agree derive the same slug, and writing both
    to that path leaves only the last one in the store -- no error, no warning,
    and a receipt indistinguishable from a fresh create. The earlier memories
    survive only in git history, which the queryable store never consults.

    Re-saving *identical* content is left alone: that is the same memory
    arriving twice, and suffixing it would litter the store with duplicates.
    That holds at every position, not just the base path -- a memory that was
    pushed to ``slug-2`` on its first save must land back on ``slug-2`` when it
    is saved again, or each repeat would claim another suffix.

    Only ever called for derived slugs. An explicit slug that collides is an
    update of the same logical memory and keeps its overwrite semantics.
    """
    if not os.path.exists(file_path):
        return slug, file_path

    incoming_hash = hashlib.sha256(content.encode()).hexdigest()
    if _holds_this_content(file_path, incoming_hash):
        return slug, file_path

    directory = os.path.dirname(file_path)
    # Bounded so a pathological directory cannot spin here; the timestamp
    # fallback below always terminates.
    for suffix in range(2, 1000):
        candidate_slug = f"{slug}-{suffix}"
        candidate_path = os.path.join(directory, f"{candidate_slug}.md")
        if os.path.exists(candidate_path):
            if _holds_this_content(candidate_path, incoming_hash):
                return candidate_slug, candidate_path
            continue
        logger.info(
            "derived slug %r already taken; saving as %r to avoid "
            "overwriting an unrelated memory",
            slug,
            candidate_slug,
        )
        return candidate_slug, candidate_path

    candidate_slug = f"{slug}-{int(time.time() * 1000)}"
    return candidate_slug, os.path.join(directory, f"{candidate_slug}.md")


def save_memory(
    *,
    content: str,
    type: str,
    slug: str | None = None,
    entities: list[str] | None = None,
    metadata: Any | None = None,
    core: bool | None = None,
    source: str | None = None,
    confidence: float | None = None,
    priority: int | None = None,
    title: str | None = None,
    project: str | None = None,
    external_refs: dict[str, Any] | None = None,
    update_policy: str | None = None,
    sources: list[dict[str, Any]] | None = None,
    epistemic: str | None = None,
    contradicts: Any = None,
    backed_by: Any = None,
    claims: Any = None,
    sync: bool = False,
    push: bool | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Create a typed memory file, commit it to git, and index it inline.

    The capability behind ``POST /save``. ``content`` and ``type`` are required;
    ``type`` selects the destination directory (``Decision`` → ``decisions/``,
    ``Insight`` → ``insights/``, …). There is no ``category`` parameter — it is
    *derived* from ``type``.

    Args:
        content: Markdown body of the memory.
        type: One of ``MEMORY_TYPES``; selects the destination category.
        slug: URL-safe filename stem. Derived from the first content line when
            omitted, and always re-sanitized.
        entities: Entity refs; bare strings gain a category-inferred prefix.
        metadata: Free-form frontmatter merged verbatim, minus the fields that
            are resolved and validated on their own (``update_policy``,
            ``epistemic``, ``contradicts``, ``backed_by``, ``claims``).
        core: Marks the memory for core injection.
        source: Resolved source attribution. Falls back to
            :func:`default_source` when None.
        confidence: Optional 0..1 confidence score.
        priority: Optional 1..5 human-assigned priority.
        title: Human-readable title; overrides a metadata-supplied one.
        project: ADR-010 sugar for the ``project/<slug>`` entity.
        external_refs: SDLC object references (GitHub PR, Jira issue, …).
        update_policy: ADR-015 §2.1 write-semantics axis, and a real write
            semantic: ``"append"`` against an existing ``(category, slug)``
            adds to that document (prior body kept verbatim, new content under
            a dated append heading, every prior frontmatter key this save does
            not set kept). ``"replace"`` replaces the body in place, keeps
            record frontmatter except old-body descriptions and anchors, and
            marks it a living document consolidation must not fork into
            history. Sticky frontmatter — carried forward from the existing
            file when omitted. A save that declares *no* policy also replaces
            the body while keeping record frontmatter; only an explicit (or
            inherited) ``append`` appends.
        sources: Source-citation quote anchors; ``quote_hash`` computed when
            absent, verified when supplied.
        epistemic: ADR-018 epistemic marker. Sticky, like ``update_policy``.
        contradicts: Typed conflict links; also written reciprocally into each
            target on a best-effort basis.
        backed_by: Typed evidence/support links.
        claims: Claim-level source anchors bound to this memory's ref.
        sync: Run the ADR-004 tier-2a write-time contradiction check inline and
            return its result, instead of enqueuing it for the background pass.
        push: Per-call override of the auto-push decision. ``None`` defers to
            ``config.git.auto_push``; ``False`` suppresses this save's auto-push
            even when the config enables it, which is how session-end's own
            ``push=False`` threads through to the individual-file save it makes
            internally. ``True`` is accepted but redundant for a caller that
            already pushes explicitly afterward.
        path: The file to write, relative to the memory dir, in place of
            ``<category>/<slug>.md``. ``type`` still sets the front matter.
            Refused alongside ``slug``, and unless it is relative, ends in
            ``.md``, holds no NUL, ``:`` or ``..``, lies outside every
            ``skip_dirs.ALWAYS_SKIP`` folder, and meets no symbolic link below the memory dir.

    Returns:
        A dict carrying ``file_path``, ``rel_path`` (and ``path``, the same
        value, when the caller gave one), ``id``, ``save_outcome``
        (one of ``created``, ``resaved``, ``disambiguated``, ``appended``, or
        ``replaced``),
        ``disambiguated_from``, the index health flags
        (``indexed``/``embedded``/``indexed_vec``/``indexed_fts``), and
        ``git_committed`` — plus ``git_error`` (why the auto-commit did not
        land: not a repo, missing identity, lock held), ``index_error``,
        ``description_pending``, ``summary_pending``, ``write_time_check`` and
        ``forget`` when they apply.

    Raises:
        SaveValidationError: any input rejection — malformed envelope, unknown
            ``update_policy``/``status``/``visibility``/``epistemic``, a failed
            security scan, bad TTL, or a malformed source/link/claim anchor.
    """
    # Fail loud BEFORE any write: an envelope indexed as memory is silent
    # corruption. No fallback file is needed on this path the way session-end
    # needs one — none of the four save surfaces is fire-and-forget: MCP and
    # the plugin return the 400 in-band to an agent that still holds the
    # content, the CLI prints it to a human who typed it, and no hook or
    # script posts /save at all.
    #
    # `missing_params=()` is the load-bearing difference from session-end. Every
    # save array (`entities`, `sources`, `claims`, `contradicts`, `backed_by`)
    # is optional and absent on most honest calls, so their absence is not an
    # absorption signature here and must not license rejection on a bare tag
    # match — that would 400 an ordinary note containing a `<details><summary>`
    # block. Save is guarded by the unmatched-tag and trailing-fragment signals.
    complaint = envelope_complaint(
        content,
        "content",
        remediation="Re-send `content` as the note text alone, without the "
        "surrounding tool call.",
    )
    if complaint:
        logger.warning("save rejected: %s", complaint)
        raise SaveValidationError(complaint)

    save_path: str | None = None
    if path is not None:
        if slug:
            raise SaveValidationError(
                "give either path or slug, not both: path names the file, slug names it "
                "inside the type's folder"
            )
        save_path = _parse_save_path(path)
        # The file's stem stands in for the slug; a path is chosen, never derived.
        slug = save_path.rsplit("/", 1)[-1].removesuffix(".md")

    if slug and save_path is None:
        # Prevent any potential JSON escape or traversal exploits if user defines slug
        slug = re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')

    # Whether the slug was chosen by the caller or inferred from the content.
    # An explicit slug that collides is an update of the same logical memory and
    # must keep overwriting. A *derived* slug that collides is an accident
    # between two unrelated memories, and overwriting there loses data silently.
    slug_was_derived = False
    if not slug:
        slug_was_derived = True
        slug = re.sub(r'[^a-z0-9]+', '-', content.split('\n')[0].lower()[:30]).strip('-')
        if not slug:
            slug = str(int(time.time()))

    # module-level map (shared with the description-eligibility predicate
    # so the writer and the count/worklist derive from one literal).
    category = _TYPE_TO_CATEGORY.get(type, "inbox")
    # What names this memory in ids, commits and links: its path relative to the store.
    rel_ref = save_path or f"{category}/{slug}.md"

    # ADR-015 §2.1: validate the write-semantics axis. Reject an
    # unknown update_policy outright rather than silently coercing — a typo'd
    # policy ("repalce") must not quietly fall back to append and leave a
    # living document mis-declared.
    from palinode.core.parser import (
        AMR_SPEC_VERSION as _AMR_SPEC_VERSION,
        VALID_AMR_VERSIONS as _VALID_AMR_VERSIONS,
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
    if metadata and isinstance(metadata, dict):
        _meta_update_policy = metadata.get("update_policy")
    _effective_update_policy = (
        update_policy if update_policy is not None else _meta_update_policy
    )
    if (
        _effective_update_policy is not None
        and _effective_update_policy not in _VALID_UPDATE_POLICIES
    ):
        raise SaveValidationError(
            f"Invalid update_policy {_effective_update_policy!r}; "
            f"expected one of {list(_VALID_UPDATE_POLICIES)}"
        )

    # ADR-015 §2.2: validate a writer-supplied `status` against the
    # combined lifecycle + incident allow-set. `status` is shared with the
    # store's search-exclusion (`config.search.exclude_status`), so a typo'd
    # status that landed in frontmatter could silently mis-classify a memory
    # for recall. Reject unknown values at the surface. The status may arrive
    # via the `metadata` dict (metadata["status"]); validate there too.
    _req_status = None
    if metadata and isinstance(metadata, dict):
        _req_status = metadata.get("status")
    if _req_status is not None and _req_status not in _VALID_STATUSES:
        raise SaveValidationError(
            f"Invalid status {_req_status!r}; "
            f"expected one of {list(_VALID_STATUSES)}"
        )

    # ADR-009 Layer 2: validate the visibility axis. It arrives only via
    # the `metadata` dict today (no first-class param yet — promoting it to a
    # four-surface param per ADR-010 is a follow-on). Two checks, both aimed at
    # the same failure: a memory the author believes is protected, or believes
    # is reachable, when it is neither.
    #
    # `private` with no `scope:` names no owner. The read path falls back to the
    # directory-inferred `project/<dir>`, which no real session chain contains,
    # so the memory would be invisible on every scoped surface — including to
    # its own author, silently and forever. Reject it here so the failure is
    # loud at write time, with the fix in the message.
    #
    # `restricted` with no `access:` allowlist is the mirror image: nothing can
    # ever intersect an empty list, so the memory is unreachable by everyone.
    _meta = metadata if isinstance(metadata, dict) else {}
    _visibility = _meta.get("visibility")
    if _visibility is not None:
        from palinode.core.parser import VALID_VISIBILITIES as _VALID_VISIBILITIES
        if _visibility not in _VALID_VISIBILITIES:
            raise SaveValidationError(
                f"Invalid visibility {_visibility!r}; "
                f"expected one of {list(_VALID_VISIBILITIES)}"
            )
        _scope_val = _meta.get("scope")
        _has_scope = isinstance(_scope_val, str) and _scope_val.strip()
        if _visibility == "private" and not _has_scope:
            raise SaveValidationError(
                "visibility: private requires an explicit scope: naming the "
                "owner, e.g. scope: agent/<name> or scope: member/<name>. "
                "Without one the memory is visible to no session, including "
                "yours."
            )
        _access_val = _meta.get("access")
        if _visibility == "restricted" and not (
            isinstance(_access_val, list)
            and any(a is not None and str(a).strip() for a in _access_val)
        ):
            raise SaveValidationError(
                "visibility: restricted requires a non-empty access: "
                "allowlist of entity refs, e.g. access: [member/alice]. "
                "An empty allowlist matches no session."
            )

    # (ADR-018): validate the epistemic marker. Like update_policy/status it
    # may arrive via the first-class param OR the `metadata` dict (merged verbatim
    # into frontmatter below) — resolve the effective value from both (the param
    # wins) and validate that, so a metadata-supplied typo ("inferrence") can't
    # land unvalidated. The effective value is written from this single var below.
    _meta_epistemic = None
    if metadata and isinstance(metadata, dict):
        _meta_epistemic = metadata.get("epistemic")
    _effective_epistemic = (
        epistemic if epistemic is not None else _meta_epistemic
    )
    if (
        _effective_epistemic is not None
        and _effective_epistemic not in _VALID_EPISTEMICS
    ):
        raise SaveValidationError(
            f"Invalid epistemic {_effective_epistemic!r}; "
            f"expected one of {list(_VALID_EPISTEMICS)}"
        )

    # AMR §4.1: every record this path writes declares the spec version it
    # conforms to. The value is authoritative — written from the constant, not
    # the caller — but a caller-supplied value tunneled through `metadata` is
    # still checked: an unrecognized version is rejected rather than guessed at
    # or silently overwritten, so a record claiming "0.9" cannot be laundered
    # into a "0.1" declaration by the save.
    _meta_amr = None
    if metadata and isinstance(metadata, dict):
        _meta_amr = metadata.get("auditable_memory")
    if _meta_amr is not None and str(_meta_amr) not in _VALID_AMR_VERSIONS:
        raise SaveValidationError(
            f"unrecognized auditable_memory version {_meta_amr!r}; "
            f"this implementation writes {_AMR_SPEC_VERSION!r}"
        )

    # AMR §4.6 / conformance l1-011: confidence is a number in [0.0, 1.0].
    # Resolved param-or-metadata like epistemic (the param wins) so a value
    # tunneled through `metadata` cannot land unvalidated. Rejected, not
    # clamped — a clamped 1.4 would read as certainty the caller never stated.
    _meta_confidence = None
    if metadata and isinstance(metadata, dict):
        _meta_confidence = metadata.get("confidence")
    _effective_confidence = (
        confidence if confidence is not None else _meta_confidence
    )
    if _effective_confidence is not None:
        if (
            isinstance(_effective_confidence, bool)
            or not isinstance(_effective_confidence, (int, float))
            or not (0.0 <= float(_effective_confidence) <= 1.0)
        ):
            raise SaveValidationError(
                f"confidence out of range: {_effective_confidence!r}; "
                "expected a number in [0.0, 1.0]"
            )
        _effective_confidence = float(_effective_confidence)

    # Security scan: reject prompt injection and exfiltration attempts
    is_safe, reason = store.scan_memory_content(content)
    if not is_safe:
        raise SaveValidationError(f"Security scan failed: {reason}")

    original_slug = slug
    file_path = os.path.join(config.palinode_dir, *rel_ref.split("/"))

    if slug_was_derived:
        slug, file_path = _disambiguate_derived_slug(slug, file_path, content)
        rel_ref = f"{category}/{slug}.md"

    # Classify the save after derived-slug resolution but before the write.
    # Looking only at the base path would mislabel a repeat save of an existing
    # suffixed memory as disambiguated; the selected target's prior existence
    # is what separates a normal re-save from creation at a new suffix.
    target_existed = os.path.exists(file_path)
    if slug_was_derived:
        if target_existed:
            save_outcome = "resaved"
        elif slug != original_slug:
            save_outcome = "disambiguated"
        else:
            save_outcome = "created"
    else:
        save_outcome = "replaced" if target_existed else "created"
    disambiguated_from = (
        original_slug if save_outcome == "disambiguated" else None
    )

    # Normalize entity refs: bare strings get a category prefix.
    # e.g. "palinode" → "project/palinode", "alice" → "person/alice"
    raw_entities = list(entities or [])
    # ADR-010: ``project`` is sugar for the ``project/<slug>`` entity.
    if project:
        project_ref = project if "/" in project else f"project/{project}"
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
    # This is deliberately NOT gated behind update_policy: re-saving the same
    # (category, slug) is the same logical memory, so its birth timestamp
    # should be preserved regardless of write policy. `last_updated` still
    # advances on every save, append included — an appended document did
    # change, and the append's own timestamp is recorded in its block marker.
    #
    # Fallback: if an existing file lacks created_at in its frontmatter, leave
    # today's behaviour (stamp now). A git-log first-commit lookup is the
    # principled fallback (ADR-015 §2.4) but is deferred to a later refinement.
    created_at = _now_iso
    # ADR-015 §2.1 / §6 Q2 (both param + sticky field): the explicit param wins;
    # otherwise carry forward the existing file's sticky update_policy so the
    # file's declared regime survives a save that omits the param. A save that
    # neither supplies a policy nor inherits one stays None — no frontmatter
    # field is written and the write is an overwrite. DEFAULT_UPDATE_POLICY
    # names the axis's default *value*, not that behaviour.
    # H4: resolve from param-or-metadata (validated above); the param wins.
    resolved_update_policy = _effective_update_policy
    # The prior body, when the target already exists. Read here (the block
    # below is already reading the file) and consumed by the append
    # composition. An unreadable file, or one whose frontmatter does not
    # parse, stops the rewrite: its privacy frontmatter cannot be carried.
    _existing_body = ""
    _existing_meta: dict[str, Any] = {}
    if os.path.exists(file_path):
        try:
            from palinode.core import parser as _parser
            with open(file_path, "r", encoding="utf-8") as _existing:
                _existing_raw = _existing.read()
            _existing_meta, _ = _parser.parse_markdown(_existing_raw)
            # A file with no frontmatter has no privacy to lose; one whose
            # frontmatter does not parse might, so it cannot be rewritten blind.
            if not _existing_meta and _existing_raw.lstrip().startswith("---"):
                raise ValueError("malformed frontmatter")
            _, _existing_body = _parser.split_frontmatter(_existing_raw)
            _prior_created = _existing_meta.get("created_at")
            if _prior_created:
                created_at = str(_prior_created)
            # Sticky carry-forward: if the caller didn't supply update_policy,
            # inherit the value the file already declares.
            if resolved_update_policy is None:
                _prior_policy = _existing_meta.get("update_policy")
                if _prior_policy in _VALID_UPDATE_POLICIES:
                    resolved_update_policy = str(_prior_policy)
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
            raise SaveValidationError(
                f"Could not read existing frontmatter for {file_path!r}; refusing rewrite"
            ) from exc

    # ADR-015 §2.1: `append` is a write semantic, not only a compaction marker.
    # A save declaring `append` against an existing (category, slug) adds to
    # that document — the prior body is carried forward verbatim and the new
    # content lands under a dated append heading. `replace` keeps the
    # overwrite-in-place behaviour, which is what a living document wants.
    #
    # Three guards on reaching this branch, each load-bearing:
    #
    # * `resolved_update_policy == "append"` — an *explicit* declaration (now or
    #   sticky on the file), never the implicit default. A save that never names
    #   a policy still overwrites, because DEFAULT_UPDATE_POLICY is `append` and
    #   making the default append would turn every ordinary re-save — session
    #   notes, snapshots, consolidation write-backs — into a growing duplicate.
    # * `not slug_was_derived` — a derived slug that collides is an accident
    #   between two *unrelated* memories (see `_disambiguate_derived_slug`),
    #   and appending one onto the other would be worse than the disambiguation
    #   that path already does. An explicit slug is the same logical memory.
    # * a non-empty prior body — nothing to append to otherwise, and a first
    #   save must not be born with a separator.
    #
    # There is deliberately NO duplicate suppression here: an append that
    # arrives twice leaves a visible, editable duplicate, whereas dropping a
    # repeated block silently discards caller content — the exact failure this
    # change exists to end.
    #
    # The security scan above ran over `content`, which is correct: the prior
    # body was scanned at its own save, and re-scanning it here would let a
    # later pattern-list change retroactively 400 a save that adds nothing to
    # the offending text.
    effective_content = content
    if (
        resolved_update_policy == "append"
        and not slug_was_derived
        and target_existed
        and _existing_body.strip()
    ):
        effective_content = compose_appended_body(_existing_body, content, _now_iso)
        save_outcome = "appended"

    content_hash = hashlib.sha256(effective_content.encode()).hexdigest()

    frontmatter_dict = {
        # AMR §4.1: the conformance declaration, REQUIRED on every record
        # written under the spec. Constant-sourced (validated above if the
        # caller also supplied one) so the field is never absent and never
        # a value this implementation does not actually implement.
        "auditable_memory": _AMR_SPEC_VERSION,
        "id": rel_ref.removesuffix(".md").replace("/", "-"),
        "category": category,
        "type": type,
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
    if metadata:
        # H4: don't let raw, unvalidated fields from the metadata dict land in
        # frontmatter — `update_policy`, `epistemic`, and the typed
        # link fields `contradicts`/`backed_by` are each resolved +
        # validated above/below and written from their own normalized values, so a
        # malformed value tunneled through metadata still gets a clean 400.
        _verbatim_excluded = {
            "update_policy", "epistemic", "contradicts", "backed_by", "claims",
            "auditable_memory", "confidence",
        }
        frontmatter_dict.update(
            {k: v for k, v in metadata.items() if k not in _verbatim_excluded}
        )
    # ADR-015 §2.3: ephemeral TTL. A metadata-supplied `ttl` (duration)
    # resolves to an absolute `expires_at`; an explicit `expires_at` is
    # validated. Both arrive via the free-form `metadata` dict (merged above),
    # so this single normalization is inherited by every interface. The
    # `archive-expired` sweep later flips an expired memory to status: archived.
    from palinode.consolidation.ttl import normalize_expiry as _normalize_expiry
    _expiry_err = _normalize_expiry(frontmatter_dict, now_iso=_now_iso)
    if _expiry_err:
        raise SaveValidationError(_expiry_err)
    # After the expiry so a new ttl beats the carried expires_at, and before the
    # ku_compat block, which fills lifecycle only when it is absent.
    if save_outcome in {"appended", "replaced"}:
        _carry_prior_frontmatter(
            frontmatter_dict,
            _existing_meta,
            has_entities=bool(entities) or bool((metadata or {}).get("entities")),
            extra_never_carried=(
                _REPLACE_BODY_BOUND if save_outcome == "replaced" else frozenset()
            ),
        )
    # Privacy belongs to the existing memory, regardless of write policy.
    # Caller-supplied values have already passed the Layer 2 validation above.
    for key in ("visibility", "scope", "access"):
        if key not in frontmatter_dict and key in _existing_meta:
            frontmatter_dict[key] = _existing_meta[key]
    if core is not None:
        frontmatter_dict["core"] = core
    if _effective_confidence is not None:
        frontmatter_dict["confidence"] = _effective_confidence
    if priority is not None:
        frontmatter_dict["priority"] = priority
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
    if resolved_update_policy is not None:
        frontmatter_dict["update_policy"] = resolved_update_policy
    # IETF KU frontmatter alignment — auto-populate KU fields when
    # ku_compat is enabled, or when the caller explicitly provides them.
    if config.ku_compat.enabled:
        if "ku_version" not in frontmatter_dict:
            frontmatter_dict["ku_version"] = config.ku_compat.ku_version
        if "lifecycle" not in frontmatter_dict:
            raw_status = frontmatter_dict.get("status") or (metadata or {}).get("status", "active")
            from palinode.core.parser import VALID_LIFECYCLES
            frontmatter_dict["lifecycle"] = raw_status if raw_status in VALID_LIFECYCLES else "active"
    # external SDLC object references (free-form dict[str, str]).
    if external_refs is not None:
        from palinode.core.parser import parse_external_refs as _parse_ext_refs
        validated = _parse_ext_refs({"external_refs": external_refs})
        if validated is not None:
            frontmatter_dict["external_refs"] = validated
        elif not external_refs and save_outcome == "replaced":
            frontmatter_dict["external_refs"] = {}
    # ADR-010: explicit ``title`` overrides metadata-supplied title.
    if title or (title is not None and save_outcome == "replaced"):
        frontmatter_dict["title"] = title
    # source-citation anchors. Only written when provided so frontmatter
    # stays clean otherwise. Validation (computes/verifies quote_hash, rejects
    # malformed entries with 400) happens here so a bad anchor is caught before
    # the file is written.
    if sources is not None:
        frontmatter_dict["sources"] = _normalize_sources(sources)

    # (G4): typed relationship links. Resolve each from param-or-metadata
    # (the explicit param wins — mirrors update_policy's H4 handling), validate,
    # and write the normalized list only when non-empty so frontmatter stays
    # clean otherwise. `_resolved_contradicts` is reused for the reciprocal
    # back-link after the file is written.
    _meta_dict = metadata if isinstance(metadata, dict) else {}
    _contradicts_in = (
        contradicts if contradicts is not None else _meta_dict.get("contradicts")
    )
    _backed_by_in = (
        backed_by if backed_by is not None else _meta_dict.get("backed_by")
    )
    _resolved_contradicts = _normalize_link_refs(_contradicts_in, "contradicts")
    _resolved_backed_by = _normalize_link_refs(_backed_by_in, "backed_by")
    if _resolved_contradicts or (
        _contradicts_in is not None and save_outcome == "replaced"
    ):
        frontmatter_dict["contradicts"] = _resolved_contradicts
    if _resolved_backed_by or (
        _backed_by_in is not None and save_outcome == "replaced"
    ):
        frontmatter_dict["backed_by"] = _resolved_backed_by

    # Claim-level source anchors: resolved param-or-metadata like the typed
    # links above (the explicit param wins; the metadata path was excluded from
    # the verbatim merge so a malformed entry still gets a clean 400). The
    # claim_id derivation is salted with this memory's path-relative ref, so
    # the normalizer needs the resolved category/slug. Written only when
    # non-empty so frontmatter stays clean otherwise.
    _claims_in = claims if claims is not None else _meta_dict.get("claims")
    if _claims_in is not None:
        _resolved_claims = _normalize_claims_or_reject(_claims_in, rel_ref)
        if _resolved_claims:
            frontmatter_dict["claims"] = _resolved_claims

    # ADR-010: explicit body field > X-Palinode-Source header > env > "api".
    # Levels 1-2 are resolved by the transport before it calls this function;
    # `default_source()` is levels 3-4.
    frontmatter_dict["source"] = source or default_source()

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
    if config.search.retrieval_mode != "lexical" and config.auto_summary.enabled and not frontmatter_dict.get("description"):
        description_pending = True
        # Leave description absent in frontmatter; watcher detects the missing
        # field and triggers /generate-summaries, which fills it.

    # Layer 2 wiki contract: auto-append See also footer for any entities
    # not already referenced as [[wikilinks]] in the body.
    footer_entities = normalized_entities
    if (
        save_outcome in {"appended", "replaced"}
        and isinstance(frontmatter_dict["entities"], list)
    ):
        # The final body needs a footer rebuilt from the final entities list;
        # only strings name a page, and the list may hold whatever a caller sent.
        footer_entities = [e for e in frontmatter_dict["entities"] if isinstance(e, str)]
    body_content = _apply_wiki_footer(effective_content, footer_entities)

    doc = f"---\n{yaml.safe_dump(frontmatter_dict, default_flow_style=False, allow_unicode=True)}---\n\n{body_content}\n"

    # Folders are made only once the save has passed every check, so a refusal leaves none behind.
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
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
    if config.search.retrieval_mode != "lexical" and config.auto_summary.enabled:
        is_core = bool(frontmatter_dict.get("core", False))
        has_summary = bool(frontmatter_dict.get("summary"))
        if (
            is_core
            and not has_summary
            and len(effective_content) >= config.auto_summary.min_content_chars
        ):
            summary_pending = True

    # Utilize auto backup procedures explicitly. One save = one per-file commit
    # via the git_tools mutation choke point (the single staging+commit primitive
    # all memory mutations route through).
    git_committed: bool = False
    git_error: str | None = None
    if config.git.auto_commit:
        commit_msg = f"{config.git.commit_prefix} auto-save: {rel_ref}"
        outcome = git_tools.try_commit_memory_files([file_path], commit_msg)
        git_committed = outcome.committed
        git_error = outcome.error
        if not git_committed:
            # exc_info-free: the choke point already logged the failure. Surface
            # a save-path signal so the git_committed contract has an
            # operator-visible breadcrumb on this logger too.
            logger.error(
                "Git auto-commit did not complete for %r: %s",
                file_path, git_error or "unknown",
            )
        elif push if push is not None else config.git.auto_push:
            try:
                # Through git_tools.push() — the same choke-point primitive
                # session-end already uses — rather than a raw subprocess.run.
                # It already logs a WARNING with the returncode/stderr on
                # failure, so there is nothing further to surface here beyond
                # the file this save touched.
                push_result = git_tools.push()
                if push_result.lower().startswith("push failed"):
                    logger.warning(
                        "git auto-push failed for %r: %s", file_path, push_result,
                    )
            except Exception as e:  # noqa: BLE001 — push must never fail the save
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
            source_ref = rel_ref.removesuffix(".md")
            add_reciprocal_contradicts(
                config.palinode_dir,
                source_ref,
                _resolved_contradicts,
                commit=config.git.auto_commit,
            )
        except Exception as exc:  # noqa: BLE001 — defensive: never fail the save
            logger.warning("reciprocal contradicts back-link skipped: %s", exc)

    logger.info(
        "Saved memory op=save file_path=%s id=%s category=%s "
        "save_outcome=%s git_committed=%s%s",
        file_path, frontmatter_dict["id"], category, save_outcome, git_committed,
        f" git_error={git_error!r}" if git_error else "",
    )

    # embed inline so that POST /save only returns once vector + FTS
    # entries actually exist. Previously the watcher embedded out-of-band,
    # leaving a race window where /search immediately after /save returned
    # zero results. The watcher remains the indexer for filesystem-direct
    # writes; this path covers API-driven saves.
    # embeddings.defer_embed_on_save opts out: FTS now, vectors on the watcher's pass.
    indexed = False
    embedded = False
    indexed_vec: bool = True
    indexed_fts: bool = True
    index_error: str | None = None
    try:
        from palinode.indexer.index_file import index_file
        outcome = index_file(
            file_path, defer_embed=config.embeddings.defer_embed_on_save
        )
        embedded = bool(outcome.get("embedded"))
        indexed = bool(outcome.get("indexed", embedded))
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
        "rel_path": to_rel_path(file_path),
        "id": frontmatter_dict["id"],
        "save_outcome": save_outcome,
        "disambiguated_from": disambiguated_from,
        "indexed": indexed,
        "embedded": embedded,
        "retrieval_mode": config.search.retrieval_mode,
        # Per-index health flags. vec/FTS failures are non-fatal
        # but silent — surface them so callers (MCP, CLI) can warn the user.
        "indexed_vec": indexed_vec,
        "indexed_fts": indexed_fts,
        # git_committed is True only when auto_commit is enabled AND the commit
        # subprocess succeeded. False when disabled or when git errors.
        "git_committed": git_committed,
    }
    if save_path is not None:
        result["path"] = save_path
    if git_error:
        result["git_error"] = git_error
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
    if config.search.retrieval_mode != "lexical" and config.consolidation.write_time.enabled:
        try:
            from palinode.consolidation import write_time
            item = {
                "content": content,
                "category": category,
                "type": type,
                "entities": entities or [],
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

    # Write-time forgetting: an explicit "please forget X" in the saved
    # content archives the resolved pref memories, while this save itself stays
    # active as the visible retraction record — which is why the hook runs
    # AFTER index_file, never before. Same save-never-fails contract as tier 2a.
    if config.consolidation.forget.enabled:
        try:
            from palinode.consolidation import forget as forget_mod
            forget_result = forget_mod.check_forget_on_save(
                file_path, content
            )
            if forget_result is not None:
                result["forget"] = forget_result
        except Exception as e:
            logger.warning(
                "forget check failed (non-fatal) op=forget_check file_path=%s error=%r",
                file_path, str(e), exc_info=True,
            )

    return result


__all__ = [
    "SaveValidationError",
    "default_source",
    "save_memory",
]
