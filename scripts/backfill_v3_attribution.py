"""
Backfill v3 Candidate Attribution - Commit 6-S.13.6 Step 2.

Computes residual_alpha (stock_return - market_return - sector_return)
for v3 candidates in shadow_candidates_v3. This is the key data needed
for v3.2 Gate validation (residual_alpha > -2%).

Reuses the same attribution formula as backfill_candidate_attribution.py
but reads from / writes to shadow_candidates_v3.

Formula (FROZEN, no regression/Barra - would overfit at this stage):
  market_beta    = stock_return - market_return
  sector_beta    = stock_return - sector_return
  residual_alpha = stock_return - market_return - sector_return

Usage:
    python scripts/backfill_v3_attribution.py
    python scripts/backfill_v3_attribution.py --episode E20240829
"""

from __future__ import annotations

import os
import sys
import sqlite3
import argparse

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.local_provider import LocalDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"
HORIZON = 20


class V3AttributionBackfiller:
    """Backfills attribution for v3 candidates."""

    def __init__(self):
        self.shadow = sqlite3.connect(SHADOW_DB)
        self.shadow.row_factory = sqlite3.Row
        self.cache = sqlite3.connect(CACHE_DB)
        self.cache.row_factory = sqlite3.Row
        self.local = LocalDataProvider()
        self._episode_cache: dict[str, tuple] = {}
        self._stock_return_cache: dict[tuple, float | None] = {}

    def _load_episode_meta(self, episode_id: str) -> tuple | None:
        if episode_id in self._episode_cache:
            return self._episode_cache[episode_id]
        row = self.shadow.execute(
            "SELECT e.trade_date, o.market_return_t20 "
            "FROM shadow_episode e "
            "LEFT JOIN shadow_outcome o ON e.episode_id = o.episode_id "
            "WHERE e.episode_id = ?",
            (episode_id,),
        ).fetchone()
        if not row:
            return None
        trade_date = row["trade_date"]
        market_return = row["market_return_t20"]
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

    def _get_stock_return(self, code: str, start: str, end: str) -> float | None:
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

    def _get_sector_cumulative_return(self, industry: str,
                                       start: str, end: str) -> float | None:
        if not industry:
            return None
        rows = self.cache.execute(
            "SELECT return FROM industry_daily_returns "
            "WHERE industry = ? AND trade_date > ? AND trade_date <= ? "
            "ORDER BY trade_date",
            (industry, start, end),
        ).fetchall()
        if not rows:
            return None
        cumulative = 1.0
        for r in rows:
            if r[0] is not None:
                cumulative *= (1.0 + r[0])
        return cumulative - 1.0

    def _get_sector_code(self, stock_code: str) -> str | None:
        row = self.cache.execute(
            "SELECT industry FROM security_master WHERE code = ?",
            (stock_code,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def _compute_market_return(self, start: str, end: str) -> float | None:
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

    def backfill(self, episode_filter: str | None = None):
        print("=" * 60, flush=True)
        print("Backfill v3 Candidate Attribution (6-S.13.6 Step 2)", flush=True)
        print("=" * 60, flush=True)

        if episode_filter:
            candidates = self.shadow.execute(
                "SELECT id, episode_id, security_id FROM shadow_candidates_v3 "
                "WHERE episode_id = ? ORDER BY id",
                (episode_filter,),
            ).fetchall()
        else:
            candidates = self.shadow.execute(
                "SELECT id, episode_id, security_id FROM shadow_candidates_v3 "
                "ORDER BY id"
            ).fetchall()

        print(f"v3 candidates to process: {len(candidates)}", flush=True)

        stats = {
            "total": 0, "stock_return": 0, "market_return": 0,
            "sector_code": 0, "sector_return": 0, "residual_alpha": 0,
            "sector_unavailable": 0,
        }

        batch = []
        for i, cand in enumerate(candidates):
            stats["total"] += 1
            result = self._process_one(cand, stats)
            if result:
                batch.append(result)
            if len(batch) >= 100:
                self._commit_batch(batch)
                batch = []
                print(f"  ... {i+1}/{len(candidates)} processed "
                      f"({stats['residual_alpha']} with residual)", flush=True)

        if batch:
            self._commit_batch(batch)

        self._print_summary(stats)

    def _process_one(self, cand: sqlite3.Row, stats: dict) -> tuple | None:
        cand_id = cand["id"]
        episode_id = cand["episode_id"]
        stock_code = cand["security_id"]

        meta = self._load_episode_meta(episode_id)
        if not meta:
            return None
        trade_date, eval_date, market_return = meta
        if not eval_date:
            return None

        stock_return = self._get_stock_return(stock_code, trade_date, eval_date)
        if stock_return is None:
            return None
        stats["stock_return"] += 1

        if market_return is None:
            market_return = self._compute_market_return(trade_date, eval_date)
        if market_return is not None:
            stats["market_return"] += 1

        sector_code = self._get_sector_code(stock_code)
        if sector_code:
            stats["sector_code"] += 1

        sector_return = None
        if sector_code:
            sector_return = self._get_sector_cumulative_return(
                sector_code, trade_date, eval_date)
            if sector_return is not None:
                stats["sector_return"] += 1
            else:
                stats["sector_unavailable"] += 1

        market_beta = None
        sector_beta = None
        residual_alpha = None
        if market_return is not None:
            market_beta = stock_return - market_return
        if sector_return is not None:
            sector_beta = stock_return - sector_return
            if market_return is not None:
                residual_alpha = stock_return - market_return - sector_return
                stats["residual_alpha"] += 1

        return (stock_return, market_return, sector_code, sector_return,
                market_beta, sector_beta, residual_alpha, cand_id)

    def _commit_batch(self, batch: list[tuple]):
        self.shadow.executemany(
            """UPDATE shadow_candidates_v3
               SET stock_return_t20=?, market_return_t20=?, sector_code=?,
                   sector_return_t20=?, market_beta=?, sector_beta=?,
                   residual_alpha=?
               WHERE id=?""",
            batch,
        )
        self.shadow.commit()

    def _print_summary(self, stats: dict):
        print("\n" + "=" * 60, flush=True)
        print("v3 Attribution Backfill Summary", flush=True)
        print("=" * 60, flush=True)
        print(f"  Total v3 candidates:         {stats['total']}", flush=True)
        print(f"  stock_return filled:         {stats['stock_return']}", flush=True)
        print(f"  market_return filled:        {stats['market_return']}", flush=True)
        print(f"  sector_code filled:          {stats['sector_code']}", flush=True)
        print(f"  sector_return filled:        {stats['sector_return']}", flush=True)
        print(f"  residual_alpha (full attrib):{stats['residual_alpha']}", flush=True)
        print(f"  sector data unavailable:     {stats['sector_unavailable']}",
              flush=True)

        # Quick stats on residual_alpha
        rows = self.shadow.execute(
            "SELECT residual_alpha, market_beta, sector_beta, "
            "frm_direction, rs_data_available "
            "FROM shadow_candidates_v3 WHERE residual_alpha IS NOT NULL"
        ).fetchall()
        if rows:
            ra = np.array([r["residual_alpha"] for r in rows])
            mb = np.array([r["market_beta"] for r in rows])
            sb = np.array([r["sector_beta"] for r in rows])
            print(f"\n  residual_alpha: N={len(ra)} "
                  f"mean={np.mean(ra):+.4f} median={np.median(ra):+.4f} "
                  f">0: {np.mean(ra>0):.1%}", flush=True)
            print(f"  market_beta:    mean={np.mean(mb):+.4f}", flush=True)
            print(f"  sector_beta:    mean={np.mean(sb):+.4f}", flush=True)

            # By FRM direction
            print(f"\n  By FRM direction:", flush=True)
            for r in self.shadow.execute(
                "SELECT frm_direction, COUNT(*) c, AVG(residual_alpha) avg_ra "
                "FROM shadow_candidates_v3 WHERE residual_alpha IS NOT NULL "
                "GROUP BY frm_direction ORDER BY c DESC"
            ).fetchall():
                print(f"    {r['frm_direction']:14s}: N={r['c']:3d} "
                      f"avg_residual={r['avg_ra']:+.4f}", flush=True)

    def close(self):
        self.shadow.close()
        self.cache.close()


def main():
    parser = argparse.ArgumentParser(
        description="Backfill v3 Attribution (6-S.13.6 Step 2)")
    parser.add_argument("--episode", type=str, default=None)
    args = parser.parse_args()
    bf = V3AttributionBackfiller()
    try:
        bf.backfill(episode_filter=args.episode)
    finally:
        bf.close()


if __name__ == "__main__":
    main()
