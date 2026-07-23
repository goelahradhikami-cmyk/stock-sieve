"""
Backfill financial snapshots for the full A-share universe.

Commit 6-L.6 data infrastructure: persists fundamentals (mootdx) + PE/PB/mcap
(Tencent) for all ~5328 active stocks so that FactorEngine and the sandbox
backtest never need to hit the network per-stock.

Usage:
    python scripts/backfill_financials.py [--limit N] [--batch-size 60]

Resumable: skips codes already cached (finance_snapshots with pe_ttm NOT NULL).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

from src.data.financials import FinancialDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


def get_universe(db_path: str = "data/cache.db") -> list[str]:
    """Get all active non-ST A-share codes from security_master."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT code FROM security_master WHERE status='active' AND is_st=0 ORDER BY code"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_cached_codes(db_path: str = "data/cache.db") -> set[str]:
    """Codes that already have PE/PB/mcap cached (resumable)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT DISTINCT code FROM finance_snapshots WHERE pe_ttm IS NOT NULL"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def main(limit: int = None, batch_pause: float = 0.15):
    universe = get_universe()
    cached = get_cached_codes()
    todo = [c for c in universe if c not in cached]

    print("=== Financial Backfill ===")
    print(f"Universe: {len(universe)} stocks")
    print(f"Already cached: {len(cached)}")
    print(f"To fetch: {len(todo)}")

    if limit:
        todo = todo[:limit]
        print(f"Limited to: {limit}")

    if not todo:
        print("All stocks cached. Nothing to do.")
        return

    fp = FinancialDataProvider()
    success = 0
    failed = 0

    for i, code in enumerate(todo):
        try:
            # get_financial_dict fetches mootdx + Tencent and caches both
            data = fp.get_financial_dict(code)
            if data and data.get("pe_ttm") is not None:
                success += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            if i < 10 or i % 100 == 0:
                logger.warning("backfill: %s failed: %s", code, e)

        if (i + 1) % 50 == 0:
            print(f"  [{i + 1}/{len(todo)}] success={success} failed={failed}")
        if batch_pause and (i + 1) % 60 == 0:
            time.sleep(batch_pause)  # rate-limit: pause every 60 stocks

    print("\n=== Done ===")
    print(f"Success: {success}")
    print(f"Failed:  {failed}")
    print(f"Total cached now: {len(cached) + success}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max stocks to fetch")
    parser.add_argument("--batch-pause", type=float, default=0.15, help="Pause every 60 stocks (s)")
    args = parser.parse_args()
    main(limit=args.limit, batch_pause=args.batch_pause)
