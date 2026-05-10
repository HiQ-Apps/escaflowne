"""
research/pull_mgc.py — Escaflowne (Celeri-on-MGC)

Pull 1-min OHLCV bars for all active MGC contracts over the backtest window
from Databento, save as CSV.zst matching the format Celeri's data_loader expects.

Critical: we need to use a parent symbol query so we get ALL active MGC contracts,
not just the front month. The data_loader's continuous-contract construction
needs every contract that traded during the window so it can pick the
highest-volume one each day.

Usage:
    python research/pull_mgc.py
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import databento as db
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# CME futures dataset on Databento. Gold (MGC) lives on COMEX which is
# distributed via the Globex MDP3 feed under this dataset code.
DATASET = "GLBX.MDP3"
SCHEMA  = "ohlcv-1m"

# Parent symbol — matches all MGC contracts (MGCG24, MGCJ24, MGCM24, ...)
PARENT_SYMBOL = "MGC.FUT"

# 7-year window to match Celeri's depth and capture multiple regimes
START = "2019-01-01"
END   = "2026-05-09"

OUTPUT_DIR = PROJECT_ROOT / "data" / "raw"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / f"mgc_1min_{START}_{END}.csv.zst"


def main():
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        sys.exit("DATABENTO_API_KEY not set. Add it to .env at project root.")

    if OUTPUT_FILE.exists():
        log.info("Output file already exists: %s", OUTPUT_FILE)
        log.info("Delete it manually if you want to re-pull.")
        return

    log.info("Initializing Databento client...")
    client = db.Historical(api_key)

    # Cost estimate first — this protects against accidental large pulls
    log.info("Estimating cost...")
    cost = client.metadata.get_cost(
        dataset=DATASET,
        symbols=[PARENT_SYMBOL],
        stype_in="parent",
        schema=SCHEMA,
        start=START,
        end=END,
    )
    log.info(f"Estimated cost: ${cost:.2f}")

    if cost > 10:
        resp = input(f"Cost is ${cost:.2f}. Proceed? [y/N]: ").strip().lower()
        if resp != "y":
            log.info("Aborted by user.")
            return

    log.info("Fetching MGC 1-min bars from %s to %s...", START, END)
    log.info("(This is a parent-symbol query — pulls ALL active MGC contracts)")

    data = client.timeseries.get_range(
        dataset=DATASET,
        symbols=[PARENT_SYMBOL],
        stype_in="parent",
        schema=SCHEMA,
        start=START,
        end=END,
    )

    log.info("Writing to %s ...", OUTPUT_FILE)
    data.to_csv(OUTPUT_FILE, compression="zstd")

    log.info("Done. File size: %.1f MB", OUTPUT_FILE.stat().st_size / 1024 / 1024)
    log.info("")
    log.info("Next step:")
    log.info(f"  python -m backtest.data_loader {OUTPUT_FILE}")


if __name__ == "__main__":
    main()