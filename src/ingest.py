"""
ingest.py — Top-level ingestion runner.

Run this file to execute all Stage 1 ingestion pipelines in sequence:
  python src/ingest.py

Flags:
  --source worldbank    Run only World Bank
  --source sec          Run only SEC EDGAR
  --dry-run             Initialize DB schema only, skip API calls
"""

import argparse
import logging
import sys
from db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_worldbank():
    from ingest_worldbank import ingest
    log.info("=" * 50)
    log.info("STAGE 1a — World Bank API")
    log.info("=" * 50)
    return ingest()


def run_sec():
    from ingest_sec import ingest
    log.info("=" * 50)
    log.info("STAGE 1b — SEC EDGAR")
    log.info("=" * 50)
    return ingest()


def main():
    parser = argparse.ArgumentParser(description="ESG Stage 1 — Data Ingestion")
    parser.add_argument(
        "--source",
        choices=["worldbank", "sec", "all"],
        default="all",
        help="Which source to ingest (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Initialize DB only, skip API calls",
    )
    args = parser.parse_args()

    log.info("Initializing database schema...")
    init_db()

    if args.dry_run:
        log.info("Dry run — schema initialized, no API calls made.")
        sys.exit(0)

    totals = {}
    if args.source in ("worldbank", "all"):
        totals["worldbank"] = run_worldbank()
    if args.source in ("sec", "all"):
        totals["sec"] = run_sec()

    log.info("")
    log.info("=" * 50)
    log.info("INGESTION SUMMARY")
    log.info("=" * 50)
    for source, count in totals.items():
        log.info(f"  {source:20s}: {count:,} rows")
    log.info(f"  {'TOTAL':20s}: {sum(totals.values()):,} rows")


if __name__ == "__main__":
    main()