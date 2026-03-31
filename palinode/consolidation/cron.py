"""
Palinode Consolidation Cron Entry Point

Run via system cron or OpenClaw cron job:
    python -m palinode.consolidation.cron

Or add to crontab:
    0 3 * * 0 cd /path/to/palinode && venv/bin/python -m palinode.consolidation.cron
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

    logger.info("Starting weekly consolidation...")
    result = run_consolidation()
    logger.info(f"Consolidation complete: {result}")


if __name__ == "__main__":
    main()
