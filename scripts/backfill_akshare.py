"""
Bulk backfill A-share financials via akshare (Commit 6-L.6).

Fetches income + balance sheet for ALL ~5200 stocks across multiple reporting
periods in ~2 minutes (vs baostock's 13 hours). Each stock_lrb_em(date) call
returns every stock for that period - the inverse of per-stock fetching.

Usage:
    # Default: last 8 quarters (2 years, enough for growth_1y)
    python scripts/backfill_akshare.py

    # Custom: last 3 years
    python scripts/backfill_akshare.py --years 3

    # Specific periods
    python scripts/backfill_akshare.py --periods 20240331 20240630 20240931 20241231
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.akshare_provider import AkshareProvider


def generate_periods(years: int = 2) -> list[str]:
    """Generate YYYYMMDD report-date strings for the last N years (quarterly)."""
    periods = []
    now = datetime.now()
    cur_year = now.year
    cur_month = now.month
    # Current quarter (most recent disclosed)
    # Q1 reports disclosed by end of April, Q2 by end of August, etc.
    # Use the most recent completed quarter
    if cur_month <= 4:
        last_q_year = cur_year - 1
        last_q = 4
    elif cur_month <= 7:
        last_q_year = cur_year
        last_q = 1
    elif cur_month <= 10:
        last_q_year = cur_year
        last_q = 2
    else:
        last_q_year = cur_year
        last_q = 3

    q_month = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}
    start_year = cur_year - years
    for y in range(start_year, last_q_year + 1):
        for q in range(1, 5):
            # Don't go beyond the last disclosed quarter
            if y == last_q_year and q > last_q:
                break
            periods.append(f"{y}{q_month[q]}")
    return periods


def main():
    parser = argparse.ArgumentParser(description="Bulk backfill financials via akshare")
    parser.add_argument("--years", type=int, default=2, help="Years of history (default 2)")
    parser.add_argument(
        "--periods", nargs="*", default=None, help="Specific periods (YYYYMMDD), overrides --years"
    )
    parser.add_argument(
        "--no-enrich", action="store_true", help="Skip PE/PB/mcap enrichment (faster)"
    )
    args = parser.parse_args()

    periods = args.periods or generate_periods(args.years)
    print("=== Akshare Bulk Backfill ===")
    print(f"Periods: {len(periods)}")
    for p in periods:
        print(f"  {p[:4]}-{p[4:6]}-{p[6:8]}")
    print()

    provider = AkshareProvider()
    import time

    t0 = time.time()
    total = provider.backfill_periods(periods, enrich_market=not args.no_enrich)
    elapsed = time.time() - t0
    print(f"\n=== Done in {elapsed:.1f}s ===")
    print(f"Total rows written: {total}")
    print(f"Speed: {total / elapsed:.0f} rows/s")

    # Verify with Maotai
    print("\n=== Verify: 茅台 600519 ===")
    d = provider.get_financial_dict("600519")
    print(f"  roe: {d.get('roe')}")
    print(f"  pe_ttm: {d.get('pe_ttm')}")
    print(f"  revenue_growth_1y: {d.get('revenue_growth_1y')}")
    print(f"  earnings_growth_1y: {d.get('earnings_growth_1y')}")
    print(f"  debt_to_equity: {d.get('debt_to_equity')}")


if __name__ == "__main__":
    main()
