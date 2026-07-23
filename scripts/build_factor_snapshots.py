"""
Build stock factor snapshots for a given date.

Commit 6-L.6: precomputes daily factor scores for the universe so sandbox
backtesting only needs SQL reweighting.

Usage:
    python scripts/build_factor_snapshots.py --date 2026-07-17 [--limit 200]
    python scripts/build_factor_snapshots.py --date 2026-07-17 --range 2026-07-10:2026-07-17
"""

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.factors.snapshot_builder import FactorSnapshotBuilder


def main():
    parser = argparse.ArgumentParser(description="Build stock factor snapshots")
    parser.add_argument("--date", type=str, required=True, help="Trade date (YYYY-MM-DD)")
    parser.add_argument(
        "--range", type=str, default=None, help="Date range start:end (builds each day in range)"
    )
    parser.add_argument("--limit", type=int, default=None, help="Max stocks per date")
    args = parser.parse_args()

    builder = FactorSnapshotBuilder()

    if args.range:
        start_str, end_str = args.range.split(":")
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
        d = start
        while d <= end:
            # Skip weekends
            if d.weekday() < 5:
                print(f"\n--- {d.isoformat()} ---")
                n = builder.build_for_date(d.isoformat(), limit=args.limit)
                print(f"  {n} rows written")
            d += timedelta(days=1)
    else:
        n = builder.build_for_date(args.date, limit=args.limit)
        print(f"Built {n} snapshot rows for {args.date}")


if __name__ == "__main__":
    main()
