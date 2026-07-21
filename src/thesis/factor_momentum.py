"""
Factor Momentum Engine - Commit 6-Q.5a.

Replaces the broken cross-sectional dispersion proxy (6-Q.4) with real
time-series factor momentum: long-short portfolio returns per factor family.

For each factor family (value/quality/growth/momentum/risk/sentiment):
  1. Sort all stocks by factor score (from stock_factor_snapshot)
  2. Long top 20%, short bottom 20%
  3. Compute the long-short return over the holding period
  4. Track 20/60/120-day momentum of this L-S return

This produces a "Factor Climate" per date:
  value:    momentum +++, crowding medium
  growth:   momentum ---, crowding low
  momentum: momentum +,  crowding high

The Adaptive Router uses this to allocate doctrine confidence:
  if value momentum is strong -> boost value_doctrine confidence
  if momentum crowding is high -> reduce momentum_doctrine confidence

Usage:
    from src.thesis.factor_momentum import FactorMomentumEngine
    fme = FactorMomentumEngine()
    climate = fme.compute_factor_climate("2026-05-27")
    # climate = {"value": {"momentum_20d": 0.03, "momentum_60d": 0.08, ...}, ...}
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.data.local_provider import LocalDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class FactorClimate:
    """Factor momentum + crowding climate for one date."""
    date: str
    factors: dict = field(default_factory=dict)  # {factor: {momentum_20d, momentum_60d, ...}}
    market_regime: str = "unknown"

    def get_strongest_factor(self, window: str = "momentum_60d") -> str:
        """Which factor has the strongest momentum."""
        if not self.factors:
            return "neutral"
        return max(self.factors.keys(),
                   key=lambda f: self.factors[f].get(window, 0))

    def get_weakest_factor(self, window: str = "momentum_60d") -> str:
        """Which factor has the weakest momentum."""
        if not self.factors:
            return "neutral"
        return min(self.factors.keys(),
                   key=lambda f: self.factors[f].get(window, 0))


class FactorMomentumEngine:
    """Computes real time-series factor momentum via long-short portfolios.

    For each date with a stock_factor_snapshot:
      1. Load all stocks' factor scores
      2. For each factor family: long top 20%, short bottom 20%
      3. Compute equal-weight L-S return over the next 20 trading days
      4. Build a time series of L-S returns per factor
      5. Compute 20/60/120-day rolling momentum

    This is the "Factor Climate" that the Adaptive Router uses.
    """

    FACTOR_FAMILIES = ["quality", "value", "growth", "momentum", "risk", "sentiment"]
    LS_PERCENTILE = 0.20  # top 20% long, bottom 20% short
    HORIZON = 20          # 20-day forward return for L-S computation

    def __init__(self, eval_db: str = "data/evaluation.db",
                 cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db
        self.local = LocalDataProvider()

    def compute_factor_climate(self, trade_date: str,
                                lookback_days: int = 120) -> FactorClimate:
        """Compute factor climate for a date.

        Uses historical factor snapshots before trade_date to compute
        factor L-S momentum. The L-S return for each factor is computed
        by looking at PAST snapshots: for each past snapshot date,
        compute the L-S return over the following 20 days, then
        take the rolling average.

        Returns: FactorClimate with per-factor momentum + market regime
        """
        # Load market regime for this date
        market_regime = self._read_regime(trade_date)

        # Load historical snapshot dates (for computing past L-S returns)
        past_dates = self._get_past_snapshot_dates(trade_date, lookback_days)
        if len(past_dates) < 3:
            return FactorClimate(date=trade_date, market_regime=market_regime)

        # For each past snapshot date, compute factor L-S returns
        ls_returns_by_factor: dict[str, list[float]] = {
            f: [] for f in self.FACTOR_FAMILIES
        }

        for past_date in past_dates:
            ls_returns = self._compute_ls_returns_for_date(past_date)
            for factor, ret in ls_returns.items():
                if ret is not None:
                    ls_returns_by_factor[factor].append(ret)

        # Compute rolling momentum per factor
        factors_climate = {}
        for factor in self.FACTOR_FAMILIES:
            returns = ls_returns_by_factor[factor]
            if not returns:
                factors_climate[factor] = {
                    "momentum_20d": 0.0, "momentum_60d": 0.0,
                    "momentum_120d": 0.0, "win_rate": 0.5, "n_samples": 0,
                }
                continue

            # 20-day momentum: average of last min(5, len) L-S returns
            mom_20d = float(np.mean(returns[-5:])) if len(returns) >= 1 else 0.0
            # 60-day: average of last min(10, len)
            mom_60d = float(np.mean(returns[-10:])) if len(returns) >= 1 else 0.0
            # 120-day: average of all
            mom_120d = float(np.mean(returns))
            win_rate = float(np.mean([1 if r > 0 else 0 for r in returns]))

            factors_climate[factor] = {
                "momentum_20d": mom_20d,
                "momentum_60d": mom_60d,
                "momentum_120d": mom_120d,
                "win_rate": win_rate,
                "n_samples": len(returns),
            }

        return FactorClimate(
            date=trade_date,
            factors=factors_climate,
            market_regime=market_regime,
        )

    def _compute_ls_returns_for_date(self, snapshot_date: str) -> dict[str, float | None]:
        """Compute factor long-short returns for one snapshot date.

        For each factor: sort stocks by factor score, long top 20%, short
        bottom 20%, compute equal-weight L-S return over next 20 trading days.

        Returns: {factor: ls_return or None}
        """
        # Load factor scores for this date
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT security_id, quality_score, value_score, growth_score, "
                "momentum_score, risk_score, sentiment_score "
                "FROM stock_factor_snapshot WHERE trade_date=?",
                (snapshot_date,),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 20:
            return {f: None for f in self.FACTOR_FAMILIES}

        # Find eval date (snapshot_date + HORIZON trading days)
        eval_date = self._get_eval_date(snapshot_date, self.HORIZON)
        if not eval_date:
            return {f: None for f in self.FACTOR_FAMILIES}

        results = {}
        for i, factor in enumerate(self.FACTOR_FAMILIES):
            # Sort by this factor's score
            scored = [(row, row[i + 1] or 50) for row in rows]
            scored.sort(key=lambda x: x[1])

            n = len(scored)
            bottom_n = max(3, int(n * self.LS_PERCENTILE))
            top_n = max(3, int(n * self.LS_PERCENTILE))

            bottom = scored[:bottom_n]
            top = scored[-top_n:]

            # Compute returns for top and bottom
            top_returns = self._compute_group_returns(
                [r[0]["security_id"] for r in top], snapshot_date, eval_date
            )
            bottom_returns = self._compute_group_returns(
                [r[0]["security_id"] for r in bottom], snapshot_date, eval_date
            )

            if top_returns and bottom_returns:
                ls_return = float(np.mean(top_returns) - np.mean(bottom_returns))
                results[factor] = ls_return
            else:
                results[factor] = None

        return results

    def _compute_group_returns(self, security_ids: list[str],
                                start: str, end: str) -> list[float]:
        """Compute forward returns for a group of stocks."""
        returns = []
        for sec_id in security_ids:
            bare = sec_id.split(".")[0] if "." in sec_id else sec_id
            try:
                kline = self.local.get_daily_kline(bare, start, end)
                if kline is not None and not kline.empty and len(kline) >= 2:
                    close = kline["close"].values
                    returns.append((close[-1] - close[0]) / close[0])
            except Exception:
                pass
        return returns

    def _get_past_snapshot_dates(self, trade_date: str,
                                  lookback: int) -> list[str]:
        """Get snapshot dates before trade_date (for momentum computation)."""
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM stock_factor_snapshot "
                "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?",
                (trade_date, lookback // 5),  # sample every ~5 days
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def _get_eval_date(self, trade_date: str, horizon: int) -> str | None:
        conn = sqlite3.connect(self.cache_db)
        try:
            row = conn.execute(
                "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
                "AND trade_date > ? ORDER BY trade_date LIMIT 1 OFFSET ?",
                (trade_date, horizon - 1),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _read_regime(self, trade_date: str) -> str:
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT regime_type FROM market_regime_snapshots WHERE obs_date=?",
                (trade_date,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT regime_type FROM market_regime_snapshots "
                    "WHERE obs_date <= ? ORDER BY obs_date DESC LIMIT 1",
                    (trade_date,),
                ).fetchone()
            return row[0] if row else "sideway"
        finally:
            conn.close()
