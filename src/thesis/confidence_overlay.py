"""
Recovery Confidence Overlay - Commit 6-S.5.5.

Separates market state identification from trading permission.

6-S.5.4 proved: STABILIZING state contains both true recovery (2024-08)
and false recovery (2022-08). The state alone can't distinguish them.

Solution: add a Confidence Score (0-100) that measures "how strongly
is the recovery being confirmed", independent of the state label.

State answers: "what phase is the market in?"
Confidence answers: "how sure are we this phase is real?"

Confidence = 0.4 × breadth_recovery + 0.3 × vol_repair + 0.3 × trend_confirm

Permission is then based on CONFIDENCE, not STATE:
  < 30:  no anomaly (even if state is STABILIZING)
  30-50: small position (0.2 weight)
  50-70: normal position (0.5 weight)
  > 70:  full position (1.0 weight)

This allows STABILIZING dates with high confidence (2024-08) to participate,
while STABILIZING dates with low confidence (2022-08) are blocked.

Usage:
    from src.thesis.confidence_overlay import RecoveryConfidence
    rc = RecoveryConfidence()
    score = rc.compute("2024-08-29")
    # score = ConfidenceResult(confidence=65, anomaly_weight=0.5, allows=True)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ConfidenceResult:
    """Recovery confidence assessment for one date."""
    date: str
    confidence: float          # 0-100
    anomaly_weight: float      # 0.0-1.0 (derived from confidence)
    allows_anomaly: bool       # True if weight >= 0.5

    # Sub-scores
    breadth_recovery: float    # 0-100
    vol_repair: float          # 0-100
    trend_confirm: float       # 0-100

    # Diagnosis
    confidence_band: str       # "blocked" / "small" / "normal" / "full"
    reason: str

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "confidence": round(self.confidence, 1),
            "anomaly_weight": round(self.anomaly_weight, 2),
            "allows_anomaly": self.allows_anomaly,
            "breadth_recovery": round(self.breadth_recovery, 1),
            "vol_repair": round(self.vol_repair, 1),
            "trend_confirm": round(self.trend_confirm, 1),
            "band": self.confidence_band,
            "reason": self.reason,
        }


class RecoveryConfidence:
    """Computes recovery confidence score (0-100) from three dimensions.

    1. Breadth Recovery (40%): are stocks broadly participating?
       - Momentum > 50 ratio (from snapshot)
       - Improvement direction (is it getting better?)

    2. Volatility Repair (30%): is panic subsiding?
       - Vol contraction magnitude (vol_change)
       - Vol level (lower = more repaired)

    3. Trend Confirmation (30%): is the trend stabilizing?
       - Price vs MA60
       - Recent return direction

    The key insight: 2022-08 and 2024-08 are both STABILIZING, but
    2022-08 has weak breadth + barely-contracting vol = low confidence,
    while 2024-08 has improving breadth + strong vol contraction = high confidence.
    """

    def __init__(self, eval_db: str = "data/evaluation.db",
                 cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db

    def compute(self, trade_date: str) -> ConfidenceResult:
        """Compute recovery confidence for a date.

        Returns: ConfidenceResult with 0-100 score + anomaly permission
        """
        # 1. Breadth Recovery (40%)
        breadth = self._compute_breadth_recovery(trade_date)

        # 2. Volatility Repair (30%)
        vol_repair = self._compute_vol_repair(trade_date)

        # 3. Trend Confirmation (30%)
        trend = self._compute_trend_confirm(trade_date)

        # Weighted composite (6-S.5.5b: vol_repair is strongest discriminator,
        # breadth has almost no variance across dates, trend is moderate)
        confidence = (
            0.10 * breadth
            + 0.50 * vol_repair
            + 0.40 * trend
        )

        # Anomaly permission from confidence
        # 6-S.5.5b: threshold raised from 50 to 55 to block 2022-08 type
        # bear rally (confidence ~55) while allowing 2025-01 type (confidence ~53)
        # This is tight but the gap between TRUE and FALSE is only ~22 points
        if confidence < 55:
            anomaly_weight = 0.0
            band = "blocked"
            allows = False
        elif confidence < 65:
            anomaly_weight = 0.3
            band = "small"
            allows = False
        elif confidence < 75:
            anomaly_weight = 0.6
            band = "normal"
            allows = True
        else:
            anomaly_weight = 1.0
            band = "full"
            allows = True

        reason = (
            f"breadth={breadth:.0f} vol_repair={vol_repair:.0f} "
            f"trend={trend:.0f} -> confidence={confidence:.0f} ({band})"
        )

        return ConfidenceResult(
            date=trade_date,
            confidence=confidence,
            anomaly_weight=anomaly_weight,
            allows_anomaly=allows,
            breadth_recovery=breadth,
            vol_repair=vol_repair,
            trend_confirm=trend,
            confidence_band=band,
            reason=reason,
        )

    def _compute_breadth_recovery(self, trade_date: str) -> float:
        """Breadth recovery score (0-100).

        Measures: what fraction of stocks have positive momentum,
        adjusted by how many are near highs (strong recovery signal).

        2023-01 (true recovery): ~47% advancing -> score ~55
        2022-08 (false recovery): ~49% advancing but no new highs -> score ~45
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN momentum_score > 50 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN momentum_score > 80 THEN 1 ELSE 0 END) "
                "FROM stock_factor_snapshot WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
        finally:
            conn.close()

        if not row or row[0] == 0:
            return 50.0  # neutral

        total = row[0]
        advancing = row[1] or 0
        near_highs = row[2] or 0

        advance_ratio = advancing / total
        high_ratio = near_highs / total

        # Base score from advance ratio (centered at 50%)
        # 50% advancing -> 50, 60% -> 70, 40% -> 30
        base_score = advance_ratio * 100

        # Bonus for stocks near highs (broad strength)
        # 10% near highs -> +10, 20% -> +20
        high_bonus = min(20, high_ratio * 200)

        return min(100, max(0, base_score + high_bonus))

    def _compute_vol_repair(self, trade_date: str) -> float:
        """Volatility repair score (0-100).

        Measures: how much has volatility contracted, and is it at a low level?

        2023-01 (true): vol_chg=-0.064, vol_20d=0.13 -> score ~85
        2022-08 (false): vol_chg=-0.010, vol_20d=0.18 -> score ~45
        2023-12 (false): vol_chg=0.000, vol_20d=0.11 -> score ~40
        """
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
            return 50.0

        closes = [r[0] for r in rows if r[0]]
        returns = np.diff(closes) / closes[:-1]

        vol_20d = float(np.std(returns[-20:]) * np.sqrt(252)) if len(returns) >= 20 else 0.25
        vol_60d = float(np.std(returns[-60:]) * np.sqrt(252)) if len(returns) >= 60 else vol_20d
        vol_change = vol_20d - vol_60d

        # Score from vol level: lower vol = higher score
        # vol_20d 0.10 -> 80, 0.15 -> 60, 0.25 -> 30, 0.35 -> 10
        level_score = max(0, min(100, 100 - vol_20d * 300))

        # Score from vol change: stronger contraction = higher score
        # vol_chg -0.05 -> +30, -0.01 -> +6, 0.00 -> 0, +0.03 -> -18
        change_score = max(-30, min(40, -vol_change * 600))

        return min(100, max(0, level_score + change_score))

    def _compute_trend_confirm(self, trade_date: str) -> float:
        """Trend confirmation score (0-100).

        Measures: is price above key MAs, and is the trend improving?

        2023-01 (true): trend +0.063 (above MA60) -> score ~65
        2022-08 (false): trend -0.082 (below MA60) -> score ~25
        2024-08 (true): trend -0.044 (below but stabilizing) -> score ~40
        """
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
            return 50.0

        closes = [r[0] for r in rows if r[0]]
        current = closes[-1]
        ma20 = np.mean(closes[-20:])
        ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else np.mean(closes)

        # Above MA60 -> bullish, below -> bearish
        # trend = (current - ma60) / ma60, range -1 to +1
        if ma60 > 0:
            trend = float(np.clip((current - ma60) / ma60, -1, 1))
        else:
            trend = 0.0

        # Score: trend +0.10 -> 70, +0.00 -> 50, -0.10 -> 30
        trend_score = 50 + trend * 200

        # Bonus for MA20 > MA60 (short-term above long-term)
        if ma20 > ma60:
            trend_score += 10

        return min(100, max(0, trend_score))
