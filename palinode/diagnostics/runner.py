"""
Runner: executes registered checks against a DoctorContext.

Each check runs under a bounded per-check time budget so a single hung probe
(classically a cold or unreachable Ollama/embed host) can never turn
`palinode doctor` into a silent multi-minute hang. A check that exceeds its
budget is reported as a `warn` timeout and the runner moves on; a check that
raises is reported as an `error` — either way the run always completes and every
other check still gets to speak. An optional `on_result` callback lets callers
stream results as they land instead of waiting for the whole batch.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

import palinode.diagnostics.checks  # noqa: F401 — trigger registration side-effects
import palinode.diagnostics.fixes  # noqa: F401 — populate the fix registry

from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import CheckResult, DoctorContext

logger = logging.getLogger(__name__)

# Per-check wall-clock budget. Generous enough for a legitimately slow-but-warm
# check, short enough that a cold-Ollama hang surfaces in seconds, not the ~2
# minutes the pre-timeout runner could block for.
DEFAULT_CHECK_TIMEOUT_S = 15.0


def _safe_call(
    check_fn: Callable[[DoctorContext], CheckResult],
    ctx: DoctorContext,
    abandoned: threading.Event | None = None,
) -> CheckResult:
    """Run one check, converting an exception into an ``error`` CheckResult.

    A check that raises must not abort the whole doctor run — every other check
    still needs to report.

    *abandoned*, when set, means the runner already gave up waiting for this
    check and reported a timeout. The worker thread outlives the caller (a
    blocking syscall cannot be cancelled), so an abandoned check must not log:
    nobody is listening, and by the time it finishes the process may be tearing
    down its stderr.
    """
    name = getattr(check_fn, "__name__", "unknown_check")
    try:
        return check_fn(ctx)
    except Exception as exc:  # noqa: BLE001 — a bad check must not kill the runner
        if abandoned is None or not abandoned.is_set():
            logger.warning("doctor check %s raised: %r", name, exc, exc_info=True)
        return CheckResult(
            name=name,
            severity="error",
            passed=False,
            message=f"Check '{name}' errored: {exc}",
            remediation="This is a bug in the check itself — the error is logged with a traceback.",
        )


def _run_with_timeout(
    check_fn: Callable[[DoctorContext], CheckResult],
    ctx: DoctorContext,
    timeout_s: float | None,
) -> CheckResult:
    """Run one check under *timeout_s*, returning a warn result if it overruns.

    The check runs in a daemon thread. A blocking network syscall can't be
    cancelled, so on timeout we stop *waiting* (and let the thread die on its own
    once its I/O returns or the process exits) rather than joining forever — that
    is exactly the silent-hang the budget exists to prevent.

    An overrun check is explicitly marked *abandoned* so that when it eventually
    finishes it neither publishes a result nobody asked for nor logs into a
    stream that may already be gone.
    """
    name = getattr(check_fn, "__name__", "unknown_check")
    if not timeout_s or timeout_s <= 0:
        return _safe_call(check_fn, ctx)

    box: dict[str, CheckResult] = {}
    abandoned = threading.Event()

    def _worker() -> None:
        result = _safe_call(check_fn, ctx, abandoned=abandoned)
        if not abandoned.is_set():
            box["result"] = result

    thread = threading.Thread(target=_worker, name=f"doctor-{name}", daemon=True)
    thread.start()
    thread.join(timeout_s)

    if thread.is_alive():
        abandoned.set()
        return CheckResult(
            name=name,
            severity="warn",
            passed=False,
            message=f"Check '{name}' timed out after {timeout_s:.0f}s and was skipped.",
            remediation=(
                "A check exceeded its time budget — most often a cold or unreachable "
                "Ollama/embed host. Check the host (`curl <ollama-url>/api/version`) or "
                "run this check alone. `palinode doctor` never blocks on one hang."
            ),
        )
    return box.get(
        "result",
        CheckResult(
            name=name, severity="error", passed=False,
            message=f"Check '{name}' produced no result.", remediation=None,
        ),
    )


def run_all(
    ctx: DoctorContext,
    *,
    tag: str | None = None,
    timeout_s: float | None = DEFAULT_CHECK_TIMEOUT_S,
    on_result: Callable[[CheckResult], None] | None = None,
) -> list[CheckResult]:
    """Run registered checks and return results in registration order.

    Parameters
    ----------
    ctx:
        Shared doctor context (config, etc.).
    tag:
        When provided, only run checks whose tags tuple contains *tag*.
        Pass ``"fast"`` to skip network probes and canary writes.
        Pass ``None`` (default) to run all registered checks.
    timeout_s:
        Per-check wall-clock budget in seconds. A check that overruns is
        reported as a ``warn`` timeout and the run continues. Pass ``None`` or
        ``0`` to disable the budget (run checks inline, legacy behavior).
    on_result:
        Optional callback invoked with each CheckResult as it completes, so a
        caller (e.g. the CLI) can stream output instead of waiting for the batch.
    """
    results: list[CheckResult] = []
    for check_fn, tags in all_checks():
        if tag is not None and tag not in tags:
            continue
        result = _run_with_timeout(check_fn, ctx, timeout_s)
        results.append(result)
        if on_result is not None:
            on_result(result)
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
