"""
Checks: api_reachable, api_status_consistent

Probe the palinode-api over HTTP.  These are network checks; they degrade
gracefully if the API is unreachable (severity drops to warn in that case so
a call site can suppress with --no-network if desired).

Both checks use a short timeout (2 s) so they don't stall `palinode doctor`
in CI or restricted environments.

api_status_consistent catches the drift failure mode where /status reports
chunks=0 while the on-disk DB has data (or vice-versa).  It also covers the
broader "API and configured DB have diverged" scenario.

Tolerance: we allow api_chunks > 0 even if md_count == 0 because
consolidation can compress many files into a smaller number of chunks.
The reverse (md_count > 0 but api_chunks == 0) is always suspicious.
"""
from __future__ import annotations

import glob
import sqlite3
from pathlib import Path

import httpx

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


def _api_base(ctx: DoctorContext) -> str:
    host = ctx.config.services.api.host
    port = ctx.config.services.api.port
    return f"http://{host}:{port}"


@register(tags=("deep",))
def api_reachable(ctx: DoctorContext) -> CheckResult:
    """Verify the palinode-api process answers at /health within 2 s."""
    base = _api_base(ctx)
    url = f"{base}/health"

    try:
        resp = httpx.get(url, timeout=2.0)
    except Exception as exc:
        return CheckResult(
            name="api_reachable",
            severity="error",
            passed=False,
            message=f"API not reachable at {url}: {exc}",
            remediation=(
                f"Start the API with 'palinode-api' or check its service status:\n"
                f"  systemctl --user status palinode-api"
            ),
        )

    if resp.status_code != 200:
        return CheckResult(
            name="api_reachable",
            severity="error",
            passed=False,
            message=f"API at {url} returned HTTP {resp.status_code}",
            remediation=(
                "API returned a non-200 status. "
                "Check service logs: 'journalctl --user -u palinode-api -n 50'"
            ),
        )

    return CheckResult(
        name="api_reachable",
        severity="error",
        passed=True,
        message=f"API is reachable at {url} (HTTP 200)",
        remediation=None,
    )


@register(tags=("deep",))
def api_status_consistent(ctx: DoctorContext) -> CheckResult:
    """Cross-check /status chunk count against configured on-disk DB.

    Catches the drift failure mode where /status reported zeros while the DB
    had data, and the broader scenario where the API is serving from a
    different DB than the one in config.

    Tolerance: we permit api_chunks > 0 when md_count is small (consolidation
    may compress many files into fewer chunks stored in the DB).  The alarming
    direction is api_chunks == 0 while disk has chunks — that is always wrong.
    """
    base = _api_base(ctx)
    url = f"{base}/status"

    # Count *.md files under memory_dir
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()
    md_files = glob.glob(str(memory_dir / "**" / "*.md"), recursive=True)
    md_count = len(md_files)

    # Count chunks in the on-disk DB directly
    db_path = Path(ctx.config.db_path).expanduser().resolve()
    disk_chunks: int | None = None
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT count(*) FROM chunks").fetchone()
                disk_chunks = row[0] if row else 0
            finally:
                conn.close()
        except Exception:
            disk_chunks = None

    # Fetch /status from the API
    try:
        resp = httpx.get(url, timeout=2.0)
    except Exception as exc:
        return CheckResult(
            name="api_status_consistent",
            severity="warn",
            passed=False,
            message=f"Cannot reach {url} to verify consistency: {exc}",
            remediation=(
                "Start the API with 'palinode-api' and then re-run 'palinode doctor'."
            ),
        )

    if resp.status_code != 200:
        return CheckResult(
            name="api_status_consistent",
            severity="warn",
            passed=False,
            message=f"/status returned HTTP {resp.status_code}; cannot verify consistency",
            remediation=(
                "Check service logs: 'journalctl --user -u palinode-api -n 50'"
            ),
        )

    body = resp.json()
    api_chunks: int = body.get("total_chunks", 0)

    # Case 1: /status says 0 but disk has data.
    if api_chunks == 0 and md_count > 0:
        return CheckResult(
            name="api_status_consistent",
            severity="error",
            passed=False,
            message=(
                f"API reports 0 chunks but memory_dir has {md_count} markdown file(s) "
                f"(on-disk DB chunks: {disk_chunks}). "
                f"The API may be connected to the wrong DB or its stats are stale."
            ),
            remediation=(
                "Restart palinode-api: 'systemctl --user restart palinode-api'. "
                "If the problem persists, run 'palinode doctor --check phantom_db_files' "
                "to check for stale/orphan DB files. Review the related diagnostics above."
            ),
        )

    # Case 2: disk_chunks readable; compare with api_chunks
    if disk_chunks is not None and api_chunks != disk_chunks:
        # Allow api > disk if md_count is small (consolidation compressed files)
        if disk_chunks == 0 and api_chunks > 0:
            # Suspicious but non-critical — maybe the API is on a different DB
            return CheckResult(
                name="api_status_consistent",
                severity="warn",
                passed=False,
                message=(
                    f"API reports {api_chunks} chunk(s) but configured DB at {db_path} "
                    f"has 0 chunks. The API may be serving from a different database."
                ),
                remediation=(
                    f"Verify PALINODE_DIR and db_path are consistent across all services. "
                    f"Run 'palinode doctor --check phantom_db_files' to locate other DB files."
                ),
            )
        if api_chunks == 0 and disk_chunks > 0:
            return CheckResult(
                name="api_status_consistent",
                severity="error",
                passed=False,
                message=(
                    f"API reports 0 chunks but on-disk DB at {db_path} has "
                    f"{disk_chunks} chunk(s). "
                    f"The API is either stale or connected to a different DB."
                ),
                remediation=(
                    "Restart palinode-api: 'systemctl --user restart palinode-api'. "
                    "Review the related diagnostics above."
                ),
            )

    return CheckResult(
        name="api_status_consistent",
        severity="error",
        passed=True,
        message=(
            f"API /status reports {api_chunks} chunk(s), "
            f"memory_dir has {md_count} markdown file(s) — consistent."
        ),
        remediation=None,
    )
