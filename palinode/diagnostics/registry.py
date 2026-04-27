"""
Check registry for palinode doctor.

Keeps it minimal: a module-level list and a @register decorator.
No class hierarchy, no plugin discovery.  Check modules import-and-register
via side-effect when `checks/__init__.py` is imported.

Tags
----
Each check can carry a ``tags`` tuple that callers use to filter.  Two tags
are currently meaningful:

  "fast"  — cheap, no network I/O, no blocking filesystem walks; target
            sub-500ms for the whole fast subset.
  "deep"  — may make network calls (HTTP probes), read /proc, or walk the
            filesystem; acceptable to take 10-15 s in total.

Use ``@register(tags=("fast",))`` or ``@register(tags=("deep",))``.
Plain ``@register`` (no call) defaults to ``tags=()``, which the runner
treats as "deep" (conservative).
"""
from __future__ import annotations

from typing import Callable

from palinode.diagnostics.types import CheckResult, DoctorContext, FixResult

# Ordered list of (callable, tags) pairs.
_checks: list[tuple[Callable[[DoctorContext], CheckResult], tuple[str, ...]]] = []

# Fix registry — name-keyed map from check name to fix function.
# A fix function takes (ctx, result) and returns a FixResult.
# This is intentionally separate from the check registry: registering a fix
# never alters check behavior, and a fix for a check that does not exist on
# this codepath is a harmless no-op. Only the
# explicit set of safe non-data fixes registered in palinode.diagnostics.fixes
# may live here.
FixFn = Callable[[DoctorContext, CheckResult], FixResult]
_fixes: dict[str, FixFn] = {}


def register(
    fn: Callable[[DoctorContext], CheckResult] | None = None,
    *,
    tags: tuple[str, ...] = (),
) -> (
    Callable[[DoctorContext], CheckResult]
    | Callable[
        [Callable[[DoctorContext], CheckResult]],
        Callable[[DoctorContext], CheckResult],
    ]
):
    """Decorator: append *fn* to the global check registry.

    Can be used bare (``@register``) or with keyword args
    (``@register(tags=("fast",))``).  Returns the function unchanged in both
    forms so it remains directly callable.
    """
    def _wrap(
        f: Callable[[DoctorContext], CheckResult],
    ) -> Callable[[DoctorContext], CheckResult]:
        _checks.append((f, tags))
        return f

    if fn is not None:
        # Bare usage: @register  (fn is the decorated function)
        return _wrap(fn)

    # Called with kwargs: @register(tags=(...))  — return a decorator
    return _wrap


def all_checks() -> list[tuple[Callable[[DoctorContext], CheckResult], tuple[str, ...]]]:
    """Return a snapshot of the registered (check, tags) pairs in registration order."""
    return list(_checks)


def register_fix(check_name: str, fix_fn: FixFn) -> FixFn:
    """Register *fix_fn* as the fix action for the check named *check_name*.

    ``--fix`` policy: this function is only ever called from
    ``palinode.diagnostics.fixes`` for checks on the explicit safety whitelist.
    It is deliberately *not* a decorator on @register because not every check
    has a fix, and adding a new fix should require an explicit whitelist edit
    rather than landing as part of a check change.
    """
    _fixes[check_name] = fix_fn
    return fix_fn


def get_fix(check_name: str) -> FixFn | None:
    """Return the registered fix function for *check_name*, or None if absent."""
    return _fixes.get(check_name)


def all_fixes() -> dict[str, FixFn]:
    """Return a snapshot of the registered name->fix map.  Used by tests."""
    return dict(_fixes)
