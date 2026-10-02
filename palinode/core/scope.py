"""ADR-009 scope chain resolution + visibility predicates.

Build a ScopeChain from config + env + caller-supplied project/session
(Layer 1), and decide whether a memory's frontmatter permits it on a given
chain — either scope-only (:func:`chain_allows`, Layer 1) or with the full
``visibility``/``access`` semantics (:func:`visible_on_chain`, Layer 2 /
the ADR-009 layer 2 work). The context prime endpoint, the shared listing helper, and store
search all consume these. This module stays pure — no I/O, no DB; callers
own file access and iteration.

See ADR-009 §3.1-3.4 for the hierarchy, auto-detection, and access rules.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from palinode.core.config import Config


def scope_level_name(level: str, value: str | None) -> str | None:
    """Strip one own entity-ref prefix, preserving other prefixes literally."""
    return value.removeprefix(f"{level}/") if value is not None else None


@dataclass(frozen=True)
class ScopeChain:
    """Ordered scope chain from narrowest (session) to broadest (org).

    Each level stores a bare name or identifier without its level prefix
    (e.g. ``project="palinode"`` or ``session="abc-123"``). Callers normalize
    non-project entity refs with :func:`scope_level_name` and resolve project
    aliases before construction; :meth:`as_list` adds each level's prefix to
    produce entity refs (e.g. ``project/palinode``).
    Unset levels are dropped when serialized via :meth:`as_list`.
    The order of :meth:`as_list` is the search-priority order: earlier
    entries are more specific and take precedence over later ones.
    """
    session: str | None = None
    agent: str | None = None
    harness: str | None = None
    project: str | None = None
    member: str | None = None
    org: str | None = None
    #: Not a level: whether this request asked to see records tagged to a
    #: project other than :attr:`project` (see :func:`other_project`). Off by
    #: default, so a request carrying a project is isolated to it. Never part
    #: of :meth:`as_list`, which stays the chain's identity levels alone.
    include_other_projects: bool = False

    def as_list(self) -> list[str]:
        """Return the chain as entity refs, narrow → broad, omitting unset levels."""
        entries: list[tuple[str, str | None]] = [
            ("session", self.session),
            ("agent", self.agent),
            ("harness", self.harness),
            ("project", self.project),
            ("member", self.member),
            ("org", self.org),
        ]
        return [f"{kind}/{value}" for kind, value in entries if value]

    def is_empty(self) -> bool:
        """True when no levels are set (caller has zero scoping context)."""
        return not self.as_list()

    def has_identity(self) -> bool:
        """True when at least one *identity* level is set.

        The ``session`` level is deliberately excluded: a session id is
        ADR-007 recall-dedup telemetry, not a scope the memory author can
        write against, so a chain carrying only ``session/<id>`` describes no
        identity to isolate on. Callers use this — not :meth:`is_empty` — to
        decide whether scope isolation should engage, so a bare
        ``session_id`` never silently hides every explicitly-scoped memory.
        """
        return any((self.agent, self.harness, self.project, self.member, self.org))


def resolve_scope_chain(
    cfg: Config,
    project: str | None = None,
    session_id: str | None = None,
) -> ScopeChain:
    """Resolve the scope chain for the current session.

    ``project`` should be the caller-resolved project entity name (typically
    supplied by the ADR-008 ambient-context detection). Pass ``None`` in
    pre-ADR-008 setups or when the caller has no project signal.

    ``session_id`` is the caller-generated session identifier or its
    ``session/`` entity ref. Pass ``None`` when session-level scoping is not
    in use. Non-project levels strip only their own prefix.

    Other levels are read from :class:`ScopeConfig` (env vars override YAML).
    """
    s = cfg.scope
    return ScopeChain(
        session=scope_level_name("session", session_id),
        agent=scope_level_name("agent", s.agent),
        harness=scope_level_name("harness", s.harness),
        project=project,
        member=scope_level_name("member", s.member),
        org=scope_level_name("org", s.org),
    )


def chain_allows(chain: ScopeChain, metadata: dict[str, Any]) -> bool:
    """Scoped-mode visibility for one memory's frontmatter (ADR-009 Layer 1).

    A memory with an **explicit** ``scope:`` frontmatter field is visible only
    when that entity ref appears on the session's chain. A memory without one
    is always visible — identical to classic-mode behavior.

    Only explicit scope isolates, deliberately. The directory-inferred default
    (:func:`palinode.core.parser._default_scope_from_path`) yields
    ``project/<parent-dir>``, which in the standard category layout
    (``decisions/``, ``insights/``, …) produces refs like ``project/decisions``
    that no session chain ever contains — filtering on it would hide every
    legacy memory. ADR-009 §7 requires the opposite: "no scope = works as
    before". So for the ``inherited`` default the directory default is NOT
    consulted; :func:`visible_on_chain`, from the ADR-009 layer 2 work, activates it only for the
    opt-in ``private``/``restricted`` visibilities, where fail-closed is the
    correct default.

    Non-string or blank ``scope`` values are treated as unscoped, matching the
    parser's soft-fail style.
    """
    raw = metadata.get("scope")
    if isinstance(raw, str) and raw.strip():
        return raw.strip() in chain.as_list()
    return True


def project_entities(metadata: dict[str, Any]) -> list[str]:
    """The ``project/*`` refs a memory's ``entities`` frontmatter names."""
    raw = metadata.get("entities")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [
        e.strip() for e in raw
        if isinstance(e, str) and e.strip().lower().startswith("project/")
    ]


def _project_key(value: str) -> str:
    """A project slug or ``project/<slug>`` ref as a comparison key: bare, lowercased."""
    slug = value.strip()
    if slug.lower().startswith("project/"):
        slug = slug[len("project/"):]
    return slug.lower()


#: ``(alias map, member index, canonical spellings)``. Holds the map object
#: itself rather than its ``id()``: a map freed after a reload can hand its id
#: to the next one, and an id-keyed memo would then serve the old groups.
_INDEX_MEMO: tuple[dict, dict[str, str], dict[str, str]] | None = None


def _project_alias_index() -> tuple[dict[str, str], dict[str, str]]:
    """The curated entity aliases, as project comparison keys.

    Reads the store's ``entity-aliases.yaml`` through
    :func:`palinode.core.aliases.load_alias_map` — the same groups search's
    entity lookup already expands — and keeps its ``project/`` members only.
    Returns ``(member key -> canonical key, canonical key -> canonical ref as
    the file spells it)``. Keys are bare and lowercased, which is what makes
    the comparison case-insensitive. Memoized on the alias map object:
    ``load_alias_map`` returns the same object until the file changes.
    """
    global _INDEX_MEMO
    from palinode.core import aliases

    groups = aliases.load_alias_map()
    if _INDEX_MEMO is not None and _INDEX_MEMO[0] is groups:
        return _INDEX_MEMO[1], _INDEX_MEMO[2]
    index: dict[str, str] = {}
    spelled: dict[str, str] = {}
    for member in groups:
        if not member.lower().startswith("project/"):
            continue
        canonical = aliases.canonical_ref(member)
        if not canonical.lower().startswith("project/"):
            canonical = min(ref for ref in groups[member] if ref.lower().startswith("project/"))
        ckey = _project_key(canonical)
        index.setdefault(_project_key(member), ckey)
        index.setdefault(ckey, ckey)
        spelled.setdefault(ckey, canonical)
    _INDEX_MEMO = (groups, index, spelled)
    return index, spelled


def alias_index() -> dict[str, str]:
    """Every project key the curated aliases name -> its canonical key."""
    return _project_alias_index()[0]


def canonical_project(value: str, index: dict[str, str] | None = None) -> str:
    """The one place a project name is canonicalized for comparison.

    Case-insensitive (``Orbit_App`` and ``orbit_app`` are one project), and a
    member of a group in the store's ``entity-aliases.yaml`` maps to that
    group's canonical project. Returns the bare lowercased key; stored entities
    are never rewritten. ``index`` is a prebuilt :func:`alias_index`, for
    callers comparing many names at once.
    """
    key = _project_key(value)
    return (alias_index() if index is None else index).get(key, key)


def canonical_project_ref(ref: str) -> str:
    """``ref`` unchanged, unless it is a ``project/`` alias: then its canonical ref.

    Used by project resolution so a request resolved to an alias counts as its
    canonical project, spelled as ``entity-aliases.yaml`` spells it. A ref that
    is not an alias keeps its exact spelling, so a store with no alias file
    resolves exactly as before.
    """
    if not ref.lower().startswith("project/"):
        return ref
    index, spelled = _project_alias_index()
    key = _project_key(ref)
    canonical = canonical_project(ref, index)
    if canonical == key:
        return ref
    return spelled.get(canonical, f"project/{canonical}")


def other_project(chain: ScopeChain | None, metadata: dict[str, Any]) -> bool:
    """Is this memory tagged to a project other than the chain's?

    True when the chain carries a project **and** the memory names at least
    one ``project/*`` entity **and** none of them is the chain's project. A
    memory that names no project is global and never "other"; one that names
    the chain's project among several belongs to it. With no project on the
    chain there is nothing to be "other" than, so an unscoped request is
    unchanged. Pure: says nothing about whether the request opted in.

    Projects compare through :func:`canonical_project` on both sides:
    case-insensitively, and with every member of an ``entity-aliases.yaml`` group
    counted as its canonical project.
    """
    if chain is None or not chain.project:
        return False
    tagged = project_entities(metadata)
    if not tagged:
        return False
    index = alias_index()
    target = canonical_project(chain.project, index)
    return all(canonical_project(ref, index) != target for ref in tagged)


def with_other_projects(chain: ScopeChain | None) -> ScopeChain | None:
    """``chain`` with other projects' records let through, identity unchanged."""
    return replace(chain, include_other_projects=True) if chain is not None else None


def visible_on_chain(
    chain: ScopeChain,
    metadata: dict[str, Any],
    *,
    file_path: str | None = None,
) -> bool:
    """Layer 2 visibility: does the session chain permit this memory?

    Combines the Layer 1 scope test (:func:`chain_allows`) with the ADR-009
    §3.3-3.4 ``visibility`` / ``access`` semantics. This is the predicate the
    shared selection, digest, and search paths enforce; :func:`chain_allows`
    stays the pure scope-only test this delegates to for the default case.

    ``visibility`` (and ``access``) are parsed and validated by
    :func:`palinode.core.parser.parse_scope`, which coerces any malformed
    value back to ``inherited`` (with a soft warning), so this only ever
    branches on the three valid values:

    - ``inherited`` (the default, and the coerced-malformed case): Layer 1
      behavior — visible iff the memory's **explicit** ``scope:`` is on the
      chain, unscoped memories always visible (ADR-009 §7, absence-is-neutral).
      The directory-inferred default is deliberately not consulted, so legacy
      files never vanish under scoped selection.

    - ``private``: visible only to the owning scope — the memory's ``scope``
      (explicit, else the directory-inferred ``project/<dir>`` default) must be
      on the chain. There is no unscoped free pass: a ``private`` memory that
      names no owner falls back to its directory scope, which no real session
      chain contains, so it fails closed under scoping.

    - ``restricted``: visible only to sessions whose chain intersects the
      ``access`` allowlist (ADR-009 §3.4). ``scope`` is irrelevant — the
      allowlist is the sole gate, and an empty ``access`` hides the memory
      from everyone.

    ``file_path`` (when known) lets ``private`` resolve the directory-inferred
    owner. It **must** already be memory-dir-relative — an absolute path makes
    ``_default_scope_from_path`` infer ``project/<memory-dir-basename>`` for a
    root-level file where a relative path correctly infers nothing, which is
    how one surface can hide a memory the next one leaks. Callers go through
    :func:`palinode.core.visibility.is_visible`, which normalizes for them.

    **Project isolation** applies before all three: when the chain carries a
    project, a memory tagged to a *different* project (:func:`other_project`)
    is not visible unless the chain opted in with ``include_other_projects``.
    The project boost (ADR-008) ranks; this is what keeps a scoped request
    from being handed another project's decision at all.

    Access control is advisory — enforced here at the selection layer, not on
    disk (ADR-009 §3.4).
    """
    from palinode.core.parser import parse_scope

    if not chain.include_other_projects and other_project(chain, metadata):
        return False

    info = parse_scope(metadata, file_path=file_path)
    visibility = info["visibility"]

    if visibility == "restricted":
        chain_refs = set(chain.as_list())
        return any(ref in chain_refs for ref in info["access"])

    if visibility == "private":
        owner = info["scope"]  # explicit, else the directory-inferred default
        return owner is not None and owner in chain.as_list()

    # ``inherited`` — and any value parse_scope coerced back to it.
    return chain_allows(chain, metadata)


def access_allows(metadata: dict[str, Any], *, file_path: str | None = None) -> bool:
    """Access control alone — no scope isolation (ADR-009 §3.4, the ADR-009 layer 2 work).

    The rule for recall surfaces that carry **no session scope context**:
    ``GET /list`` (which the SessionStart hook injects from), classic-mode
    priming, and any search whose chain resolved to no identity level.

    A ``private`` or ``restricted`` memory is never returned by such a
    surface — with no identity in hand nothing can match an owner or an
    allowlist, and a memory the author flagged as non-shared must fail
    closed rather than default to visible. ``inherited`` memories pass
    untouched, **including explicitly-scoped ones**, so the classic
    ``/list`` contract and the ADR-009 §7 zero-migration promise both hold:
    scope is a *selection preference* that needs a chain to evaluate,
    while visibility is *access control* that applies unconditionally.
    """
    from palinode.core.parser import parse_scope

    return parse_scope(metadata, file_path=file_path)["visibility"] == "inherited"
