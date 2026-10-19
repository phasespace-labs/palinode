"""One normalise seam below every write surface.

Before this module, "a surface received input" and "the capability runs" were
separated by nothing, so input handling was per-param, per-surface and
per-author. Two symptoms:

* ``_coerce_str_array`` — the JSON-string-array tolerance MCP clients need —
  existed exactly once, in ``mcp.py``, applied at ten sites all inside
  ``mcp.py``. CLI, API and plugin had no equivalent.
* The four surfaces disagreed about what an *empty* value means. ``cli/_api.py``
  elided an empty ``contradicts`` (``if contradicts:``) while ``mcp.py``
  forwarded it (``is not None``), so ``contradicts: []`` meant "no conflicts,
  asserted" over MCP and "never specified" over CLI.

That second bug had already been found and fixed once, on one param, on one
surface — ``cli/_api.py``'s ``session_end`` carries the post-mortem for
``decisions``/``blockers``: *"An empty list means 'considered, none to report';
eliding it makes the server read a parameter the caller did send as one that
never arrived."* The lesson was never propagated to ``save``. Re-deriving the
same fix per-param, per-surface is what a missing seam looks like from outside.

So the rule lives here, once, declaratively:

**Include a parameter when it is not None.** An empty list or dict is a
deliberate assertion and is forwarded. The sole exception is a parameter marked
``omit_if_empty`` — the free-text strings where ``""`` carries no meaning the
server could act on (``title``, ``slug``, ``source``, ``project``); those elide
when blank, which is what every surface already did for them.

Surfaces call :func:`build_payload` with whatever they received and get back the
canonical parameter set. The plugin (``plugin/index.ts``) is deliberately not a
consumer: it is already a bare ``JSON.stringify(params)`` pass-through, which is
the correct shape — it was the *thinnest* adapter and the most correct one.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Param",
    "SAVE_PARAMS",
    "SESSION_END_PARAMS",
    "build_payload",
    "coerce_str_array",
]


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


@dataclass(frozen=True)
class Param:
    """One write parameter and how a surface should hand it over.

    Attributes:
        name: Canonical parameter name — what the capability and the API model
            call it, regardless of what a surface named its own argument.
        array: Run :func:`coerce_str_array` over the value, so a double-encoded
            JSON array string arrives as a list on every surface.
        omit_if_empty: Elide a falsy-but-not-None value as well as ``None``.
            Reserved for free-text strings where ``""`` is indistinguishable
            from absent. Never set on an array — that is the bug this module
            exists to close.
        cast: Applied to a non-None value before inclusion, so a surface that
            receives ``"0.8"`` from a JSON-typed transport sends ``0.8``.
    """

    name: str
    array: bool = False
    omit_if_empty: bool = False
    cast: Callable[[Any], Any] | None = None


#: ``POST /save`` — every parameter a surface may forward.
#:
#: ``content`` and ``type`` are deliberately absent: both are required and each
#: surface resolves them differently (MCP derives ``type`` from the ``ps``
#: shorthand, the CLI takes it as a flag), so they are set explicitly by the
#: caller rather than pulled through this spec.
SAVE_PARAMS: tuple[Param, ...] = (
    Param("source", omit_if_empty=True),
    Param("slug", omit_if_empty=True),
    Param("path"),
    Param("project", omit_if_empty=True),
    Param("title", omit_if_empty=True),
    Param("core"),
    Param("metadata"),
    Param("confidence", cast=float),
    Param("priority", cast=int),
    Param("epistemic"),
    Param("external_refs"),
    Param("update_policy"),
    Param("entities", array=True),
    Param("sources", array=True),
    Param("contradicts", array=True),
    Param("backed_by", array=True),
    Param("claims", array=True),
)


#: ``POST /session-end`` — same rule set. ``summary`` is required and set by the
#: caller, like ``content`` above.
SESSION_END_PARAMS: tuple[Param, ...] = (
    Param("project", omit_if_empty=True),
    Param("source", omit_if_empty=True),
    Param("harness", omit_if_empty=True),
    Param("cwd", omit_if_empty=True),
    Param("model", omit_if_empty=True),
    Param("trigger", omit_if_empty=True),
    Param("session_id", omit_if_empty=True),
    Param("duration_seconds", cast=int),
    Param("push", cast=bool),
    Param("dry_run", cast=bool),
    Param("decisions", array=True),
    Param("blockers", array=True),
)


def build_payload(
    params: Iterable[Param], values: Mapping[str, Any]
) -> dict[str, Any]:
    """Assemble a canonical write payload from one surface's raw values.

    The single inclusion rule, applied identically on every surface: a value is
    included when it ``is not None``, so an explicitly-empty list or dict
    survives as the assertion the caller made. ``omit_if_empty`` params drop a
    blank value too.

    Unknown keys in *values* are ignored, so a surface can pass its whole
    argument mapping without filtering first.

    Args:
        params: The spec for this operation — :data:`SAVE_PARAMS` or
            :data:`SESSION_END_PARAMS`.
        values: Raw values as the surface received them.

    Returns:
        The payload dict, containing only the parameters that should be sent.
    """
    payload: dict[str, Any] = {}
    for param in params:
        value = values.get(param.name)
        if value is None:
            continue
        if param.array:
            value = coerce_str_array(value)
        if param.omit_if_empty and not value:
            continue
        if param.cast is not None:
            value = param.cast(value)
        payload[param.name] = value
    return payload
