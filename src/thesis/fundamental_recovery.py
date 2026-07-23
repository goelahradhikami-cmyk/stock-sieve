"""
Fundamental Recovery Momentum (FRM) Scorer - Commit 6-S.12.2.

Answers: is the business itself starting to recover?

Uses earnings_yoy change as a revision proxy. This is NOT analyst consensus
revision - it is a purely internal, offline-first signal derived from
vintage-aware financial reports. The rationale:

  1. External analyst forecast data would break offline-first replay.
  2. A-share analyst coverage has severe survivorship bias (small caps,
     cyclicals, theme stocks have no coverage), so 'no revision' would
     conflate 'no improvement' with 'no analyst'.
  3. The earnings_yoy trend reversal (negative -> less negative -> positive)
     is a genuine fundamental recovery signal that does not require
     external prediction data.

FRM Score = 0.50 * earnings_acceleration
          + 0.30 * margin_stabilization
          + 0.20 * revenue_acceleration

Market-state weighting (recovery-phase amplification):
  EARLY_RECOVERY    1.50  (market doesn't know yet, FRM most valuable)
  STABILIZING       1.00
  CONFIRMED_RECOVERY 0.60 (market already pricing it in)
  other             0.50

Usage:
    from src.thesis.fundamental_recovery import FundamentalRecoveryScorer
    scorer = FundamentalRecoveryScorer()
    result = scorer.compute("600519", "2024-08-29", "EARLY_RECOVERY")
    # result = FRMResult(score=68.5, direction='improving', ...)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class FRMResult:
    """Fundamental Recovery Momentum assessment for one stock."""

    code: str
    as_of_date: str
    market_state: str

    # Subscores (0-100)
    earnings_acceleration: float = 50.0
    margin_stabilization: float = 50.0
    revenue_acceleration: float = 50.0

    # Raw signals
    earnings_yoy_current: float | None = None
    earnings_yoy_previous: float | None = None
    revenue_yoy_current: float | None = None
    revenue_yoy_previous: float | None = None
    margin_current: float | None = None
    margin_previous: float | None = None

    # Diagnosis
    revision_direction: str = "unknown"  # improving / stable / deteriorating
    market_state_weight: float = 1.0

    # Composite
    score: float = 50.0  # 0-100, weighted + market-state amplified

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "as_of_date": self.as_of_date,
            "market_state": self.market_state,
            "earnings_acceleration": round(self.earnings_acceleration, 1),
            "margin_stabilization": round(self.margin_stabilization, 1),
            "revenue_acceleration": round(self.revenue_acceleration, 1),
            "earnings_yoy_current": self.earnings_yoy_current,
            "earnings_yoy_previous": self.earnings_yoy_previous,
            "revenue_yoy_current": self.revenue_yoy_current,
            "revenue_yoy_previous": self.revenue_yoy_previous,
            "revision_direction": self.revision_direction,
            "market_state_weight": self.market_state_weight,
            "score": round(self.score, 1),
        }


# Market-state amplification weights. Earlier recovery phases get higher
# weight because FRM is most valuable when the market has not yet priced
# in the fundamental improvement.
MARKET_STATE_WEIGHTS = {
    "EARLY_RECOVERY": 1.50,
    "STABILIZING": 1.00,
    "CONFIRMED_RECOVERY": 0.60,
    "PANIC": 0.50,
    "EUPHORIA": 0.50,
    "unknown": 0.50,
}

# Revision direction thresholds. We require a meaningful change, not just
# noise. 2 percentage points is the minimum to count as improvement/deterioration.
REVISION_THRESHOLD = 0.02


class FundamentalRecoveryScorer:
    """6-S.12.2: Fundamental Recovery Momentum (FRM) Score.

    Uses earnings_yoy change as a revision proxy. Offline-first, no external
    analyst data. Vintage-aware (respects report availability dates).
    """

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db

    def compute(self, code: str, as_of_date: str, market_state: str = "unknown") -> FRMResult:
        """Compute FRM score for a stock at a given date.

        Args:
            code: bare stock code (e.g. "600519")
            as_of_date: ISO date (e.g. "2024-08-29")
            market_state: one of PANIC/STABILIZING/EARLY_RECOVERY/
                          CONFIRMED_RECOVERY/EUPHORIA/unknown

        Returns: FRMResult with score 0-100 + subscores + raw signals
        """
        code = str(code).zfill(6)
        result = FRMResult(code=code, as_of_date=as_of_date, market_state=market_state)
        result.market_state_weight = MARKET_STATE_WEIGHTS.get(market_state, 0.50)

        # Load vintage-aware last 2 reporting periods
        periods = self._load_vintage_periods(code, as_of_date, limit=2)
        if len(periods) < 2:
            # Insufficient history - return neutral score
            result.score = 50.0 * result.market_state_weight
            result.score = min(100, max(0, result.score))
            return result

        current = periods[0]
        previous = periods[1]

        # Extract signals
        result.earnings_yoy_current = current.get("earnings_yoy")
        result.earnings_yoy_previous = previous.get("earnings_yoy")
        result.revenue_yoy_current = current.get("revenue_yoy")
        result.revenue_yoy_previous = previous.get("revenue_yoy")
        result.margin_current = current.get("net_margin")
        result.margin_previous = previous.get("net_margin")

        # Layer 1: earnings acceleration (50%)
        result.earnings_acceleration = self._score_earnings_acceleration(
            result.earnings_yoy_current, result.earnings_yoy_previous
        )

        # Layer 2: margin stabilization (30%)
        result.margin_stabilization = self._score_margin_stabilization(
            result.margin_current, result.margin_previous
        )

        # Layer 3: revenue acceleration (20%)
        result.revenue_acceleration = self._score_revenue_acceleration(
            result.revenue_yoy_current, result.revenue_yoy_previous
        )

        # Revision direction (for audit / shadow_candidates column)
        result.revision_direction = self._classify_revision(
            result.earnings_yoy_current, result.earnings_yoy_previous
        )

        # Composite: weighted sum, then market-state amplified, clamped 0-100
        base = (
            0.50 * result.earnings_acceleration
            + 0.30 * result.margin_stabilization
            + 0.20 * result.revenue_acceleration
        )
        # Market-state amplification: shifts the score away from 50 (neutral)
        # in the direction of the base signal. A strong improving signal in
        # EARLY_RECOVERY gets amplified; in CONFIRMED_RECOVERY it is dampened.
        amplified = 50.0 + (base - 50.0) * result.market_state_weight
        result.score = float(min(100.0, max(0.0, amplified)))
        return result

    # ------------------------------------------------------------------
    # Vintage-aware period loading
    # ------------------------------------------------------------------

    def _load_vintage_periods(self, code: str, as_of_date: str, limit: int = 2) -> list[dict]:
        """Load the most recent `limit` reporting periods available as of date.

        Vintage-aware: only uses reports whose available_date <= as_of_date.
        Falls back to report_date + 90 days if available_date is NULL.
        """
        conn = sqlite3.connect(self.cache_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT code, report_date, available_date, earnings_yoy, "
                "revenue_yoy, net_profit, revenue, roe, total_assets "
                "FROM akshare_financials WHERE code = ? "
                "AND (available_date IS NOT NULL AND available_date <= ? "
                "     OR available_date IS NULL AND date(report_date, '+90 days') <= ?) "
                "ORDER BY report_date DESC LIMIT ?",
                (code, as_of_date, as_of_date, limit),
            ).fetchall()
        finally:
            conn.close()

        periods = []
        for r in rows:
            net_profit = r["net_profit"]
            revenue = r["revenue"]
            net_margin = None
            if net_profit is not None and revenue and revenue != 0:
                net_margin = net_profit / revenue
            periods.append(
                {
                    "report_date": r["report_date"],
                    "earnings_yoy": (r["earnings_yoy"] / 100.0)
                    if r["earnings_yoy"] is not None
                    else None,
                    "revenue_yoy": (r["revenue_yoy"] / 100.0)
                    if r["revenue_yoy"] is not None
                    else None,
                    "net_margin": net_margin,
                    "roe": r["roe"],
                }
            )
        return periods

    # ------------------------------------------------------------------
    # Subscore computations
    # ------------------------------------------------------------------

    def _score_earnings_acceleration(self, current: float | None, previous: float | None) -> float:
        """Score earnings_yoy change (50% weight).

        revision = current_earnings_yoy - previous_earnings_yoy
        Positive revision = improvement (earnings trend reversing up).

        Scoring (0-100, 50 = neutral):
          Large improvement (revision > +10pp):  90
          Moderate improvement (+2 to +10pp):    70
          Small improvement (+0.5 to +2pp):      60
          Stable (-0.5 to +0.5pp):               50
          Small deterioration (-2 to -0.5pp):    40
          Moderate deterioration (-10 to -2pp):  30
          Large deterioration (< -10pp):         10
        """
        if current is None or previous is None:
            return 50.0
        revision = current - previous
        if revision > 0.10:
            return 90.0
        elif revision > 0.02:
            return 70.0
        elif revision > 0.005:
            return 60.0
        elif revision > -0.005:
            return 50.0
        elif revision > -0.02:
            return 40.0
        elif revision > -0.10:
            return 30.0
        else:
            return 10.0

    def _score_margin_stabilization(self, current: float | None, previous: float | None) -> float:
        """Score margin change (30% weight).

        Margin expansion = positive; margin contraction = negative.
        Stabilization (small change) is neutral-to-slightly-positive in
        a recovery context (stops bleeding).
        """
        if current is None or previous is None:
            return 50.0
        delta = current - previous
        if delta > 0.03:
            return 85.0
        elif delta > 0.01:
            return 70.0
        elif delta > 0:
            return 60.0
        elif delta > -0.01:
            return 50.0
        elif delta > -0.03:
            return 35.0
        else:
            return 20.0

    def _score_revenue_acceleration(self, current: float | None, previous: float | None) -> float:
        """Score revenue_yoy change (20% weight)."""
        if current is None or previous is None:
            return 50.0
        revision = current - previous
        if revision > 0.05:
            return 85.0
        elif revision > 0.02:
            return 70.0
        elif revision > 0:
            return 58.0
        elif revision > -0.02:
            return 50.0
        elif revision > -0.05:
            return 35.0
        else:
            return 20.0

    def _classify_revision(self, current: float | None, previous: float | None) -> str:
        """Classify the earnings revision direction.

        improving:     revision > +REVISION_THRESHOLD (meaningful improvement)
        deteriorating: revision < -REVISION_THRESHOLD (meaningful deterioration)
        stable:        |revision| <= REVISION_THRESHOLD
        """
        if current is None or previous is None:
            return "unknown"
        revision = current - previous
        if revision > REVISION_THRESHOLD:
            return "improving"
        elif revision < -REVISION_THRESHOLD:
            return "deteriorating"
        else:
            return "stable"
