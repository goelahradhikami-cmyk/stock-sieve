"""
ARCHIVED 2026-07-27 — zero production callers; superseded by the frozen
state_transition.py; see src/thesis/archive/__init__.py.

Market State Machine - Commit 6-S.5.3.

Upgrades Recovery Gate from a single threshold to a 4-state model.

6-S.5.2 proved: vol contraction alone is necessary but NOT sufficient.
2022-08-23 had vol contraction (-0.010) but was a bear market rally (91% fail rate).

The missing dimension is: sustained breadth + risk premium compression.
A true recovery requires:
  1. Volatility contracting (panic subsiding) - necessary
  2. Breadth improving (market-wide participation) - confirming
  3. Trend stabilizing (not just a bounce) - sustaining

Four states:
  PANIC:              vol high, breadth weak -> NO anomaly bets
  FALSE_RECOVERY:     price bounces but breadth/vol don't confirm -> NO bets
  EARLY_RECOVERY:     vol contracting + breadth improving -> SMALL anomaly bets
  CONFIRMED_RECOVERY: sustained recovery -> FULL anomaly bets

State transitions are one-directional (PANIC -> FALSE -> EARLY -> CONFIRMED)
but can reverse if conditions deteriorate.

Usage:
    from src.thesis.market_state_machine import MarketStateMachine
    msm = MarketStateMachine()
    state = ms.classify("2023-01-15")
    # state = MarketState(type="EARLY_RECOVERY", anomaly_weight=0.6)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketStateMachineResult:
    """Market state classification result."""

    date: str
    state_type: str  # PANIC / FALSE_RECOVERY / EARLY_RECOVERY / CONFIRMED_RECOVERY
    anomaly_weight: float  # 0.0 = no bets, 0.3 = small, 0.6 = normal, 1.0 = full
    # Sub-scores
    vol_score: float  # 0-1 (1 = vol contracting strongly)
    breadth_score: float  # 0-1 (1 = broad participation)
    trend_score: float  # 0-1 (1 = strong uptrend)
    recovery_prob: float  # from RecoveryEngine
    # Diagnosis
    state_reason: str  # why this state
    allows_anomaly: bool  # should we act on anomalies?

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "state": self.state_type,
            "anomaly_weight": round(self.anomaly_weight, 2),
            "vol_score": round(self.vol_score, 2),
            "breadth_score": round(self.breadth_score, 2),
            "trend_score": round(self.trend_score, 2),
            "recovery_prob": round(self.recovery_prob, 2),
            "allows_anomaly": self.allows_anomaly,
            "reason": self.state_reason,
        }


class MarketStateMachine:
    """4-state market classifier for anomaly permission.

    Replaces the binary recovery gate (6-S.1/6-S.5) with a nuanced model
    that distinguishes:
      - True recovery (vol + breadth + trend all confirm)
      - False recovery (vol contracts but breadth doesn't follow)
      - Early recovery (vol + breadth improving but not yet sustained)
      - Panic (vol high, breadth weak)

    Key insight from 6-S.5.2:
      2022-08-23: vol_chg=-0.010 (barely contracting), breadth=0.49 (weak)
      -> FALSE_RECOVERY (bear market rally, not real recovery)

      2023-01-15: vol_chg=-0.064 (strongly contracting), breadth=0.47
      -> EARLY_RECOVERY (vol confirmed, breadth still catching up)
    """

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db

    def classify(self, trade_date: str) -> MarketStateMachineResult:
        """Classify market state for a date.

        Returns: MarketStateMachineResult with state + anomaly permission
        """
        # Compute raw indicators
        vol_score, vol_chg = self._compute_vol_score(trade_date)
        breadth_score = self._compute_breadth_score(trade_date)
        trend_score, trend_val = self._compute_trend_score(trade_date)
        recovery_prob = self._compute_recovery_prob(trade_date)

        # State classification logic
        # PANIC: high volatility, no contraction
        if vol_score < 0.3:
            return MarketStateMachineResult(
                date=trade_date,
                state_type="PANIC",
                anomaly_weight=0.0,
                vol_score=vol_score,
                breadth_score=breadth_score,
                trend_score=trend_score,
                recovery_prob=recovery_prob,
                allows_anomaly=False,
                state_reason=f"vol_score={vol_score:.2f} < 0.3, panic not subsided",
            )

        # Check if vol is actually contracting (not just low)
        vol_contracting = vol_chg < -0.01

        # FALSE_RECOVERY: price bounced but vol not contracting OR breadth not improving
        # This catches bear market rallies like 2022-08
        if not vol_contracting:
            return MarketStateMachineResult(
                date=trade_date,
                state_type="FALSE_RECOVERY",
                anomaly_weight=0.0,
                vol_score=vol_score,
                breadth_score=breadth_score,
                trend_score=trend_score,
                recovery_prob=recovery_prob,
                allows_anomaly=False,
                state_reason=f"vol_chg={vol_chg:+.4f} >= -0.01, volatility not contracting (bear rally)",
            )

        # Vol is contracting. Now check breadth + recovery quality.
        # EARLY_RECOVERY: vol contracting + recovery_prob > 0.48 + breadth > 0.40
        # This is the minimum viable recovery (2023-01 type)
        if recovery_prob > 0.48 and breadth_score > 0.40:
            # Check if confirmed (strong vol contraction + decent breadth)
            if vol_chg < -0.03 and breadth_score > 0.45:
                return MarketStateMachineResult(
                    date=trade_date,
                    state_type="CONFIRMED_RECOVERY",
                    anomaly_weight=1.0,
                    vol_score=vol_score,
                    breadth_score=breadth_score,
                    trend_score=trend_score,
                    recovery_prob=recovery_prob,
                    allows_anomaly=True,
                    state_reason=f"vol_chg={vol_chg:+.4f} strong contraction + breadth={breadth_score:.2f} confirmed",
                )
            else:
                return MarketStateMachineResult(
                    date=trade_date,
                    state_type="EARLY_RECOVERY",
                    anomaly_weight=0.6,
                    vol_score=vol_score,
                    breadth_score=breadth_score,
                    trend_score=trend_score,
                    recovery_prob=recovery_prob,
                    allows_anomaly=True,
                    state_reason=f"vol_chg={vol_chg:+.4f} contracting + recovery={recovery_prob:.3f} + breadth={breadth_score:.2f}",
                )

        # Vol contracting but recovery_prob too low or breadth too weak
        # This is the 2022-08 case: vol barely contracting, recovery_prob ~0.46
        if recovery_prob < 0.48:
            return MarketStateMachineResult(
                date=trade_date,
                state_type="FALSE_RECOVERY",
                anomaly_weight=0.0,
                vol_score=vol_score,
                breadth_score=breadth_score,
                trend_score=trend_score,
                recovery_prob=recovery_prob,
                allows_anomaly=False,
                state_reason=f"vol contracting but recovery_prob={recovery_prob:.3f} < 0.48, not enough recovery",
            )

        # Default: uncertain, don't bet
        return MarketStateMachineResult(
            date=trade_date,
            state_type="FALSE_RECOVERY",
            anomaly_weight=0.0,
            vol_score=vol_score,
            breadth_score=breadth_score,
            trend_score=trend_score,
            recovery_prob=recovery_prob,
            allows_anomaly=False,
            state_reason=f"uncertain: vol={vol_score:.2f} breadth={breadth_score:.2f} recovery={recovery_prob:.3f}",
        )

    def _compute_vol_score(self, trade_date: str) -> tuple[float, float]:
        """Volatility score: 1 = strongly contracting, 0 = expanding.

        Returns: (vol_score 0-1, vol_change)
        """
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
            return 0.5, 0.0

        closes = [r[0] for r in rows if r[0]]
        returns = np.diff(closes) / closes[:-1]

        vol_20d = np.std(returns[-20:]) * np.sqrt(252) if len(returns) >= 20 else 0.25
        vol_60d = np.std(returns[-60:]) * np.sqrt(252) if len(returns) >= 60 else vol_20d
        vol_change = vol_20d - vol_60d

        # Score: strong contraction (< -0.05) -> 1.0, no change (0) -> 0.5, expansion (>0.03) -> 0.0
        vol_score = max(0, min(1, 0.5 - vol_change * 5))

        return float(vol_score), float(vol_change)

    def _compute_breadth_score(self, trade_date: str) -> float:
        """Breadth score: fraction of stocks with positive momentum.

        6-S.5.2 showed breadth alone can't distinguish true/false recovery,
        but it's still a confirming signal. Low breadth + vol contraction
        = early recovery (ok). Low breadth + no vol contraction = false.
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN momentum_score > 50 THEN 1 ELSE 0 END) "
                "FROM stock_factor_snapshot WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
            total, advancing = row[0], row[1]
            return float(advancing / total) if total > 0 else 0.5
        finally:
            conn.close()

    def _compute_trend_score(self, trade_date: str) -> tuple[float, float]:
        """Trend score: 1 = strong uptrend, 0 = strong downtrend.

        Returns: (trend_score 0-1, raw_trend_value)
        """
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
            return 0.5, 0.0

        closes = [r[0] for r in rows if r[0]]
        current = closes[-1]
        ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else np.mean(closes)

        trend_val = float(np.clip((current - ma60) / ma60, -1, 1)) if ma60 > 0 else 0.0

        trend_score = max(0, min(1, 0.5 + trend_val * 0.5))
        return trend_score, trend_val

    def _compute_recovery_prob(self, trade_date: str) -> float:
        """Reuse MarketRecoveryEngine's composite probability."""
        from src.thesis.market_recovery import MarketRecoveryEngine

        engine = MarketRecoveryEngine(eval_db=self.eval_db, cache_db=self.cache_db)
        state = engine.compute_recovery_probability(trade_date)
        return state.recovery_probability
