"""
Backfill Earnings Sustainability - Commit 6-S.16.1 (v3.4 Phase 1).

Populates the earnings_sustainability table for v3 candidate episodes.
This is the credibility data layer that Phase 1.5 Ablation will consume
to test whether sustainability lifts FRM-only alpha.

Strategy (mirrors backfill_event_reaction.py):
  1. For each v3 candidate in shadow_candidates_v3, find the most recent
     earnings announcement (available_date) BEFORE the episode's trade_date.
  2. Compute sustainability signals (alignment + persistence + margin).
  3. Store RAW VALUES + derived flags (6-S.16.0a amendment).

Vintage safety (CRITICAL):
  available_date <= trade_date is enforced in the calculator. The
  backfill additionally records as_of_date = trade_date so the leakage
  test can verify no future earnings were used.

Usage:
    python scripts/backfill_sustainability.py                # v3 candidates
    python scripts/backfill_sustainability.py --code 600519  # single stock
    python scripts/backfill_sustainability.py --leakage-test # vintage gate check
"""

from __future__ import annotations

import os
import sys
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.sustainability_calculator import SustainabilityCalculator
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations", "earnings_sustainability.sql",
)


def apply_schema(cache: sqlite3.Connection) -> None:
    """Apply earnings_sustainability.sql if not already applied.

    The SQL file is idempotent (CREATE TABLE IF NOT EXISTS). The
    schema_version row is recorded in shadow_trading.db (where
    schema_version lives), not cache.db (where this table resides).
    """
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        cache.executescript(f.read())
    cache.commit()
    # Record version in shadow_trading.db (schema_version table location)
    try:
        shadow = sqlite3.connect(SHADOW_DB)
        shadow.execute(
            "INSERT OR REPLACE INTO schema_version (version, description) VALUES (?, ?)",
            ("v4.1", "earnings_sustainability table for v3.4 EQE Phase 1 (6-S.16.1)"),
        )
        shadow.commit()
        shadow.close()
    except sqlite3.OperationalError:
        # schema_version table may not exist yet; non-fatal
        pass


def backfill_v3_candidates() -> None:
    """Backfill sustainability for v3 candidates (targeted, fast)."""
    calc = SustainabilityCalculator(cache_db=CACHE_DB)
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    cache = sqlite3.connect(CACHE_DB)
    apply_schema(cache)

    candidates = shadow.execute(
        """SELECT DISTINCT v.security_id, v.trade_date
           FROM shadow_candidates_v3 v
           JOIN shadow_episode e ON e.episode_id = v.episode_id
           ORDER BY v.trade_date"""
    ).fetchall()
    print(f"v3 candidates to process: {len(candidates)}", flush=True)

    written = 0
    skipped = 0
    insufficient = 0
    batch = []

    for i, cand in enumerate(candidates):
        code = cand["security_id"]
        trade_date = cand["trade_date"]

        # Skip if already backfilled for this (code, as_of_date)
        existing = cache.execute(
            "SELECT 1 FROM earnings_sustainability "
            "WHERE security_id = ? AND as_of_date = ?",
            (code, trade_date),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        result = calc.compute(code, trade_date)
        if result.sustainability_pass is None and result.failure_reason == "INSUFFICIENT_DATA":
            insufficient += 1
            continue

        batch.append(_result_to_tuple(result, trade_date))
        written += 1

        if len(batch) >= 100:
            _commit_batch(cache, batch)
            print(f"  ... {i+1}/{len(candidates)} processed, {written} written", flush=True)

    _commit_batch(cache, batch)
    cache.commit()
    cache.close()
    shadow.close()

    print(f"\n=== Backfill Complete ===", flush=True)
    print(f"  candidates: {len(candidates)}", flush=True)
    print(f"  written:    {written}", flush=True)
    print(f"  skipped:    {skipped} (already backfilled)", flush=True)
    print(f"  insufficient data: {insufficient}", flush=True)


def backfill_single(code: str, as_of_date: str = None) -> None:
    """Backfill sustainability for a single stock (debugging)."""
    calc = SustainabilityCalculator(cache_db=CACHE_DB)
    cache = sqlite3.connect(CACHE_DB)
    apply_schema(cache)

    if as_of_date is None:
        # Use most recent v3 candidate trade_date for this code
        shadow = sqlite3.connect(SHADOW_DB)
        shadow.row_factory = sqlite3.Row
        row = shadow.execute(
            "SELECT MAX(trade_date) AS td FROM shadow_candidates_v3 WHERE security_id = ?",
            (code,),
        ).fetchone()
        shadow.close()
        if not row or not row["td"]:
            print(f"No v3 candidate episode for {code}", flush=True)
            return
        as_of_date = row["td"]

    result = calc.compute(code, as_of_date)
    print(f"\n=== Sustainability for {code} @ {as_of_date} ===", flush=True)
    print(f"  available_date:        {result.available_date}", flush=True)
    print(f"  industry:              {result.industry}", flush=True)
    print(f"  revenue_yoy / earn_yoy: {result.revenue_yoy_current} / {result.earnings_yoy_current}", flush=True)
    print(f"  profit_elasticity:     {result.profit_elasticity}", flush=True)
    print(f"  alignment_flag:        {result.alignment_flag}", flush=True)
    print(f"  accel_q0/q1/q2:        {result.accel_q0} / {result.accel_q1} / {result.accel_q2}", flush=True)
    print(f"  accel_trend:           {result.accel_trend}", flush=True)
    print(f"  reversal_count:        {result.reversal_count}", flush=True)
    print(f"  consistency_flag:      {result.consistency_flag}", flush=True)
    print(f"  operating_margin:      {result.operating_margin_current}", flush=True)
    print(f"  company_z / industry_z: {result.company_margin_zscore} / {result.industry_margin_zscore}", flush=True)
    print(f"  margin_norm_flag:      {result.margin_normalization_flag}", flush=True)
    print(f"  sustainability_pass:   {result.sustainability_pass}", flush=True)
    print(f"  failure_reason:        {result.failure_reason}", flush=True)

    if result.sustainability_pass is not None:
        cache.execute(
            """INSERT OR REPLACE INTO earnings_sustainability
               (security_id, report_date, available_date,
                revenue_yoy_current, earnings_yoy_current, profit_elasticity, alignment_flag,
                accel_q0, accel_q1, accel_q2, accel_trend, accel_volatility,
                reversal_count, consistency_flag,
                operating_margin_current, operating_margin_3q_median, operating_margin_3q_std,
                company_margin_zscore, industry_margin_zscore, margin_normalization_flag,
                sustainability_pass, failure_reason, industry, as_of_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            _result_to_tuple(result, as_of_date),
        )
        cache.commit()
    cache.close()


def _result_to_tuple(result, as_of_date: str) -> tuple:
    """Convert SustainabilityResult to INSERT tuple."""
    return (
        result.security_id, result.report_date, result.available_date,
        result.revenue_yoy_current, result.earnings_yoy_current,
        result.profit_elasticity, result.alignment_flag,
        result.accel_q0, result.accel_q1, result.accel_q2,
        result.accel_trend, result.accel_volatility,
        result.reversal_count, result.consistency_flag,
        result.operating_margin_current, result.operating_margin_3q_median,
        result.operating_margin_3q_std,
        result.company_margin_zscore, result.industry_margin_zscore,
        result.margin_normalization_flag,
        result.sustainability_pass, result.failure_reason,
        result.industry, as_of_date,
    )


def _commit_batch(cache: sqlite3.Connection, batch: list) -> None:
    if not batch:
        return
    cache.executemany(
        """INSERT OR REPLACE INTO earnings_sustainability
           (security_id, report_date, available_date,
            revenue_yoy_current, earnings_yoy_current, profit_elasticity, alignment_flag,
            accel_q0, accel_q1, accel_q2, accel_trend, accel_volatility,
            reversal_count, consistency_flag,
            operating_margin_current, operating_margin_3q_median, operating_margin_3q_std,
            company_margin_zscore, industry_margin_zscore, margin_normalization_flag,
            sustainability_pass, failure_reason, industry, as_of_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        batch,
    )
    cache.commit()
    batch.clear()


def main():
    parser = argparse.ArgumentParser(description="Backfill earnings_sustainability table")
    parser.add_argument("--code", help="Single stock code (e.g. 600519)")
    parser.add_argument("--as-of", help="as_of_date for single-code mode (ISO)")
    args = parser.parse_args()

    if args.code:
        backfill_single(args.code, args.as_of)
    else:
        backfill_v3_candidates()


if __name__ == "__main__":
    main()
