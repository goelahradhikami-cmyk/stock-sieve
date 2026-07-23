"""
Backfill liquidity fields in security_master from LOCAL TDX .day files.

Fills two fields that were never populated (left at 0), using only the offline
LocalDataProvider — no network calls:

  avg_amount_20d : mean of the last 20 daily `amount` (turnover value, in CNY)
  list_days      : number of trading-day bars available (proxy for days listed)

Idempotent: re-running just recomputes. Stocks with no local .day file are
skipped (counted in `missing`).

Run:
    python scripts/backfill_liquidity.py
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data.local_provider import LocalDataProvider
from src.data.security_master import SecurityMaster


def main():
    db_path = os.path.join(PROJECT_ROOT, "data", "cache.db")
    sm = SecurityMaster(db_path)
    local = LocalDataProvider()

    df = sm.get_active_universe()
    print(f"Active, non-ST stocks in security_master: {len(df)}")

    updated = 0
    skipped_no_data = 0
    skipped_short = 0

    for _, row in df.iterrows():
        code = str(row["code"])
        kline = local.get_daily_kline(code)
        if kline is None or kline.empty:
            skipped_no_data += 1
            continue

        n = len(kline)
        if n < 5:
            skipped_short += 1
            continue

        last20 = kline["amount"].tail(20)
        avg_amount = float(last20.mean()) if len(last20) else float(kline["amount"].mean())
        list_days = int(n)

        sm.db.execute(
            "UPDATE security_master SET avg_amount_20d=?, list_days=? WHERE code=?",
            (avg_amount, list_days, code),
        )
        updated += 1

    sm.db.commit()

    print(f"  updated        : {updated}")
    print(f"  skipped (no .day): {skipped_no_data}")
    print(f"  skipped (<5 bars): {skipped_short}")

    # Sanity: how many now have non-zero liquidity?
    non_zero = sm.db.execute(
        "SELECT COUNT(*) FROM security_master WHERE status='active' AND is_st=0 AND avg_amount_20d > 0"
    ).fetchone()[0]
    print(f"  active+non-zero avg_amount_20d: {non_zero}")


if __name__ == "__main__":
    main()
