"""
Mem0 Backfill — Full Pipeline

Exports from Qdrant → deduplicates → classifies → generates Palinode files.

Usage:
    python -m palinode.migration.run_mem0_backfill

This is a one-time migration script. Run it once, review the output,
then optionally disable Mem0's autoRecall in OpenClaw config.
"""
from __future__ import annotations

import logging
import json

from palinode.migration.mem0_export import export_all
from palinode.migration.mem0_classify import run_classification, deduplicate
from palinode.migration.mem0_generate import generate_files

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("palinode.migration")


def main():
    # Step 1: Export
    logger.info("=" * 60)
    logger.info("STEP 1: Exporting from Qdrant...")
    export_path = export_all()

    # Step 2: Classify
    logger.info("=" * 60)
    logger.info("STEP 2: Deduplicating and classifying...")
    classified_path = run_classification()

    # Step 3: Generate files
    logger.info("=" * 60)
    logger.info("STEP 3: Generating Palinode markdown files...")
    stats = generate_files()

    # Summary
    logger.info("=" * 60)
    logger.info("BACKFILL COMPLETE")
    logger.info(f"Files generated: {stats}")
    logger.info(f"Total files: {sum(stats.values())}")
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Review the generated files in $PALINODE_DIR/")
    logger.info("  2. Run 'curl -X POST http://localhost:6340/reindex' to index them")
    logger.info("  3. Test search: 'curl -X POST http://localhost:6340/search -d {\"query\":\"test\"}'")
    logger.info("  4. If satisfied, disable Mem0 autoRecall in OpenClaw config")


if __name__ == "__main__":
    main()
