"""
Palinode Consolidation Cron Entry Point

Two-tier memory freshness:
  Tier 1: Session append (plugin, every session, free — captures intent + result)
  Tier 2: Deep clean twice/week (this script, full ops)

Crontab examples:
    # Nightly (recommended if you have a local LLM — free)
    0 3 * * * cd /path/to/palinode && PALINODE_DIR=~/.palinode venv/bin/python -m palinode.consolidation.cron --days 1

    # Twice-weekly (if using cloud LLM — saves API cost)
    0 3 * * 2,5 cd /path/to/palinode && PALINODE_DIR=~/.palinode venv/bin/python -m palinode.consolidation.cron --days 4
"""
from __future__ import annotations

import logging
import sys

from palinode.core.config import config
from palinode.consolidation.runner import run_consolidation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("palinode.consolidation.cron")


def main() -> None:
    if not config.consolidation.enabled:
        logger.info("Consolidation is disabled in config. Exiting.")
        sys.exit(0)

    # Parse --days N for custom lookback (default: config value)
    lookback = None
    if "--days" in sys.argv:
        try:
            idx = sys.argv.index("--days")
            lookback = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            pass

    logger.info(f"Starting consolidation (lookback: {lookback or 'config default'} days)...")
    result = run_consolidation(lookback_days=lookback)

    logger.info(f"Consolidation complete: {result}")


if __name__ == "__main__":
    main()
