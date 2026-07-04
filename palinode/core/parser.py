"""
Palinode Markdown Parse Utilities
"""
from __future__ import annotations

import logging
import os
import frontmatter
import re
from typing import Any


logger = logging.getLogger("palinode.parser")

# ADR-009 §3.3: allowed values for the `visibility` frontmatter field.
VALID_VISIBILITIES: tuple[str, ...] = ("inherited", "private", "restricted")
DEFAULT_VISIBILITY: str = "inherited"

# Regex for Obsidian-style wikilinks: [[Target]] or [[Target|Display]]
_WIKILINK_RE = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]')

# Canonical schema kinds (from PROGRAM.md).  Used to detect typed wikilinks.
_CANONICAL_KINDS: frozenset[str] = frozenset(
    ("person", "project", "decision", "insight", "research", "daily")
)


def slugify(text: str) -> str:
    """Converts a standard text string to a URL-safe lowercase slug.

    Args:
        text (str): The raw section header or file title.

    Returns:
        str: The generated URL-safe slug stripped of special characters.
    """
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    return text.strip('-')


def canonicalize_wikilink(label: str, known_entities: list[str] | None = None) -> str:
    """Convert a raw wikilink label to its canonical ``kind/slug`` form.

    Canonicalization rules (per PROGRAM.md Wiki Maintenance § Canonicalization):
    - Lowercase, spaces → hyphens, strip leading/trailing hyphens.
    - If the label already contains a ``/`` (e.g. ``[[person/alice-smith]]``),
      treat it as an already-typed reference and just normalise the slug.
    - If the label matches one of the canonical kinds as a prefix separated by
      a space or hyphen (e.g. ``[[person alice smith]]``), parse that.
    - Otherwise try to match against ``known_entities`` (the frontmatter list):
      a slug-match means the two references point at the same entity, so return
      the known entity's canonical string.
    - If nothing matches, fall back to ``entity/<slug>`` where ``entity`` is a
      type-less sentinel that indicates the kind could not be inferred.

    Args:
        label: Raw wikilink content, e.g. ``"Alice Smith"`` or ``"person/alice-smith"``.
        known_entities: Optional list of already-canonical entity strings from the
            same file's frontmatter; used to detect label ↔ slug equivalences.

    Returns:
        Canonical entity string, e.g. ``"person/alice-smith"``.
    """
    label = label.strip()

    # Already contains a slash → typed ref; just normalise
    if "/" in label:
        kind, _, rest = label.partition("/")
        kind = kind.lower().strip()
        slug = re.sub(r'[^a-z0-9]+', '-', rest.lower()).strip('-')
        return f"{kind}/{slug}"

    # Check if label slug matches any known entity's slug portion
    label_slug = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')
    if known_entities:
        for entity in known_entities:
            if "/" in entity:
                _, _, ent_slug = entity.partition("/")
                if ent_slug == label_slug:
                    return entity  # exact slug match → same entity

    # Fall back: no type can be inferred
    return f"entity/{label_slug}"


def parse_entities(
    metadata: dict[str, Any],
    body: str,
) -> dict[str, Any]:
    """Extract and merge entity references from frontmatter and body wikilinks.

    Reads two surfaces:
    - ``entities:`` frontmatter field (list of canonical ``kind/slug`` strings).
    - ``[[wikilink]]`` patterns anywhere in *body* (outside the auto-footer too).

    Returns a dict with three keys:

    ``entities_frontmatter``
        The raw list from ``metadata['entities']``, or ``[]``.  Preserved
        unchanged for back-compat and for the ``wiki_drift`` lint check.

    ``entities_body``
        Canonicalised list of entities found via ``[[wikilinks]]`` in *body*
        (including under the auto-footer).  Each label is resolved against
        ``entities_frontmatter`` first so that ``[[Alice Smith]]`` and
        ``person/alice-smith`` are recognised as the same entity.

    ``entities_resolved``
        Merged, deduplicated union of the two surfaces.  This is the field
        downstream consumers should use.  Ordering: frontmatter entries first,
        then body-only additions, both in stable insertion order.

    Args:
        metadata: Parsed frontmatter dict (as returned by ``parse_markdown``).
        body: Markdown body text (frontmatter stripped).

    Returns:
        Dict with keys ``entities_frontmatter``, ``entities_body``,
        ``entities_resolved``.
    """
    # ── Surface 1: frontmatter ────────────────────────────────────────────────
    raw_fm = metadata.get("entities", [])
    if isinstance(raw_fm, list):
        entities_fm: list[str] = [str(e).strip() for e in raw_fm if e]
    else:
        entities_fm = []

    # ── Surface 2: body wikilinks ─────────────────────────────────────────────
    raw_labels = _WIKILINK_RE.findall(body)
    entities_body: list[str] = []
    seen_body: set[str] = set()
    for label in raw_labels:
        canonical = canonicalize_wikilink(label.strip(), known_entities=entities_fm)
        if canonical not in seen_body:
            seen_body.add(canonical)
            entities_body.append(canonical)

    # ── Merge ─────────────────────────────────────────────────────────────────
    # Build slug-set from frontmatter for dedup against body entries.
    fm_set = set(entities_fm)
    resolved: list[str] = list(entities_fm)  # frontmatter entries first
    for ent in entities_body:
        if ent not in fm_set:
            resolved.append(ent)

    return {
        "entities_frontmatter": entities_fm,
        "entities_body": entities_body,
        "entities_resolved": resolved,
    }


def _build_canonical_question_prefix(metadata: dict[str, Any]) -> str:
    """Build a text prefix from canonical_question frontmatter.

    Accepts a single string or a list of strings.  Returns a formatted
    prefix like ``"Q: …\\n\\n"`` ready to be prepended to chunk content,
    or an empty string if the field is absent.
    """
    cq = metadata.get("canonical_question")
    if not cq:
        return ""

    if isinstance(cq, str):
        questions = [cq]
    elif isinstance(cq, list):
        questions = [str(q) for q in cq if q]
    else:
        return ""

    if not questions:
        return ""

    lines = [f"Q: {q}" for q in questions]
    return "\n".join(lines) + "\n\n"


def parse_markdown(content: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Parses a complete markdown string payload containing YAML frontmatter.

    Automatically handles text segmentation. If the markdown document is 
    extremely short, it will remain categorized natively as a single "root" chunk.
    Larger documents are contextually broken apart via H2 (##) or H3 (###) headers.

    Args:
        content (str): Complete file content string including frontmatter and markdown body.

    Returns:
        tuple: A nested pair containing:
            - metadata (dict[str, Any]): Loaded YAML variables extracted via 'python-frontmatter'.
            - sections (list[dict[str, str]]): Document subdivided portions comprising `section_id` 
              and their relative `content`.
    """
    try:
        post = frontmatter.loads(content)
        metadata = post.metadata
        body = post.content
    except Exception:
        metadata = {}
        body = content

    # Build canonical question prefix from frontmatter (string or list of strings).
    cq_prefix = _build_canonical_question_prefix(metadata)

    # If document is short (~500 tokens corresponds to roughly 2000 chars), keep it safely bound
    # to a single core chunk to prevent semantic fracturing.
    if len(body) < 2000:
        return metadata, [{"section_id": "root", "content": cq_prefix + body}]

    # Split by h2 or h3 natively formatted headers.
    # regex intentionally matches lines starting exclusively with ## or ###
    heading_pattern = re.compile(r'^(#{2,3})\s+(.*)$', re.MULTILINE)
    
    sections = []
    
    parts = heading_pattern.split(body)
    
    preamble = parts[0].strip()
    if preamble:
        sections.append({
            "section_id": "root",
            "content": preamble
        })
        
    for i in range(1, len(parts), 3):
        if i + 2 >= len(parts):
            break
        level = parts[i]
        heading_text = parts[i+1]
        section_content = parts[i+2]
        
        full_content = f"{level} {heading_text}\n{section_content}".strip()
        slug = slugify(heading_text)
        
        if full_content:
            sections.append({
                "section_id": slug,
                "content": full_content
            })

    # Failsafe fallback: Handle missing headers implicitly.
    if not sections:
        sections = [{"section_id": "root", "content": body}]

    # Prepend canonical question prefix to the first chunk so the
    # embedding captures the question semantics the file answers.
    if cq_prefix and sections:
        sections[0]["content"] = cq_prefix + sections[0]["content"]

    return metadata, sections


# ── IETF KU frontmatter parsing (issue) ─────────────────────────────────

# Allowed values for the KU `lifecycle` field (mirrors `status` vocabulary).
VALID_LIFECYCLES: tuple[str, ...] = ("active", "archived", "deprecated")
DEFAULT_LIFECYCLE: str = "active"

# ADR-015 §2.1: write-semantics axis, orthogonal to `type`.
#   append  — current behaviour; every save is an episodic file (the default).
#   replace — re-saving the same (folder, slug) updates the one file in place;
#             the file is a living/current-state document. Consolidation must
#             never SUPERSEDE/ARCHIVE-into-history a `replace` doc (executor
#             guard), and `created_at` is preserved across overwrites (PR-A).
# Persisted as sticky frontmatter so the file declares its own regime.
VALID_UPDATE_POLICIES: tuple[str, ...] = ("append", "replace")
DEFAULT_UPDATE_POLICY: str = "append"

# ADR-015 §2.2: incident lifecycle lives in the `status` field on a
# *fixed* type — no new memory types. These values are deliberately DISJOINT
# from VALID_LIFECYCLES (active/archived/deprecated) and from the default
# search-exclusion set (`config.search.exclude_status` == ["archived"]), so an
# incident's status never collides with status-based recall exclusion: an
# `open`/`monitoring`/`resolved` incident is never silently excluded from
# search. See `VALID_STATUSES` for the combined writer-supplied allow-set.
VALID_INCIDENT_STATUSES: tuple[str, ...] = ("open", "monitoring", "resolved")

# Combined allow-set for a writer-supplied `status` field: the existing
# lifecycle vocabulary (which the parser already maps to KU `lifecycle` and the
# store uses for `exclude_status` filtering) PLUS the incident lifecycle. A
# `status` outside this set is rejected at the save surface so a typo'd status
# can't silently slip past exclusion logic.
VALID_STATUSES: tuple[str, ...] = VALID_LIFECYCLES + VALID_INCIDENT_STATUSES

# (ADR-018): epistemic markers — a per-memory declaration of what KIND of
# claim the memory makes, orthogonal to `type` and `status`. These are the
# SETTABLE values (what the save surfaces accept and validate):
#   fact          — directly observed / verified (an explicit, earned claim).
#   inference     — derived or extrapolated from other facts (lower trust;
#                   should ideally carry a `backed_by` link).
#   open_question — unresolved; an explicit marker that this is NOT yet settled.
#                   `lint` flags long-lived open questions as a staleness signal.
VALID_EPISTEMICS: tuple[str, ...] = ("fact", "inference", "open_question")

# The ABSENCE of the field is its own state — `unmarked`: no epistemic claim was
# made. Crucially this is NOT equated with `fact`; for an audit-grade store,
# "nobody declared this" must not silently inherit the authority of "verified".
# `unmarked` is trust-NEUTRAL — it is neither flagged as a problem (no lint
# noise) nor asserted as verified (downstream consumers like treat it
# as "no claim", not as fact). It is not a settable value (you reach it only by
# omitting the field), so it is intentionally NOT in VALID_EPISTEMICS; the save
# surface rejects an explicit `unmarked`. Every memory written before this field
# existed is `unmarked` and is byte-for-byte unaffected. To assert fact-hood a
# writer sets `epistemic: fact` explicitly.
DEFAULT_EPISTEMIC: str = "unmarked"


def parse_ku_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    """Extract IETF Knowledge Unit frontmatter fields from parsed metadata.

    Recognizes three additive fields that align with the
    ``draft-farley-acta-knowledge-units`` specification:

    ``ku_version``
        String; expected to be ``"1.0"`` for the current draft.  Returned
        as-is when present; ``None`` otherwise.

    ``confidence``
        Float in 0.0–1.0.  Invalid values (outside range or wrong type) are
        coerced to ``None`` and a warning is logged — soft-fail, consistent
        with the rest of the parser.

    ``lifecycle``
        One of ``"active"``, ``"archived"``, ``"deprecated"``.  When absent,
        falls back to ``status`` if that field is one of the valid values;
        otherwise defaults to ``"active"``.  Invalid values log a warning.

    All three fields are purely additive — missing fields return ``None`` /
    the default rather than raising.  Existing files that lack these fields
    parse without issue (backward compat).

    Args:
        metadata: Parsed frontmatter dict (as returned by ``parse_markdown``).

    Returns:
        Dict with keys ``ku_version`` (str | None), ``confidence``
        (float | None), ``lifecycle`` (str).
    """
    # ku_version — accept any string
    raw_version = metadata.get("ku_version")
    ku_version: str | None = str(raw_version).strip() if raw_version is not None else None

    # confidence — float in [0.0, 1.0]
    raw_conf = metadata.get("confidence")
    confidence: float | None = None
    if raw_conf is not None:
        try:
            conf_val = float(raw_conf)
            if 0.0 <= conf_val <= 1.0:
                confidence = conf_val
            else:
                logger.warning(
                    "Invalid confidence %r (must be 0.0–1.0); ignoring.", raw_conf
                )
        except (TypeError, ValueError):
            logger.warning("Invalid confidence %r (not a number); ignoring.", raw_conf)

    # lifecycle — with fallback to `status`
    raw_lc = metadata.get("lifecycle")
    if isinstance(raw_lc, str) and raw_lc in VALID_LIFECYCLES:
        lifecycle = raw_lc
    else:
        if raw_lc is not None:
            logger.warning(
                "Invalid lifecycle %r (expected one of %s); falling back.",
                raw_lc,
                VALID_LIFECYCLES,
            )
        # Fall back to `status` field if it maps to a KU lifecycle value
        raw_status = metadata.get("status", "active")
        if isinstance(raw_status, str) and raw_status in VALID_LIFECYCLES:
            lifecycle = raw_status
        else:
            lifecycle = DEFAULT_LIFECYCLE

    return {
        "ku_version": ku_version,
        "confidence": confidence,
        "lifecycle": lifecycle,
    }


# ── ADR-009 §3.3: scope frontmatter parsing ───────────────────────────────


def _default_scope_from_path(file_path: str) -> str | None:
    """Infer a default scope entity ref from a memory file's location.

    Per ADR-009 §3.3 / §7: a memory file with no explicit ``scope`` frontmatter
    defaults to ``project/<directory>`` where ``<directory>`` is the
    immediate parent directory name (e.g. ``decisions/foo.md`` →
    ``project/decisions``). Returns ``None`` when no parent directory name
    can be determined (e.g. a bare filename or empty string), which lets the
    caller decide how to handle a path-less parse.
    """
    if not file_path:
        return None
    parent = os.path.basename(os.path.dirname(file_path))
    if not parent:
        return None
    return f"project/{parent}"


def parse_external_refs(metadata: dict[str, Any]) -> dict[str, str] | None:
    """Extract and validate ``external_refs`` from frontmatter.

    The field is expected to be a flat ``dict[str, str]``.  Values that are
    not already strings are coerced with ``str()`` (e.g. ints). Nested
    dicts/lists trigger a soft warning and are dropped from the result so
    they don't corrupt downstream consumers.

    Returns the validated dict, or ``None`` when the field is absent.

    Args:
        metadata: Parsed frontmatter dict (as returned by ``parse_markdown``).

    Returns:
        ``dict[str, str]`` of external references, or ``None`` when the field
        is not present in the frontmatter.
    """
    raw = metadata.get("external_refs")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "external_refs must be a dict, got %s — field ignored",
            type(raw).__name__,
        )
        return None

    result: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, (dict, list)):
            logger.warning(
                "external_refs[%r] has nested value (type %s); "
                "nested values are not supported — entry dropped",
                key,
                type(value).__name__,
            )
        else:
            result[str(key)] = str(value)

    return result if result else None


def parse_sources(metadata: dict[str, Any]) -> list[dict[str, str]]:
    """Extract the ``sources:`` quote anchors from parsed frontmatter (#459).

    Each anchor is a ``{ref, quote, quote_hash}`` dict pointing a memory at the
    exact passage it cites in another file. This is the typed accessor for the
    capture half of the source-citation path; the verifier
    (:mod:`palinode.core.quote_verify`) reads the same ``sources:`` list.

    Returns a list of normalized anchor dicts. Malformed entries (non-dict, or
    missing ``ref``/``quote``) are dropped with a soft warning — consistent with
    the parser's existing soft-fail style. Returns ``[]`` when the field is
    absent or not a list, so a file with no anchors round-trips as a clean
    no-op.

    Args:
        metadata: Parsed frontmatter dict (as returned by ``parse_markdown``).

    Returns:
        List of ``{ref, quote, quote_hash}`` dicts (``quote_hash`` may be ``""``).
    """
    raw = metadata.get("sources")
    if not isinstance(raw, list):
        return []

    anchors: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning("sources entry is not a dict (%s); dropped", type(entry).__name__)
            continue
        ref = str(entry.get("ref", "")).strip()
        quote = str(entry.get("quote", "")).strip()
        if not ref or not quote:
            logger.warning("sources entry missing ref or quote; dropped")
            continue
        anchors.append({
            "ref": ref,
            "quote": quote,
            "quote_hash": str(entry.get("quote_hash", "")).strip(),
        })
    return anchors


def parse_scope(
    metadata: dict[str, Any],
    file_path: str | None = None,
) -> dict[str, Any]:
    """Extract scope, visibility, and access from frontmatter (ADR-009 §3.3).

    Returns a dict with the keys ``scope`` (str | None), ``visibility``
    (str, one of :data:`VALID_VISIBILITIES`), and ``access`` (list[str]).

    Defaults:
      - ``scope``: the value of ``metadata['scope']`` if present, else the
        directory-inferred default (``project/<parent-dir>``) when
        ``file_path`` is given, else ``None``.
      - ``visibility``: ``metadata['visibility']`` if it is one of the three
        allowed strings; otherwise :data:`DEFAULT_VISIBILITY` (a warning is
        logged for invalid values, matching the parser's existing
        soft-fail style for malformed metadata).
      - ``access``: ``metadata['access']`` coerced to ``list[str]`` if it is
        a list; otherwise ``[]``. Only meaningful when
        ``visibility == "restricted"`` per ADR-009 §3.4.

    This helper is purely additive — it does not modify ``metadata`` and does
    not affect :func:`parse_markdown`'s return shape. Slice 3 will consume
    the result when wiring scope into search.
    """
    raw_scope = metadata.get("scope")
    if isinstance(raw_scope, str) and raw_scope.strip():
        scope: str | None = raw_scope.strip()
    else:
        scope = _default_scope_from_path(file_path) if file_path else None

    raw_vis = metadata.get("visibility", DEFAULT_VISIBILITY)
    if isinstance(raw_vis, str) and raw_vis in VALID_VISIBILITIES:
        visibility = raw_vis
    else:
        if "visibility" in metadata:
            logger.warning(
                "Invalid visibility %r (expected one of %s); falling back to %r",
                raw_vis,
                VALID_VISIBILITIES,
                DEFAULT_VISIBILITY,
            )
        visibility = DEFAULT_VISIBILITY

    raw_access = metadata.get("access", [])
    if isinstance(raw_access, list):
        access = [str(a) for a in raw_access if a is not None and str(a).strip()]
    else:
        access = []

    return {"scope": scope, "visibility": visibility, "access": access}
