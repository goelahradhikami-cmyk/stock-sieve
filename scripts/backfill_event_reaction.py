"""
Backfill Event Reaction Data - Commit 6-S.15.1 (v3.3 Phase 1).

Populates the earnings_event_reaction table for all stock-announcement
pairs relevant to v3 candidate episodes. This is the event-study data
layer that the Expectation Gap Engine will consume.

Strategy:
  1. For each v3 candidate in shadow_candidates_v3, find the most recent
     earnings announcement (available_date) BEFORE the episode's trade_date.
  2. Compute the event reaction (t1/t5/t10/t20 forward returns).
  3. Also backfill for ALL announcements in the TDX coverage window
     (2021-08-02 to 2026-07-17) to build a complete event-reaction database.

Usage:
    python scripts/backfill_event_reaction.py                    # v3 candidates only
    python scripts/backfill_event_reaction.py --full             # all announcements
    python scripts/backfill_event_reaction.py --code 600519      # single stock
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.event_reaction import EventReactionCalculator
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"


def backfill_v3_candidates():
    """Backfill event reaction for v3 candidates (fast, targeted)."""
    calc = EventReactionCalculator()
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    cache = sqlite3.connect(CACHE_DB)

    # Get all v3 candidates with their episode trade dates
    candidates = shadow.execute(
        """SELECT DISTINCT v.security_id, v.trade_date, e.episode_id
           FROM shadow_candidates_v3 v
           JOIN shadow_episode e ON e.episode_id = v.episode_id
           ORDER BY v.trade_date"""
    ).fetchall()
    print(f"v3 candidates to process: {len(candidates)}", flush=True)

    # For each (code, trade_date), find the most recent available_date
    # BEFORE trade_date, then compute event reaction
    written = 0
    skipped = 0
    batch = []

    for i, cand in enumerate(candidates):
        code = cand["security_id"]
        trade_date = cand["trade_date"]

        # Find most recent earnings announcement before trade_date
        ann = cache.execute(
            "SELECT available_date FROM akshare_financials "
            "WHERE code = ? AND available_date IS NOT NULL "
            "AND available_date <= ? "
            "ORDER BY available_date DESC LIMIT 1",
            (code, trade_date),
        ).fetchone()
        if not ann or not ann[0]:
            skipped += 1
            continue

        available_date = ann[0]

        # Check if already computed
        existing = cache.execute(
            "SELECT 1 FROM earnings_event_reaction WHERE security_id = ? AND available_date = ?",
            (code, available_date),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        # Compute event reaction
        try:
            result = calc.compute(code, available_date)
            batch.append(_result_to_tuple(result))
            written += 1
        except Exception as e:
            if skipped < 3:
                print(f"  WARN: {code}@{available_date}: {e}", flush=True)
            skipped += 1

        if len(batch) >= 50:
            _commit_batch(cache, batch)
            batch = []
            print(
                f"  ... {i + 1}/{len(candidates)} processed ({written} written, {skipped} skipped)",
                flush=True,
            )

    if batch:
        _commit_batch(cache, batch)

    cache.commit()
    print(f"\nDone: {written} written, {skipped} skipped", flush=True)
    _print_summary(cache)

    cache.close()
    shadow.close()


def backfill_full(start_date: str = "2021-08-02"):
    """Backfill event reaction for ALL announcements (slow, comprehensive)."""
    calc = EventReactionCalculator()
    cache = sqlite3.connect(CACHE_DB)

    # Get all unique (code, available_date) pairs in TDX coverage
    announcements = cache.execute(
        "SELECT DISTINCT code, available_date FROM akshare_financials "
        "WHERE available_date IS NOT NULL AND available_date >= ? "
        "ORDER BY available_date",
        (start_date,),
    ).fetchall()
    print(f"Announcements to process: {len(announcements)}", flush=True)

    written = 0
    skipped = 0
    batch = []

    for i, (code, available_date) in enumerate(announcements):
        # Check if already computed
        existing = cache.execute(
            "SELECT 1 FROM earnings_event_reaction WHERE security_id = ? AND available_date = ?",
            (code, available_date),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        try:
            result = calc.compute(code, available_date)
            batch.append(_result_to_tuple(result))
            written += 1
        except Exception:
            skipped += 1

        if len(batch) >= 100:
            _commit_batch(cache, batch)
            batch = []
            if (i + 1) % 1000 == 0:
                print(
                    f"  ... {i + 1}/{len(announcements)} processed ({written} written)", flush=True
                )

    if batch:
        _commit_batch(cache, batch)

    cache.commit()
    print(f"\nDone: {written} written, {skipped} skipped", flush=True)
    _print_summary(cache)

    cache.close()


def backfill_single(code: str):
    """Backfill event reaction for a single stock."""
    calc = EventReactionCalculator()
    cache = sqlite3.connect(CACHE_DB)

    announcements = cache.execute(
        "SELECT DISTINCT available_date FROM akshare_financials "
        "WHERE code = ? AND available_date IS NOT NULL "
        "ORDER BY available_date",
        (code,),
    ).fetchall()
    print(f"Announcements for {code}: {len(announcements)}", flush=True)

    written = 0
    for available_date in announcements:
        result = calc.compute(code, available_date[0])
        cache.execute(
            """INSERT OR REPLACE INTO earnings_event_reaction
               (security_id, available_date, announcement_date,
                earnings_yoy_current, earnings_yoy_previous, earnings_yoy_previous2,
                earnings_acceleration, earnings_acceleration_2nd, frm_direction,
                return_t1, return_t5, return_t10, return_t20,
                market_return_t1, market_return_t5, market_return_t10, market_return_t20,
                market_adjusted_t1, market_adjusted_t5, market_adjusted_t10, market_adjusted_t20,
                sector_code, sector_return_t1, sector_return_t5, sector_return_t10, sector_return_t20,
                sector_adjusted_t1, sector_adjusted_t5, sector_adjusted_t10, sector_adjusted_t20,
                residual_t5, residual_t20)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            _result_to_tuple(result),
        )
        written += 1
    cache.commit()
    print(f"Written: {written}", flush=True)
    _print_summary(cache)
    cache.close()


def _result_to_tuple(result):
    return (
        result.security_id,
        result.available_date,
        None,  # announcement_date
        result.earnings_yoy_current,
        result.earnings_yoy_previous,
        result.earnings_yoy_previous2,
        result.earnings_acceleration,
        result.earnings_acceleration_2nd,
        result.frm_direction,
        result.return_t1,
        result.return_t5,
        result.return_t10,
        result.return_t20,
        result.market_return_t1,
        result.market_return_t5,
        result.market_return_t10,
        result.market_return_t20,
        result.market_adjusted_t1,
        result.market_adjusted_t5,
        result.market_adjusted_t10,
        result.market_adjusted_t20,
        result.sector_code,
        result.sector_return_t1,
        result.sector_return_t5,
        result.sector_return_t10,
        result.sector_return_t20,
        result.sector_adjusted_t1,
        result.sector_adjusted_t5,
        result.sector_adjusted_t10,
        result.sector_adjusted_t20,
        result.residual_t5,
        result.residual_t20,
    )


def _commit_batch(cache: sqlite3.Connection, batch: list):
    cache.executemany(
        """INSERT OR REPLACE INTO earnings_event_reaction
           (security_id, available_date, announcement_date,
            earnings_yoy_current, earnings_yoy_previous, earnings_yoy_previous2,
            earnings_acceleration, earnings_acceleration_2nd, frm_direction,
            return_t1, return_t5, return_t10, return_t20,
            market_return_t1, market_return_t5, market_return_t10, market_return_t20,
            market_adjusted_t1, market_adjusted_t5, market_adjusted_t10, market_adjusted_t20,
            sector_code, sector_return_t1, sector_return_t5, sector_return_t10, sector_return_t20,
            sector_adjusted_t1, sector_adjusted_t5, sector_adjusted_t10, sector_adjusted_t20,
            residual_t5, residual_t20)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        batch,
    )
    cache.commit()


def _print_summary(cache: sqlite3.Connection):
    print("\n=== earnings_event_reaction Summary ===", flush=True)
    total = cache.execute("SELECT COUNT(*) FROM earnings_event_reaction").fetchone()[0]
    print(f"  Total rows: {total}", flush=True)

    if total == 0:
        return

    # By frm_direction
    print("\n  By FRM direction:", flush=True)
    for r in cache.execute(
        "SELECT frm_direction, COUNT(*) c, "
        "AVG(sector_adjusted_t5) avg_sa5, AVG(residual_t5) avg_res5 "
        "FROM earnings_event_reaction "
        "WHERE sector_adjusted_t5 IS NOT NULL "
        "GROUP BY frm_direction ORDER BY c DESC"
    ).fetchall():
        print(
            f"    {r[0] or 'null':14s}: N={r[1]:5d} "
            f"avg_sector_adj_t5={r[2]:+.4f} avg_residual_t5={r[3]:+.4f}",
            flush=True,
        )

    # Sector adjusted t5 distribution
    print("\n  sector_adjusted_t5 distribution:", flush=True)
    for _r in cache.execute(
        "SELECT COUNT(*) c, AVG(sector_adjusted_t5) avg, "
        "MIN(sector_adjusted_t5) mn, MAX(sector_adjusted_t5) mx "
        "FROM earnings_event_reaction WHERE sector_adjusted_t5 IS NOT NULL"
    ).fetchone():
        pass
    row = cache.execute(
        "SELECT COUNT(*) c, AVG(sector_adjusted_t5) avg, "
        "MIN(sector_adjusted_t5) mn, MAX(sector_adjusted_t5) mx "
        "FROM earnings_event_reaction WHERE sector_adjusted_t5 IS NOT NULL"
    ).fetchone()
    if row and row[0] > 0:
        print(f"    N={row[0]} mean={row[1]:+.4f} min={row[2]:+.4f} max={row[3]:+.4f}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Backfill Event Reaction Data (6-S.15.1)")
    parser.add_argument("--full", action="store_true", help="Backfill ALL announcements (slow)")
    parser.add_argument("--code", type=str, default=None, help="Single stock code")
    args = parser.parse_args()

    if args.code:
        backfill_single(args.code)
    elif args.full:
        backfill_full()
    else:
        backfill_v3_candidates()


if __name__ == "__main__":
    main()
