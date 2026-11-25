"""The operation spec: one definition per operation, every surface renders it (ADR-022).

Two things live here:

* **The cross-surface vocabularies:** the enums a surface must use verbatim
  for a parameter, and the protocol constants that travel with them. They moved
  here from ``core/parity.py``, which keeps only parity test bookkeeping and
  re-exports them, so production code never imports the test registry.
* **The operation specs:** each migrated operation's parameters, with their
  structure (name, type, enum, ``required``), whether the server owns the
  default, and how a surface forwards a value (:func:`build_payload`). The MCP
  ``inputSchema`` properties are generated from a spec (:func:`schema_properties`)
  and the MCP and CLI request bodies are built from it. Click options and the
  REST pydantic models stay hand-written and are checked against it by tests.
  Descriptions and help text stay with each surface (ADR-010).

**The server owns defaults.** A surface forwards only what the caller set, so a
param the server defaults is simply absent. A surface may show a different
default to a person (a *display override*), and the spec records each one with
its reason; it never changes what a program gets.

Migration is incremental (ADR-022 §8). An operation is defined either here or
in ``parity.py``'s ``CanonicalParam`` form, never both.

**Standard library only.** ``scripts/dump-parity-registry.py`` loads
``parity.py`` (and so this module) with a bare ``python3`` in a CI job that
has no project dependencies installed. ``tests/test_parity_is_dependency_free.py``
enforces it for both modules.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


# ─────────────────────────────────────────────────────────────────────────────
# Canonical category + type sets
# ─────────────────────────────────────────────────────────────────────────────


#: The canonical ``category`` enum.  Matches the memory-directory names
#: (plural) — that is the value the ``chunks.category`` column stores
#: (``palinode/api/server.py:660-668`` and the watcher's directory-basename
#: derivation).  Surfaces that expose a ``category`` filter MUST use
#: this exact tuple — ADR-010, finding.
CATEGORIES: tuple[str, ...] = (
    "people",
    "projects",
    "decisions",
    "insights",
    "research",
)

#: The canonical memory ``type`` enum (used by save).  Lives here so
#: the API can validate ``SaveRequest.type`` server-side instead of
#: relying on per-surface enum lists. ADR-010, finding.
MEMORY_TYPES: tuple[str, ...] = (
    "PersonMemory",
    "Decision",
    "ProjectSnapshot",
    "Insight",
    "ResearchRef",
    "ActionItem",
)

#: The canonical write-semantics enum (ADR-015 §5). ``append`` is episodic;
#: ``replace`` marks a living/current-state document. Defined here rather than
#: in ``core/parser.py`` for the same reason ``CATEGORIES`` and ``MEMORY_TYPES``
#: are: the surfaces that must agree on it should all import one tuple, and this
#: module is the one every surface can import (see the dependency note at the
#: top). ``core/parser.py`` re-exports it, so the many
#: ``from palinode.core.parser import VALID_UPDATE_POLICIES`` call sites are
#: unaffected.
VALID_UPDATE_POLICIES: tuple[str, ...] = ("append", "replace")

#: The canonical epistemic-marker enum (ADR-018) — the KIND of claim a memory
#: makes, orthogonal to ``type``. The ABSENCE of the field is its own state
#: (``unmarked``) and is deliberately NOT a member: for an audit-grade store,
#: "nobody declared this" must not silently inherit the authority of "verified".
VALID_EPISTEMICS: tuple[str, ...] = (
    "fact",
    "inference",
    "open_question",
    "unverified",
)

#: The Auditable Memory Records (AMR) specification version every record the
#: save path writes declares in its ``auditable_memory`` frontmatter field.
#: The declaration is REQUIRED by the spec (§4.1) and is what makes a
#: conforming record distinguishable from one that merely looks similar — an
#: implementation that emits no declaration conforms at no level (§7).
AMR_SPEC_VERSION: str = "0.1"

#: Versions the save surface recognises. A caller-supplied
#: ``auditable_memory`` outside this set is rejected, never guessed at (§4.1).
VALID_AMR_VERSIONS: tuple[str, ...] = (AMR_SPEC_VERSION,)

#: The canonical read-tier enum — how much of a memory a caller wants
#: back. Tiers are deterministic views computed at read time, never a second
#: content store; ``full`` is the content unchanged and is what every surface
#: returns when the caller says nothing.
TIERS: tuple[str, ...] = (
    "abstract",
    "overview",
    "full",
)

#: The canonical evidence-resolution enum on search — how much of the
#: evidence around each hit to gather (``palinode.core.evidence``). ``none``
#: is what every surface does when the caller says nothing; ``linked``
#: follows the typed links both ways under fixed budgets; ``full`` adds
#: bounded unlinked discovery. Read-only on every surface.
RESOLVE_MODES: tuple[str, ...] = (
    "none",
    "linked",
    "full",
)

#: The canonical intent enum on the bounded-resolution operation — what the
#: caller is asking the store to answer. ``current_state`` is the only value
#: and every surface defaults to it; the field exists so an as-of / known-at
#: question has somewhere to land once the temporal-assertion work defines
#: those semantics. Until then no surface advertises them, because a
#: parameter a caller can pass and the resolver cannot honour is worse than
#: the absence of one.
RESOLVE_INTENTS: tuple[str, ...] = (
    "current_state",
)

#: The canonical correction-review action enum. ``supersede`` records a
#: replacement and archives the original with ``superseded_by``; ``retire``
#: withdraws it with no successor. Both end at ``status: archived`` — the same
#: two outcomes ``palinode archive`` has always had, now reachable through a
#: previewed, revision-checked, confirmed review. Mirrored (not redefined) in
#: :mod:`palinode.corrections.review`, which keeps this module stdlib-only.
CORRECTION_ACTIONS: tuple[str, ...] = (
    "supersede",
    "retire",
)

#: The value ``applied`` carries when a document-level correction got its
#: first write in and not its second: the replacement is saved, the original
#: is still current. Neither a refusal (which writes nothing) nor a success,
#: so it is a third value rather than a boolean. Every surface reads it to
#: decide how to report; mirrored (not redefined) in
#: :mod:`palinode.corrections.review`, which keeps this module stdlib-only.
CORRECTION_APPLIED_PARTIAL = "partial"

#: The canonical prompt-task enum.  Single source replacing the duplicate
#: ``"enum"`` keys at ``palinode/mcp.py:624-625``. ADR-010, finding.
PROMPT_TASKS: tuple[str, ...] = (
    "compaction",
    "extraction",
    "update",
    "classification",
    "nightly-consolidation",
)


# ─────────────────────────────────────────────────────────────────────────────
# Parameter spec and the one forwarding rule
# ─────────────────────────────────────────────────────────────────────────────


ParamType = Literal["string", "boolean", "array", "integer", "number", "object"]


def coerce_str_array(value: Any) -> Any:
    """Tolerate JSON-encoded array strings from MCP clients that double-encode.

    Some MCP transports/clients serialize array arguments as JSON strings
    (e.g. ``'["a","b"]'``) instead of native arrays. FastAPI's Pydantic
    validation rejects those with "expected array, received string". This
    helper decodes the string form when it's clearly a JSON array; otherwise
    it returns ``value`` unchanged so native lists pass through.

    Despite the name, element types are never inspected — any decoded JSON list
    is returned as-is. That is why it applies equally to arrays of objects
    (``sources``, ``claims``) and arrays of strings (``entities``,
    ``contradicts``, ``backed_by``). Applied to EVERY array parameter on a
    write-path tool: guarding only some produced a partial experience where one
    array silently worked and the rest failed in three different shapes, which
    is harder to diagnose than uniform failure.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
        if isinstance(decoded, list):
            return decoded
    return value


def as_bool(value: Any) -> bool:
    """A boolean from a JSON-typed transport, where ``"false"`` must not read as true."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


@dataclass(frozen=True)
class Param:
    """One operation parameter: its structure, and how a surface hands it over.

    Attributes:
        name: Canonical parameter name — what the capability and the API model
            call it, regardless of what a surface named its own argument.
        array: Run :func:`coerce_str_array` over the value, so a double-encoded
            JSON array string arrives as a list on every surface. Implied by
            ``type="array"``.
        omit_if_empty: Elide a falsy-but-not-None value as well as ``None``.
            For free-text strings where ``""`` is indistinguishable from
            absent, and for filter lists, where ``[]`` filters nothing and so
            means the same as absent. Never set it on a list whose emptiness is
            an assertion (``contradicts: []``): that is the bug the
            not-None rule exists to close.
        cast: Applied to a non-None value before inclusion, so a surface that
            receives ``"0.8"`` from a JSON-typed transport sends ``0.8``.
        type: The JSON type surfaces advertise. ``None`` on the write specs
            that predate typed specs (save, session-end).
        required: The caller must supply it.
        enum: The allowed values: of the param itself, or of each item when
            ``type="array"``.
        server_default: The server fills this when it is absent, from its own
            configuration (the config key). Surfaces never send a default for
            it, and generated schemas carry no ``default``.
        flag: A switch whose off state is the server's own default: only
            ``True`` is sent, so an explicit ``false`` and an absent flag are
            the same request.
        unset_values: Values that mean "the server's default" and are not sent
            (``resolve="none"``).
        fields: The named fields of an object: of the param itself when
            ``type="object"``, or of each item when ``type="array"`` (save's
            ``sources`` and ``claims``). Each is a :class:`Param` with a type and
            ``required``; a field may have fields of its own. Empty for a free
            object (``metadata``) or a list of strings.
        surfaces: The surfaces that advertise this param, when not all of them;
            ``None`` means every surface. ``surfaces_reason`` says why the others
            do not (session-end's provenance, which a harness supplies).
    """

    name: str
    array: bool = False
    omit_if_empty: bool = False
    cast: Callable[[Any], Any] | None = None
    type: ParamType | None = None
    required: bool = False
    enum: tuple[str, ...] | None = None
    server_default: str | None = None
    flag: bool = False
    unset_values: tuple[Any, ...] = ()
    fields: tuple[Param, ...] = ()
    surfaces: tuple[str, ...] | None = None
    surfaces_reason: str | None = None

    def on(self, surface: str | None) -> bool:
        """Whether *surface* advertises this param (``None``: any surface)."""
        return surface is None or self.surfaces is None or surface in self.surfaces


def build_payload(
    params: Iterable[Param], values: Mapping[str, Any]
) -> dict[str, Any]:
    """Assemble a canonical payload from one surface's raw values.

    The single inclusion rule, applied identically on every surface: a value is
    included when it ``is not None``, so an explicitly-empty list or dict
    survives as the assertion the caller made. ``omit_if_empty`` params drop a
    blank value too, a ``flag`` is sent only when true, and a value in
    ``unset_values`` is not sent.

    Unknown keys in *values* are ignored, so a surface can pass its whole
    argument mapping without filtering first.

    Args:
        params: The spec for this operation (``SEARCH.params``, ``SAVE.params``,
            ``SESSION_END.params``).
        values: Raw values as the surface received them.

    Returns:
        The payload dict, containing only the parameters that should be sent.
    """
    payload: dict[str, Any] = {}
    for param in params:
        value = values.get(param.name)
        if value is None:
            continue
        if param.flag:
            if as_bool(value):
                payload[param.name] = True
            continue
        if param.array or param.type == "array":
            value = coerce_str_array(value)
        if param.omit_if_empty and not value:
            continue
        if value in param.unset_values:
            continue
        if param.cast is not None:
            value = param.cast(value)
        payload[param.name] = value
    return payload


@dataclass(frozen=True)
class DisplayOverride:
    """A default a surface shows a person in place of the server's.

    Applies only where a person reads the output; a program (piped or JSON
    output) gets the server default. Recorded here so the exception is a
    reviewed fact with a reason, not a literal in one surface.
    """

    surface: str
    param: str
    value: Any
    reason: str


@dataclass(frozen=True)
class Shorthand:
    """Surface sugar that supplies a required param, so those surfaces need not ask for it.

    The server always receives the param; a surface listed here may fill it
    from its own sugar (``ps`` sets save's ``type``), so its schema does not
    mark the param required.
    """

    param: str
    by: str
    surfaces: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class OperationSpec:
    """One operation's parameters, as every surface must forward them."""

    name: str
    endpoint: str
    params: tuple[Param, ...]
    display_overrides: tuple[DisplayOverride, ...] = field(default=())
    shorthands: tuple[Shorthand, ...] = field(default=())

    def param(self, name: str) -> Param:
        for p in self.params:
            if p.name == name:
                return p
        raise KeyError(name)

    def display_default(self, surface: str, name: str) -> Any:
        """The value *surface* shows a person for *name*, or ``None`` (server's)."""
        for o in self.display_overrides:
            if o.surface == surface and o.param == name:
                return o.value
        return None


def _field_schema(p: Param, descriptions: Mapping[str, str], prefix: str) -> dict[str, Any]:
    """The JSON Schema of one (possibly nested) field, descriptions keyed by dotted path."""
    prop: dict[str, Any] = {"type": p.type}
    if p.type == "array":
        item: dict[str, Any] = {"type": "object" if p.fields else "string"}
        if p.enum:
            item["enum"] = list(p.enum)
        if p.fields:
            item.update(_object_schema(p.fields, descriptions, prefix))
        prop["items"] = item
    elif p.fields:
        prop.update(_object_schema(p.fields, descriptions, prefix))
    elif p.enum:
        prop["enum"] = list(p.enum)
    return prop


def _object_schema(
    fields: tuple[Param, ...], descriptions: Mapping[str, str], prefix: str
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for f in fields:
        path = f"{prefix}.{f.name}"
        prop = _field_schema(f, descriptions, path)
        if path in descriptions:
            prop["description"] = descriptions[path]
        properties[f.name] = prop
    out: dict[str, Any] = {"properties": properties}
    required = [f.name for f in fields if f.required]
    if required:
        out["required"] = required
    return out


def schema_properties(
    spec: OperationSpec, extras: Mapping[str, Mapping[str, Any]] | None = None,
    surface: str | None = None,
) -> dict[str, dict[str, Any]]:
    """JSON Schema ``properties`` for *spec*, in spec order, with no ``default``.

    *extras* carries what the spec deliberately does not own: each surface's
    ``description`` and any surface-only bound (MCP caps ``limit`` lower than
    the API does). A param with nested ``fields`` takes its fields'
    descriptions as ``"fields": {"ref": "...", "span.quote": "..."}``, keyed by
    dotted path below the param. A key in *extras* that would restate
    structure the spec owns (``type``, ``enum``, ``items``, ``properties``,
    ``required``, ``default``) is refused. With *surface*, only the params that
    surface advertises are rendered.
    """
    extras = extras or {}
    params = [p for p in spec.params if p.on(surface)]
    unknown = set(extras) - {p.name for p in params}
    if unknown:
        raise ValueError(f"extras for params not in {spec.name}: {sorted(unknown)}")
    props: dict[str, dict[str, Any]] = {}
    for p in params:
        if p.type is None:
            raise ValueError(f"{spec.name}.{p.name} has no type")
        extra = dict(extras.get(p.name, {}))
        owned = {"type", "enum", "items", "properties", "required", "default"} & set(extra)
        if owned:
            raise ValueError(f"extras for {spec.name}.{p.name} restate {sorted(owned)}")
        nested = {f"{p.name}.{k}": v for k, v in extra.pop("fields", {}).items()}
        prop = _field_schema(p, nested, p.name)
        prop.update(extra)
        props[p.name] = prop
    return props


def required_params(spec: OperationSpec, surface: str | None = None) -> list[str]:
    """The params *surface* must mark required: the spec's, less any its own sugar supplies."""
    sugared = {s.param for s in spec.shorthands if surface in s.surfaces}
    return [p.name for p in spec.params
            if p.required and p.on(surface) and p.name not in sugared]


# ─────────────────────────────────────────────────────────────────────────────
# Operation specs
# ─────────────────────────────────────────────────────────────────────────────


#: ``POST /search``. Order is the order surfaces advertise.
SEARCH = OperationSpec(
    name="search",
    endpoint="/search",
    params=(
        Param("query", type="string", required=True),
        Param("category", type="string", enum=CATEGORIES, omit_if_empty=True),
        Param("limit", type="integer", cast=int, server_default="search.default_limit"),
        Param("date_after", type="string", omit_if_empty=True),
        Param("date_before", type="string", omit_if_empty=True),
        Param("include_daily", type="boolean", flag=True),
        Param("include_telemetry", type="boolean", flag=True),
        Param("include_other_projects", type="boolean", flag=True),
        Param("since_days", type="integer", cast=int),
        Param("types", type="array", enum=MEMORY_TYPES, omit_if_empty=True),
        Param("where", type="array", omit_if_empty=True),
        Param("min_priority", type="integer", cast=int),
        Param(
            "threshold", type="number", cast=float,
            server_default="search.mcp_threshold for MCP callers, else search.api_threshold",
        ),
        Param("tier", type="string", enum=TIERS, omit_if_empty=True),
        Param("resolve", type="string", enum=RESOLVE_MODES, unset_values=("none", "")),
        Param("include_retired", type="boolean", flag=True),
    ),
    display_overrides=(
        DisplayOverride(
            surface="cli", param="limit", value=3,
            reason=(
                "A person reading a terminal. The server's 15 was measured for "
                "agent consumers (the k-sweep behind search.default_limit); JSON "
                "and piped output send nothing and get the server default."
            ),
        ),
    ),
)

#: ``GET /list``. Query params. Not spec params: ``project`` (the scope itself,
#: which MCP takes from its session and the CLI from ``--project``) and
#: ``include_retired_core`` (the browse view's opt-in, sent beside ``core_only``).
LIST = OperationSpec(
    name="list",
    endpoint="/list",
    params=(
        Param("category", type="string", enum=CATEGORIES, omit_if_empty=True),
        Param("core_only", type="boolean", flag=True),
        Param("where", type="array", omit_if_empty=True),
        # Lifts the inbox/ skip; ActionItems are saved there.
        Param("include_inbox", type="boolean", flag=True),
        # Project isolation opt-out, as on search: a project-scoped list leaves
        # out records tagged to another project; true lists them, labelled.
        Param("include_other_projects", type="boolean", flag=True),
    ),
)

#: ``GET /read``. Query params. Not a spec param: ``project``, as on list.
READ = OperationSpec(
    name="read",
    endpoint="/read",
    params=(
        Param("file_path", type="string", required=True),
        Param("meta", type="boolean", flag=True),
        Param("tier", type="string", enum=TIERS, omit_if_empty=True),
    ),
)

#: ``POST /save``. The inclusion rule is the one every write surface shares: a
#: value is sent when it is not None, so an explicitly-empty ``contradicts: []``
#: survives as the assertion the caller made; only the free-text strings where
#: ``""`` means nothing (``slug``, ``project``, ``title``, ``source``) elide when
#: blank. An omitted ``source`` lets the ``X-Palinode-Source`` header carry
#: attribution (ADR-010).
SAVE = OperationSpec(
    name="save",
    endpoint="/save",
    params=(
        Param("content", type="string", required=True),
        Param("type", type="string", required=True, enum=MEMORY_TYPES),
        Param("slug", type="string", omit_if_empty=True),
        # The file to write, relative to the memory dir, in place of
        # <category>/<slug>.md; refused together with slug.
        Param("path", type="string"),
        Param("core", type="boolean"),
        Param("entities", type="array"),
        # Sugar on every surface: "foo" becomes entity "project/foo".
        Param("project", type="string", omit_if_empty=True),
        Param("title", type="string", omit_if_empty=True),
        Param("metadata", type="object"),
        Param("confidence", type="number", cast=float),
        Param("priority", type="integer", cast=int),
        # ADR-018: the kind of claim; omitted leaves the memory unmarked.
        Param("epistemic", type="string", enum=VALID_EPISTEMICS),
        Param("external_refs", type="object"),
        Param("source", type="string", omit_if_empty=True),
        # ADR-015 §2.1: how a save to an existing (category, slug) is written.
        Param("update_policy", type="string", enum=VALID_UPDATE_POLICIES),
        # Source-citation anchors; quote_hash is computed, or verified, on save.
        Param("sources", type="array", fields=(
            Param("ref", type="string", required=True),
            Param("quote", type="string", required=True),
            Param("quote_hash", type="string"),
        )),
        # Typed links: a conflict with no winner picked, and an evidence edge.
        Param("contradicts", type="array"),
        Param("backed_by", type="array"),
        # Claim-level anchors: a claim bound to the source span justifying it.
        Param("claims", type="array", fields=(
            Param("text", type="string", required=True),
            Param("source_id", type="string", required=True),
            Param("span", type="object", required=True, fields=(
                Param("quote", type="string", required=True),
                Param("quote_hash", type="string"),
            )),
            Param("claim_id", type="string"),
            Param("anchor_id", type="string"),
        )),
    ),
    shorthands=(
        Shorthand(
            param="type", by="ps", surfaces=("mcp", "cli"),
            reason="`ps` is shorthand for type=ProjectSnapshot; the surface sends the type.",
        ),
        Shorthand(
            param="content", by="--file", surfaces=("cli",),
            reason="`palinode save --file PATH` reads the content from a file.",
        ),
    ),
)

_PROVENANCE = ("cli", "api")
_PROVENANCE_REASON = (
    "Provenance a harness supplies about its own session: the session-end hook "
    "and `palinode session-end` fill it; an agent calling the MCP tool would "
    "have to invent it."
)


#: ``POST /session-end``. The save spec's inclusion rule: ``decisions: []`` is
#: sent, because the server's envelope guard reads their absence as the mark of
#: an absorbed tool call. ``push`` is three-way (absent: the server's
#: ``auto_push``; false: never). ``dry_run`` is a flag.
SESSION_END = OperationSpec(
    name="session_end",
    endpoint="/session-end",
    params=(
        Param("summary", type="string", required=True),
        Param("decisions", type="array"),
        Param("blockers", type="array"),
        Param("project", type="string", omit_if_empty=True),
        Param("source", type="string", omit_if_empty=True),
        Param("push", type="boolean", cast=as_bool),
        Param("dry_run", type="boolean", flag=True),
        Param("harness", type="string", omit_if_empty=True,
              surfaces=_PROVENANCE, surfaces_reason=_PROVENANCE_REASON),
        Param("cwd", type="string", omit_if_empty=True,
              surfaces=_PROVENANCE, surfaces_reason=_PROVENANCE_REASON),
        Param("model", type="string", omit_if_empty=True,
              surfaces=_PROVENANCE, surfaces_reason=_PROVENANCE_REASON),
        Param("trigger", type="string", omit_if_empty=True,
              surfaces=_PROVENANCE, surfaces_reason=_PROVENANCE_REASON),
        Param("session_id", type="string", omit_if_empty=True,
              surfaces=_PROVENANCE, surfaces_reason=_PROVENANCE_REASON),
        Param("duration_seconds", type="integer", cast=int,
              surfaces=_PROVENANCE, surfaces_reason=_PROVENANCE_REASON),
    ),
)

#: Every migrated operation. Anything not listed is still in ``parity.py``'s
#: ``CanonicalParam`` form.
SPECS: tuple[OperationSpec, ...] = (SEARCH, LIST, READ, SAVE, SESSION_END)



# ─────────────────────────────────────────────────────────────────────────────
# docs/PARITY.md tables (ADR-022 §9)
# ─────────────────────────────────────────────────────────────────────────────


def _forwarding(p: Param) -> str:
    rules = []
    if p.flag:
        rules.append("sent only when true")
    if p.omit_if_empty:
        rules.append("not sent when empty")
    if p.unset_values:
        shown = ", ".join(f"`{v}`" for v in p.unset_values if v != "")
        rules.append(f"not sent when {shown}")
    if p.surfaces is not None:
        rules.append(f"{', '.join(p.surfaces)} only")
    return "; ".join(rules) or "sent when set"


def render_param_table(spec: OperationSpec, out_of_box: Callable[[str], Any]) -> str:
    """The generated ``docs/PARITY.md`` table for *spec*.

    *out_of_box* maps a config key (``search.default_limit``) to its built-in
    value. It is passed in because this module cannot import the config.
    """
    import re

    def default(p: Param) -> str:
        if not p.server_default:
            return ""
        return re.sub(
            r"\b(search\.[a-z_]+)\b",
            lambda m: f"`{m.group(1)}` ({out_of_box(m.group(1))})",
            p.server_default,
        )

    rows = [
        f"### `{spec.name}` — `{spec.endpoint}`",
        "",
        "| Param | Type | Enum | Required | Server default (out of the box) | Forwarding |",
        "|---|---|---|---|---|---|",
    ]
    for p in spec.params:
        enum = ", ".join(f"`{v}`" for v in p.enum) if p.enum else ""
        item = "object" if p.fields else "enum" if p.enum else "string"
        kind = f"array of {item}" if p.type == "array" else p.type
        rows.append(
            f"| `{p.name}` | {kind} | {enum} | {'yes' if p.required else ''} "
            f"| {default(p)} | {_forwarding(p)} |"
        )
    if spec.display_overrides:
        rows += ["", "Display overrides (shown to a person; programs get the server default):", ""]
        for o in spec.display_overrides:
            rows.append(f"- **{o.surface}** `{o.param}` = {o.value}: {o.reason}")
    if spec.shorthands:
        rows += ["", "Shorthands (a surface fills a required param from its own sugar):", ""]
        for sh in spec.shorthands:
            rows.append(f"- **{', '.join(sh.surfaces)}** `{sh.by}` → `{sh.param}`: {sh.reason}")
    limited = {p.surfaces_reason: [] for p in spec.params if p.surfaces is not None}
    for p in spec.params:
        if p.surfaces is not None:
            limited[p.surfaces_reason].append(p)
    if limited:
        rows += ["", "Surface limits (a param only some surfaces advertise):", ""]
        for reason, params in limited.items():
            names = ", ".join(f"`{p.name}`" for p in params)
            rows.append(f"- {names} on {', '.join(params[0].surfaces)} only: {reason}")
    return "\n".join(rows) + "\n"
