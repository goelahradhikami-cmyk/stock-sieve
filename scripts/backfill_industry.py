"""
Backfill industry classification + build industry daily returns (Commit 6-L.7).

Usage:
    python scripts/backfill_industry.py [--build-returns] [--start 2024-01-01]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market.industry_bootstrap import IndustryBootstrap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build-returns", action="store_true", help="Also build industry_daily_returns (slower)"
    )
    parser.add_argument("--start", default="2024-01-01")
    args = parser.parse_args()

    ib = IndustryBootstrap()

    print("=== Industry Bootstrap ===")
    n = ib.backfill_industry()
    print(f"Updated {n} stocks with industry")
    print()
    dist = ib.get_industry_distribution()
    print("=== Top 15 industries ===")
    for ind, cnt in list(dist.items())[:15]:
        print(f"  {ind:25s}: {cnt}")
    print(f"  ... ({len(dist)} industries total)")

    if args.build_returns:
        print(f"\n=== Building industry daily returns from {args.start} ===")
        n = ib.build_industry_daily_returns(args.start)
        print(f"Wrote {n} (date, industry) rows")


if __name__ == "__main__":
    main()
