"""
Market Recovery Engine - Commit 6-S.1.

Predicts "when will the market admit it's wrong?" - the mispricing recovery
probability. This is NOT a stock predictor. It's a market environment
classifier that determines whether narrative divergence anomalies are likely
to correct soon or continue deteriorating.

6-S.0.5 proved: anomaly detection finds mispriced stocks, but the same
anomaly that works in 2023-01 (+9.13%) fails in 2022-03 (-10.28%). The
difference isn't the anomaly - it's the market state. In 2022-03, the market
was selling risk premium (macro fear), not business value. In 2023-01,
liquidity and breadth were recovering, so mispricings got corrected.

Four dimensions of market recovery:
  1. Breadth: are stocks broadly advancing? (advance/decline ratio)
  2. Liquidity: is trading volume recovering? (amount trend)
  3. Volatility: is panic subsiding? (vol contraction)
  4. Risk appetite: is the trend stabilizing? (MA crossover + breadth)

Recovery Probability = weighted composite (0-1)
  > 0.5: market is recovering -> anomalies likely to correct -> BUY
  < 0.3: market still in panic -> anomalies likely to worsen -> REJECT
  0.3-0.5: uncertain -> hold

Usage:
    from src.thesis.market_recovery import MarketRecoveryEngine
    engine = MarketRecoveryEngine()
    prob = engine.compute_recovery_probability("2023-01-13")
    # prob = 0.72 -> high recovery -> anomaly bets allowed
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.data.local_provider import LocalDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketState:
    """Multi-dimensional market state vector for one date."""

    date: str
    # Raw indicators
    breadth_advance_ratio: float = 0.5  # fraction of stocks advancing
    breadth_new_highs: float = 0.0  # fraction near 60d highs
    liquidity_amount_change: float = 0.0  # 20d amount trend (%)
    volatility_20d: float = 0.25  # 20d annualized vol
    volatility_change: float = 0.0  # vol change vs 60d (negative = contracting)
    trend_ma: float = 0.0  # price vs MA60 (-1 to +1)
    # Composite
    recovery_probability: float = 0.5  # 0-1 (1 = market recovering)
    state_label: str = "neutral"  # recovering / panic / uncertain

    def allows_anomaly_bets(self) -> bool:
        """Should we act on anomaly signals in this market state?

        6-S.5b: upgraded with volatility contraction gate.
        True recovery requires vol contraction (vol_change < -0.01).
        False recovery has vol_change >= 0 (volatility not subsiding).
        """
        return (
            self.recovery_probability > 0.5 and self.volatility_change < -0.01
        )  # vol must be contracting

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "breadth": round(self.breadth_advance_ratio, 3),
            "liquidity": round(self.liquidity_amount_change, 3),
            "volatility": round(self.volatility_20d, 3),
            "vol_change": round(self.volatility_change, 3),
            "trend": round(self.trend_ma, 3),
            "recovery_prob": round(self.recovery_probability, 3),
            "label": self.state_label,
        }


class MarketRecoveryEngine:
    """Computes market recovery probability from price/volume data.

    Uses existing data sources (no new data needed):
      - stock_factor_snapshot: for breadth (how many stocks have positive momentum)
      - market_index_daily (000300): for trend, volatility, liquidity
      - LocalDataProvider: for individual stock price data (breadth calc)

    The engine doesn't predict stock returns. It predicts whether the
    market environment is conducive to mispricing correction.
    """

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db
        self.local = LocalDataProvider()

    def compute_recovery_probability(self, trade_date: str) -> MarketState:
        """Compute market recovery probability for a date.

        Returns: MarketState with 4 dimensions + composite probability
        """
        state = MarketState(date=trade_date)

        # 1. Breadth: fraction of stocks with positive momentum score
        state.breadth_advance_ratio = self._compute_breadth(trade_date)
        state.breadth_new_highs = self._compute_new_highs_ratio(trade_date)

        # 2. Liquidity: 20-day amount trend
        state.liquidity_amount_change = self._compute_liquidity_trend(trade_date)

        # 3. Volatility: 20d vol + change vs 60d
        state.volatility_20d, state.volatility_change = self._compute_volatility(trade_date)

        # 4. Trend: price vs MA60
        state.trend_ma = self._compute_trend(trade_date)

        # Composite recovery probability
        state.recovery_probability = self._compute_composite(state)
        state.state_label = self._label_state(state.recovery_probability)

        return state

    def _compute_composite(self, state: MarketState) -> float:
        """Weighted composite of 4 dimensions -> recovery probability (0-1)."""
        # Breadth: >0.5 advancing = recovering
        breadth_score = max(0, min(1, (state.breadth_advance_ratio - 0.3) / 0.4))

        # Liquidity: positive amount change = recovering
        liquidity_score = max(0, min(1, 0.5 + state.liquidity_amount_change * 2))

        # Volatility: contracting (negative change) = recovering
        vol_score = max(0, min(1, 0.5 - state.volatility_change * 3))

        # Trend: above MA60 = recovering
        trend_score = max(0, min(1, 0.5 + state.trend_ma * 0.5))

        # Weighted: breadth is most important (market-wide participation)
        recovery = (
            0.35 * breadth_score + 0.25 * liquidity_score + 0.20 * vol_score + 0.20 * trend_score
        )

        return max(0, min(1, recovery))

    def _label_state(self, prob: float) -> str:
        if prob > 0.6:
            return "recovering"
        elif prob < 0.35:
            return "panic"
        else:
            return "uncertain"

    def _compute_breadth(self, trade_date: str) -> float:
        """Fraction of stocks with positive momentum (from snapshot)."""
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN momentum_score > 50 THEN 1 ELSE 0 END) "
                "FROM stock_factor_snapshot WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
            total, advancing = row[0], row[1]
            return advancing / total if total > 0 else 0.5
        finally:
            conn.close()

    def _compute_new_highs_ratio(self, trade_date: str) -> float:
        """Fraction of stocks with momentum_score > 80 (near highs)."""
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN momentum_score > 80 THEN 1 ELSE 0 END) "
                "FROM stock_factor_snapshot WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
            total, highs = row[0], row[1]
            return highs / total if total > 0 else 0.0
        finally:
            conn.close()

    def _compute_liquidity_trend(self, trade_date: str) -> float:
        """20-day amount change rate (positive = liquidity recovering)."""
        from datetime import date, timedelta

        start = (date.fromisoformat(trade_date) - timedelta(days=40)).isoformat()

        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT amount FROM market_index_daily "
                "WHERE index_code='000300' AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date",
                (start, trade_date),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 20:
            return 0.0

        amounts = [r[0] for r in rows if r[0]]
        if len(amounts) < 20:
            return 0.0

        recent_avg = np.mean(amounts[-5:])
        past_avg = np.mean(amounts[-20:-5])
        if past_avg > 0:
            return (recent_avg - past_avg) / past_avg
        return 0.0

    def _compute_volatility(self, trade_date: str) -> tuple[float, float]:
        """20d annualized vol + change vs 60d. Returns (vol_20d, vol_change)."""
        from datetime import date, timedelta

        start = (date.fromisoformat(trade_date) - timedelta(days=90)).isoformat()

        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT close FROM market_index_daily "
                "WHERE index_code='000300' AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date",
                (start, trade_date),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 30:
            return 0.25, 0.0

        closes = [r[0] for r in rows if r[0]]
        returns = np.diff(closes) / closes[:-1]

        vol_20d = np.std(returns[-20:]) * np.sqrt(252) if len(returns) >= 20 else 0.25
        vol_60d = np.std(returns[-60:]) * np.sqrt(252) if len(returns) >= 60 else vol_20d

        vol_change = vol_20d - vol_60d  # negative = contracting (good)
        return float(vol_20d), float(vol_change)

    def _compute_trend(self, trade_date: str) -> float:
        """Price vs MA60 (normalized -1 to +1)."""
        from datetime import date, timedelta

        start = (date.fromisoformat(trade_date) - timedelta(days=90)).isoformat()

        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT close FROM market_index_daily "
                "WHERE index_code='000300' AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date",
                (start, trade_date),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 20:
            return 0.0

        closes = [r[0] for r in rows if r[0]]
        current = closes[-1]
        ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else np.mean(closes)

        if ma60 > 0:
            return float(np.clip((current - ma60) / ma60, -1, 1))
        return 0.0
