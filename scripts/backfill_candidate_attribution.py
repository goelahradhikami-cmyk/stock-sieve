"""
Backfill Candidate Attribution - Commit 6-S.12.1.

Populates the Recovery Beta Decomposition fields added by migration v2:
  - stock_return_t20    (per-candidate, was only filled for BUY-selected)
  - market_return_t20   (from shadow_outcome, episode-level)
  - sector_code         (from security_master.industry)
  - sector_return_t20   (cumulative industry return over [trade_date, T+20])
  - market_beta         (stock_return - market_return)
  - sector_beta         (stock_return - sector_return)
  - residual_alpha      (stock_return - market_return - sector_return)

Data availability constraint:
  industry_daily_returns covers 2024-06-04 onwards only. Episodes before
  2024-06 will have NULL sector_return_t20 / sector_beta / residual_alpha.
  market_beta is always available (HS300 covers 2021+). This limitation
  is reported, not hidden.

Usage:
    python scripts/backfill_candidate_attribution.py
    python scripts/backfill_candidate_attribution.py --episode E20250813
    python scripts/backfill_candidate_attribution.py --limit 100
"""

from __future__ import annotations

import os
import sys
import sqlite3
import argparse
from datetime import date
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.local_provider import LocalDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"
HORIZON = 20


class AttributionBackfiller:
    """Backfills Recovery Beta Decomposition for shadow_candidates."""

    def __init__(self):
        self.shadow = sqlite3.connect(SHADOW_DB)
        self.shadow.row_factory = sqlite3.Row
        self.cache = sqlite3.connect(CACHE_DB)
        self.cache.row_factory = sqlite3.Row
        self.local = LocalDataProvider()

        # Cache: episode_id -> (trade_date, eval_date, market_return_t20)
        self._episode_cache: dict[str, tuple] = {}
        # Cache: (code, start, end) -> stock_return
        self._stock_return_cache: dict[tuple, float | None] = {}
        # Cache: industry -> set of trade_dates available (for range queries)
        self._industry_dates_cache: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Episode metadata
    # ------------------------------------------------------------------

    def _load_episode_meta(self, episode_id: str) -> tuple | None:
        """Load (trade_date, eval_date, market_return_t20) for an episode."""
        if episode_id in self._episode_cache:
            return self._episode_cache[episode_id]

        row = self.shadow.execute(
            "SELECT e.trade_date, o.market_return_t20, o.evaluated_at "
            "FROM shadow_episode e "
            "LEFT JOIN shadow_outcome o ON e.episode_id = o.episode_id "
            "WHERE e.episode_id = ?",
            (episode_id,),
        ).fetchone()
        if not row:
            return None

        trade_date = row["trade_date"]
        market_return = row["market_return_t20"]

        # Find T+HORIZON eval_date from trading_calendar
        eval_row = self.cache.execute(
            "SELECT trade_date FROM trading_calendar "
            "WHERE is_trading=1 AND trade_date > ? "
            "ORDER BY trade_date LIMIT 1 OFFSET ?",
            (trade_date, HORIZON - 1),
        ).fetchone()
        eval_date = eval_row[0] if eval_row else None

        meta = (trade_date, eval_date, market_return)
        self._episode_cache[episode_id] = meta
        return meta

    # ------------------------------------------------------------------
    # Stock return
    # ------------------------------------------------------------------

    def _get_stock_return(self, code: str, start: str, end: str) -> float | None:
        """Forward return for a stock over [start, end]."""
        key = (code, start, end)
        if key in self._stock_return_cache:
            return self._stock_return_cache[key]
        try:
            kline = self.local.get_daily_kline(code, start, end)
            if kline is not None and not kline.empty and len(kline) >= 2:
                close = kline["close"].values
                ret = float((close[-1] - close[0]) / close[0])
                self._stock_return_cache[key] = ret
                return ret
        except Exception:
            pass
        self._stock_return_cache[key] = None
        return None

    # ------------------------------------------------------------------
    # Sector return (cumulative over a date range)
    # ------------------------------------------------------------------

    def _get_sector_cumulative_return(self, industry: str,
                                       start: str, end: str) -> float | None:
        """Cumulative industry return over [start, end] by compounding daily returns.

        industry_daily_returns stores daily returns (not prices), so we
        compound: prod(1 + r_i) - 1.
        """
        if not industry:
            return None
        rows = self.cache.execute(
            "SELECT trade_date, return FROM industry_daily_returns "
            "WHERE industry = ? AND trade_date > ? AND trade_date <= ? "
            "ORDER BY trade_date",
            (industry, start, end),
        ).fetchall()
        if not rows:
            return None
        cumulative = 1.0
        for r in rows:
            if r["return"] is not None:
                cumulative *= (1.0 + r["return"])
        return cumulative - 1.0

    # ------------------------------------------------------------------
    # Sector code lookup
    # ------------------------------------------------------------------

    def _get_sector_code(self, stock_code: str) -> str | None:
        """Look up the stock's industry from security_master."""
        row = self.cache.execute(
            "SELECT industry FROM security_master WHERE code = ?",
            (stock_code,),
        ).fetchone()
        if row and row["industry"]:
            return row["industry"]
        return None

    # ------------------------------------------------------------------
    # Main backfill
    # ------------------------------------------------------------------

    def backfill_all(self, episode_filter: str | None = None,
                     limit: int | None = None) -> dict:
        """Backfill attribution for all candidates (or one episode)."""
        print("=" * 60, flush=True)
        print("Backfill Candidate Attribution (6-S.12.1)", flush=True)
        print("=" * 60, flush=True)

        if episode_filter:
            candidates = self.shadow.execute(
                "SELECT id, episode_id, stock_code, stock_return_t20 "
                "FROM shadow_candidates WHERE episode_id = ? ORDER BY id",
                (episode_filter,),
            ).fetchall()
        else:
            candidates = self.shadow.execute(
                "SELECT id, episode_id, stock_code, stock_return_t20 "
                "FROM shadow_candidates ORDER BY id"
            ).fetchall()

        if limit:
            candidates = candidates[:limit]

        print(f"Candidates to process: {len(candidates)}", flush=True)

        stats = {
            "total": 0,
            "stock_return_filled": 0,
            "market_return_filled": 0,
            "sector_code_filled": 0,
            "sector_return_filled": 0,
            "residual_alpha_filled": 0,
            "sector_data_unavailable": 0,  # pre-2024-06 episodes
        }

        batch = []
        for i, cand in enumerate(candidates):
            stats["total"] += 1
            result = self._process_one(cand, stats)
            if result:
                batch.append(result)

            # Commit in batches of 200
            if len(batch) >= 200:
                self._commit_batch(batch)
                batch = []
                print(f"  ... {i+1}/{len(candidates)} processed "
                      f"({stats['residual_alpha_filled']} with full attribution)",
                      flush=True)

        if batch:
            self._commit_batch(batch)

        self._print_summary(stats)
        return stats

    def _process_one(self, cand: sqlite3.Row, stats: dict) -> tuple | None:
        """Process one candidate row. Returns update tuple or None."""
        cand_id = cand["id"]
        episode_id = cand["episode_id"]
        stock_code = cand["stock_code"]

        meta = self._load_episode_meta(episode_id)
        if not meta:
            return None
        trade_date, eval_date, market_return = meta
        if not eval_date:
            return None

        # 1. Stock return (may already exist for BUY-selected)
        stock_return = cand["stock_return_t20"]
        if stock_return is None:
            stock_return = self._get_stock_return(stock_code, trade_date, eval_date)
            if stock_return is not None:
                stats["stock_return_filled"] += 1
        if stock_return is None:
            return None  # can't compute anything without stock return

        # 2. Market return (episode-level, from shadow_outcome)
        if market_return is None:
            # Fallback: compute from HS300
            market_return = self._compute_market_return(trade_date, eval_date)
        if market_return is not None:
            stats["market_return_filled"] += 1

        # 3. Sector code
        sector_code = self._get_sector_code(stock_code)
        if sector_code:
            stats["sector_code_filled"] += 1

        # 4. Sector return (cumulative over [trade_date, eval_date])
        sector_return = None
        if sector_code:
            sector_return = self._get_sector_cumulative_return(
                sector_code, trade_date, eval_date)
            if sector_return is not None:
                stats["sector_return_filled"] += 1
            else:
                stats["sector_data_unavailable"] += 1

        # 5. Compute betas
        market_beta = None
        sector_beta = None
        residual_alpha = None
        if market_return is not None:
            market_beta = stock_return - market_return
        if sector_return is not None:
            sector_beta = stock_return - sector_return
            if market_return is not None:
                residual_alpha = stock_return - market_return - sector_return
                stats["residual_alpha_filled"] += 1

        return (stock_return, market_return, sector_code, sector_return,
                market_beta, sector_beta, residual_alpha, cand_id)

    def _compute_market_return(self, start: str, end: str) -> float | None:
        """HS300 return over [start, end] from market_index_daily."""
        p0 = self._index_close("000300", start)
        p1 = self._index_close("000300", end)
        if p0 is None or p1 is None or p0 == 0:
            return None
        return (p1 - p0) / p0

    def _index_close(self, code: str, trade_date: str) -> float | None:
        row = self.cache.execute(
            "SELECT adj_close FROM market_index_daily "
            "WHERE index_code=? AND trade_date=?",
            (code, trade_date),
        ).fetchone()
        if not row or row[0] is None:
            row = self.cache.execute(
                "SELECT adj_close FROM market_index_daily "
                "WHERE index_code=? AND trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 1",
                (code, trade_date),
            ).fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])

    def _commit_batch(self, batch: list[tuple]):
        """Commit a batch of candidate updates."""
        self.shadow.executemany(
            """UPDATE shadow_candidates
               SET stock_return_t20=?, market_return_t20=?, sector_code=?,
                   sector_return_t20=?, market_beta=?, sector_beta=?,
                   residual_alpha=?
               WHERE id=?""",
            batch,
        )
        self.shadow.commit()

    def _print_summary(self, stats: dict):
        print("\n" + "=" * 60, flush=True)
        print("Backfill Summary", flush=True)
        print("=" * 60, flush=True)
        print(f"  Total candidates processed:   {stats['total']}", flush=True)
        print(f"  stock_return filled:          {stats['stock_return_filled']}",
              flush=True)
        print(f"  market_return filled:         {stats['market_return_filled']}",
              flush=True)
        print(f"  sector_code filled:           {stats['sector_code_filled']}",
              flush=True)
        print(f"  sector_return filled:         {stats['sector_return_filled']}",
              flush=True)
        print(f"  residual_alpha (full attrib): {stats['residual_alpha_filled']}",
              flush=True)
        print(f"  sector data unavailable:      {stats['sector_data_unavailable']} "
              f"(pre-2024-06 episodes)", flush=True)

        # Verify with a quick query
        total = self.shadow.execute(
            "SELECT COUNT(*) FROM shadow_candidates "
            "WHERE stock_return_t20 IS NOT NULL").fetchone()[0]
        with_residual = self.shadow.execute(
            "SELECT COUNT(*) FROM shadow_candidates "
            "WHERE residual_alpha IS NOT NULL").fetchone()[0]
        with_market = self.shadow.execute(
            "SELECT COUNT(*) FROM shadow_candidates "
            "WHERE market_beta IS NOT NULL").fetchone()[0]
        print(f"\n  Verification (DB counts):", flush=True)
        print(f"    candidates with stock_return: {total}", flush=True)
        print(f"    candidates with market_beta:   {with_market}", flush=True)
        print(f"    candidates with residual_alpha:{with_residual}", flush=True)

    def close(self):
        self.shadow.close()
        self.cache.close()


def main():
    parser = argparse.ArgumentParser(
        description="Backfill Recovery Beta Decomposition (6-S.12.1)")
    parser.add_argument("--episode", type=str, default=None,
                        help="Process only one episode (e.g. E20250813)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of candidates (for testing)")
    args = parser.parse_args()

    backfiller = AttributionBackfiller()
    try:
        backfiller.backfill_all(episode_filter=args.episode, limit=args.limit)
    finally:
        backfiller.close()


if __name__ == "__main__":
    main()
