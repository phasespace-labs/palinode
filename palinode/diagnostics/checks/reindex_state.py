"""
Check: reindex_in_progress

Queries the running palinode-api's /status endpoint for the reindex state
introduced in the API status surface.  If the API is unreachable, the check
degrades gracefully to "unknown".

The check is informational — knowing a reindex is running prevents false
alarms from other checks (e.g. chunks_match_md_count may show a low ratio
mid-reindex).  It becomes "warn" only if the API reports a reindex that
has been running for an unusually long time (>30 minutes), which suggests
it is stuck.

Severity: info (running) | info (idle) | warn (stuck / running >30 min)

 """
from __future__ import annotations

import httpx

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

# Threshold after which a "running" reindex is considered stuck.
_STUCK_MINUTES = 30


def _api_base(ctx: DoctorContext) -> str:
    host = ctx.config.services.api.host
    port = ctx.config.services.api.port
    return f"http://{host}:{port}"


@register(tags=("fast",))
def reindex_in_progress(ctx: DoctorContext) -> CheckResult:
    """Report whether a reindex is currently running by querying /status.

    Severity:
    - info: idle (normal case)
    - info: running — note for other checks that may see a partial index
    - warn: appears stuck (running for >30 minutes)
    - info: cannot reach API to check (degrades gracefully)
    """
    base = _api_base(ctx)
    url = f"{base}/status"

    try:
        resp = httpx.get(url, timeout=2.0)
    except Exception:
        # API is not running — check degrades to "unknown".
        return CheckResult(
            name="reindex_in_progress",
            severity="info",
            passed=True,
            message=(
                "Could not reach the API to check reindex state "
                f"({url} is not responding).  Start palinode-api if you need "
                "accurate reindex status."
            ),
            remediation=None,
        )

    if resp.status_code != 200:
        return CheckResult(
            name="reindex_in_progress",
            severity="info",
            passed=True,
            message=f"/status returned HTTP {resp.status_code}; reindex state unknown.",
            remediation=None,
        )

    body = resp.json()
    reindex = body.get("reindex", {})
    running: bool = reindex.get("running", False)
    started_at: str | None = reindex.get("started_at")
    files_processed: int = reindex.get("files_processed", 0)
    total_files: int = reindex.get("total_files", 0)

    if not running:
        return CheckResult(
            name="reindex_in_progress",
            severity="info",
            passed=True,
            message="No reindex in progress — idle.",
            remediation=None,
        )

    # Reindex is running — check whether it's stuck.
    elapsed_str = ""
    stuck = False
    if started_at:
        try:
            from datetime import datetime, timezone, timedelta
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            now = datetime.now(tz=timezone.utc)
            elapsed = now - started
            elapsed_min = elapsed.total_seconds() / 60
            elapsed_str = f" (started {started_at}, {elapsed_min:.0f} min ago)"
            stuck = elapsed_min > _STUCK_MINUTES
        except (ValueError, AttributeError):
            pass

    progress = ""
    if total_files > 0:
        progress = f"{files_processed}/{total_files} files"

    if stuck:
        return CheckResult(
            name="reindex_in_progress",
            severity="warn",
            passed=False,
            message=(
                f"Reindex appears stuck{elapsed_str}.  "
                f"Progress: {progress or 'unknown'}.  "
                f"Expected completion is within a few minutes for most stores."
            ),
            remediation=(
                "Check palinode-api logs for errors:\n"
                "  journalctl --user -u palinode-api -n 100\n"
                "If stuck, restart palinode-api: "
                "  systemctl --user restart palinode-api\n"
                "Note: restarting mid-reindex is safe — the index is rebuilt "
                "from disk on the next 'palinode reindex' call."
            ),
        )

    return CheckResult(
        name="reindex_in_progress",
        severity="info",
        passed=True,
        message=(
            f"Reindex in progress{elapsed_str}.  "
            f"Progress: {progress or 'unknown'}.  "
            "Other index checks may show partial counts until it completes."
        ),
        remediation=None,
    )
