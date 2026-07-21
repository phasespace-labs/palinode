"""ADR-009 scope chain resolution + visibility predicates.

Build a ScopeChain from config + env + caller-supplied project/session
(Layer 1), and decide whether a memory's frontmatter permits it on a given
chain — either scope-only (:func:`chain_allows`, Layer 1) or with the full
``visibility``/``access`` semantics (:func:`visible_on_chain`, Layer 2 /
#108). The context prime endpoint, the shared listing helper, and store
search all consume these. This module stays pure — no I/O, no DB; callers
own file access and iteration.

See ADR-009 §3.1-3.4 for the hierarchy, auto-detection, and access rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from palinode.core.config import Config


@dataclass(frozen=True)
class ScopeChain:
    """Ordered scope chain from narrowest (session) to broadest (org).

    Each level is an entity ref string (e.g. ``project/palinode``).
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
        """True when at least one *identity* level is set (#108).

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

    ``session_id`` is the caller-generated session identifier. Pass ``None``
    when session-level scoping is not in use.

    Other levels are read from :class:`ScopeConfig` (env vars override YAML).
    """
    s = cfg.scope
    return ScopeChain(
        session=session_id,
        agent=s.agent,
        harness=s.harness,
        project=project,
        member=s.member,
        org=s.org,
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
    consulted; #108's :func:`visible_on_chain` activates it only for the
    opt-in ``private``/``restricted`` visibilities, where fail-closed is the
    correct default.

    Non-string or blank ``scope`` values are treated as unscoped, matching the
    parser's soft-fail style.
    """
    raw = metadata.get("scope")
    if isinstance(raw, str) and raw.strip():
        return raw.strip() in chain.as_list()
    return True


def visible_on_chain(
    chain: ScopeChain,
    metadata: dict[str, Any],
    *,
    file_path: str | None = None,
) -> bool:
    """Layer 2 visibility (#108): does the session chain permit this memory?

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

    Access control is advisory — enforced here at the selection layer, not on
    disk (ADR-009 §3.4).
    """
    from palinode.core.parser import parse_scope

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
    """Access control alone — no scope isolation (ADR-009 §3.4, #108).

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
