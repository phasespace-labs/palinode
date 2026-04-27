"""
Runner: executes registered checks against a DoctorContext.
"""
from __future__ import annotations

import palinode.diagnostics.checks  # noqa: F401 — trigger registration side-effects
import palinode.diagnostics.fixes  # noqa: F401 — populate the fix registry

from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import CheckResult, DoctorContext


def run_all(ctx: DoctorContext, *, tag: str | None = None) -> list[CheckResult]:
    """Run registered checks and return results in registration order.

    Parameters
    ----------
    ctx:
        Shared doctor context (config, etc.).
    tag:
        When provided, only run checks whose tags tuple contains *tag*.
        Pass ``"fast"`` to skip network probes and canary writes.
        Pass ``None`` (default) to run all registered checks.
    """
    results = []
    for check_fn, tags in all_checks():
        if tag is not None and tag not in tags:
            continue
        results.append(check_fn(ctx))
    return results


def run_one(ctx: DoctorContext, name: str) -> CheckResult:
    """Run the single check whose function name matches *name*.

    Raises ValueError if no check with that name is registered.
    """
    for check_fn, _tags in all_checks():
        if check_fn.__name__ == name:
            return check_fn(ctx)
    raise ValueError(
        f"No check named {name!r} is registered. "
        f"Available: {[fn.__name__ for fn, _ in all_checks()]}"
    )
