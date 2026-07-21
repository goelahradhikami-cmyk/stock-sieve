"""
Backfill multi-period financials via baostock (Commit 6-L.6 data infrastructure).

Fills the growth family (revenue_growth_1y/3y, earnings_growth_1y/3y,
margin_trend) that mootdx could not provide (single-period snapshot only).

Baostock connects to its OWN server (baostock.com), no token, proxy-friendly.
~9s/stock after optimization, so 5328 stocks ≈ 13 hours. Resumable via
baostock_cache table.

Usage:
    python scripts/backfill_baostock.py [--limit N] [--resume]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from src.data.baostock_provider import BaostockProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


def get_universe(db_path: str = "data/cache.db") -> list[str]:
    """All active non-ST A-share codes, sorted."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT code FROM security_master WHERE status='active' AND is_st=0 "
        "ORDER BY code"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_cached_codes(db_path: str = "data/cache.db") -> set[str]:
    """Codes already in baostock_cache (resumable)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT DISTINCT code FROM baostock_cache").fetchall()
    conn.close()
    return {r[0] for r in rows}


def main(limit: int = None, log_every: int = 50):
    universe = get_universe()
    cached = get_cached_codes()
    todo = [c for c in universe if c not in cached]

    print(f"=== Baostock Financial Backfill ===")
    print(f"Universe: {len(universe)} stocks")
    print(f"Already cached: {len(cached)}")
    print(f"To fetch: {len(todo)}")
    print(f"Estimated time: ~{len(todo) * 9 / 3600:.1f} hours (at 9s/stock)")
    print()

    if limit:
        todo = todo[:limit]
        print(f"Limited to: {limit}")

    if not todo:
        print("All stocks cached. Nothing to do.")
        return

    bp = BaostockProvider()
    success = 0
    failed = 0
    t_start = time.time()

    for i, code in enumerate(todo):
        try:
            data = bp.get_financial_dict(code)
            if data and (data.get("roe") is not None or data.get("gross_margin") is not None):
                success += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            if i < 5 or i % 200 == 0:
                logger.warning("backfill_baostock: %s failed: %s", code, e)

        if (i + 1) % log_every == 0:
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed
            eta = (len(todo) - i - 1) / rate / 3600 if rate > 0 else 0
            print(f"  [{i+1}/{len(todo)}] success={success} failed={failed} "
                  f"rate={rate:.2f}/s ETA={eta:.1f}h")

    elapsed = time.time() - t_start
    print(f"\n=== Done in {elapsed/3600:.1f}h ===")
    print(f"Success: {success}")
    print(f"Failed:  {failed}")
    print(f"Total cached now: {len(cached) + success}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--log-every", type=int, default=50)
    args = parser.parse_args()
    main(limit=args.limit, log_every=args.log_every)
