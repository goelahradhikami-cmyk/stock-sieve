"""
Backfill market_regime_snapshots from 000300 history (Commit 6-L.7).

Usage:
    python scripts/backfill_market_regime.py [--start 2024-01-01]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market.regime_bootstrap import RegimeBootstrap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-01", help="Start date (default 2024-01-01)")
    args = parser.parse_args()

    print("=== Market Regime Bootstrap ===")
    print(f"Start date: {args.start}")
    rb = RegimeBootstrap()
    n = rb.backfill_history(args.start)
    print(f"Wrote {n} regime labels")
    print()
    dist = rb.get_regime_distribution()
    print("=== Regime distribution ===")
    total = sum(dist.values())
    for regime, count in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {regime:18s}: {count:4d} ({count / total * 100:.1f}%)")
    print()
    # Sample
    import sqlite3

    conn = sqlite3.connect("data/evaluation.db")
    rows = conn.execute(
        "SELECT obs_date, regime_type FROM market_regime_snapshots ORDER BY obs_date DESC LIMIT 5"
    ).fetchall()
    print("=== Latest 5 ===")
    for r in rows:
        print(f"  {r[0]}: {r[1]}")
    conn.close()


if __name__ == "__main__":
    main()
