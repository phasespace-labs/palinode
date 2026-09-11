"""
Palinode Consolidation Cron Entry Point

Three-tier memory freshness:
  Tier 1: Session append (hook/MCP, every session, free — captures intent + result)
  Tier 2: Nightly dedup (--nightly, UPDATE/SUPERSEDE only, 1-day lookback)
  Tier 3: Weekly deep clean (full ops, 3-7 day lookback)

Crontab examples (times in UTC, target 4am PT = 11:00 UTC during PDT):
    # Nightly — lightweight dedup of today's sessions
    0 11 * * * cd /path/to/palinode && PALINODE_DIR=~/.palinode venv/bin/python -m palinode.consolidation.cron --nightly --days 1

    # Weekly — full compaction with MERGE/ARCHIVE
    0 11 * * 0 cd /path/to/palinode && PALINODE_DIR=~/.palinode venv/bin/python -m palinode.consolidation.cron --days 3

The schedule above is now an upper bound, not the trigger: this entry point
consults the activity gate (``consolidation.auto_gate``) first and exits 0
quietly when a pass is not yet due, so the cron can fire as often as hourly and
the pass lands on use rather than on the calendar. ``--ignore-gate`` forces the
pass, which is what a hand-run recovery wants. See ``docs/OPERATIONS.md``.
"""
from __future__ import annotations

import logging
import sys

from palinode.core.config import config
from palinode.consolidation import activity_gate
from palinode.consolidation.runner import run_consolidation, run_nightly
from palinode.consolidation.run_lock import ConsolidationAlreadyRunning

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("palinode.consolidation.cron")


def main() -> None:
    if not config.consolidation.enabled:
        logger.info("Consolidation is disabled in config. Exiting.")
        sys.exit(0)

    nightly = "--nightly" in sys.argv

    # Parse --days N for custom lookback (default: config value)
    lookback = None
    if "--days" in sys.argv:
        try:
            idx = sys.argv.index("--days")
            lookback = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            pass

    mode = "nightly" if nightly else "weekly"

    if "--ignore-gate" not in sys.argv:
        decision = activity_gate.evaluate(mode)
        if not decision.should_run:
            # One line, with both numerators and both denominators: an operator
            # asking "why didn't it run last night" gets the answer from the
            # cron log alone, without reconstructing the state file by hand.
            logger.info("Skipping %s consolidation — %s", mode, decision.reason)
            sys.exit(0)

    logger.info(f"Starting {mode} consolidation (lookback: {lookback or 'config default'} days)...")

    try:
        if nightly:
            result = run_nightly(lookback_days=lookback)
        else:
            result = run_consolidation(lookback_days=lookback)
    except ConsolidationAlreadyRunning as error:
        logger.error("%s", error)
        raise SystemExit(1) from None

    logger.info(f"Consolidation complete: {result}")


if __name__ == "__main__":
    main()
