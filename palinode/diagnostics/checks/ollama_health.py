"""Check: ollama_circuit_health (#338 Phase 5).

Reports the state of the centralized Ollama client's per-role circuit breakers
and rolling latency, so `palinode doctor` can say "Ollama degraded" or "circuit
open" rather than just a binary reachable/unreachable.

This is a ``fast`` check: it reads the in-process `OllamaClient.metrics()`
directly (no network I/O). When run via the `/doctor?fast=true` endpoint it
executes *inside the API process*, so it sees the live circuit/latency state of
the same client the save/search/embed paths use. Run from the standalone CLI it
sees a fresh process with no traffic and reports "no recent traffic" — accurate
for that process.
"""
from __future__ import annotations

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

# p95 (ms) above which a reachable host is flagged "degraded" — matches the
# #338 acceptance criterion ("Ollama degraded when p95 > 5s").
_DEGRADED_P95_MS = 5000.0


@register(tags=("fast",))
def ollama_circuit_health(ctx: DoctorContext) -> CheckResult:
    """Flag an open circuit (error) or high p95 latency (warn) per Ollama role."""
    from palinode.core.ollama_client import get_ollama_client

    metrics = get_ollama_client().metrics()

    open_roles = [r for r, m in metrics.items() if m.get("circuit_state") == "open"]
    if open_roles:
        return CheckResult(
            name="ollama_circuit_health",
            severity="error",
            passed=False,
            message=(
                f"Ollama circuit OPEN for role(s): {', '.join(sorted(open_roles))} — "
                f"calls are fast-failing; the host is unreachable or badly degraded."
            ),
            remediation=(
                "Check the Ollama host for that role (embed → the embed box, "
                "chat/consolidation → the chat box): `curl <url>/api/version`. "
                "The breaker half-opens after its cooldown and recovers "
                "automatically once the host answers again."
            ),
            tags=("fast",),
        )

    degraded = [
        (r, m) for r, m in metrics.items()
        if (m.get("p95_ms") or 0) > _DEGRADED_P95_MS
    ]
    if degraded:
        detail = ", ".join(f"{r} (p95={int(m['p95_ms'])}ms)" for r, m in sorted(degraded))
        return CheckResult(
            name="ollama_circuit_health",
            severity="warn",
            passed=False,
            message=(
                f"Ollama degraded — p95 latency over {int(_DEGRADED_P95_MS)}ms for "
                f"role(s): {detail}. Saves/searches may be slow."
            ),
            remediation=(
                "The host is reachable but slow (cold model, VRAM contention, or "
                "concurrent load). Check `ollama ps` on the host; a keepalive ping "
                "keeps the model resident. See docs/ollama-degraded.md."
            ),
            tags=("fast",),
        )

    active = {r: m for r, m in metrics.items() if m.get("count_5m", 0) > 0}
    if not active:
        return CheckResult(
            name="ollama_circuit_health",
            severity="info",
            passed=True,
            message="No recent Ollama traffic recorded in this process — nothing to assess.",
            remediation=None,
            tags=("fast",),
        )

    summary = ", ".join(
        f"{r}: p95={int(m['p95_ms']) if m.get('p95_ms') else 0}ms "
        f"err={m.get('error_rate_5m', 0.0)}"
        for r, m in sorted(active.items())
    )
    return CheckResult(
        name="ollama_circuit_health",
        severity="info",
        passed=True,
        message=f"Ollama healthy — {summary}.",
        remediation=None,
        tags=("fast",),
    )
