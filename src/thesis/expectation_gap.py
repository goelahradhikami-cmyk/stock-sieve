"""
Expectation Gap Engine - Commit 6-S.15.2 (v3.3 Phase 2).

Tests the hypothesis: the market underreacts to the SPEED of earnings
improvement. Mispricing occurs when fundamental improvement > price repricing.

This replaces the RS gate (v3.2 Stage 2) which was proved to be a negative
contribution layer (v3.2.2 RS Ablation). RS is ABSORBED here as the
price_reaction input, not used as a gate.

Formula (frozen, v3.3 Design Freeze):
  gap_score = zscore(earnings_acceleration) - zscore(sector_adjusted_t5)

  high gap = earnings improved fast but price hasn't reacted = underreaction
  low gap  = market already priced in the improvement

The gap_score is used for RANKING, not gating. No hard threshold.
KillCriteria and DoctrineUnderwriter remain unchanged (they read core
MispricingObject fields only, never v3_features).

Usage:
    from src.thesis.expectation_gap import ExpectationGapEngine
    engine = ExpectationGapEngine()
    score = engine.compute("600519", "2024-08-29")
    # score.gap_score = 1.35 (high gap = underreaction)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExpectationGapScore:
    """Expectation Gap assessment for one stock at one decision date."""

    security_id: str
    trade_date: str

    # Raw inputs (from earnings_event_reaction table)
    earnings_acceleration: float | None = None  # 1st derivative
    earnings_acceleration_2nd: float | None = None  # 2nd derivative
    price_reaction: float | None = None  # sector_adjusted_t5 (PRIMARY)
    price_reaction_20d: float | None = None  # sector_adjusted_t20
    available_date: str | None = None  # event anchor

    # FRM context (from earnings_event_reaction)
    frm_direction: str | None = None
    earnings_yoy_current: float | None = None
    earnings_yoy_previous: float | None = None

    # Composite (frozen formula)
    gap_score: float | None = None  # z(EA) - z(PR)
    gap_percentile: float | None = None  # 0-1, higher = more underreaction
    confidence: float | None = None  # 0-1, data quality flag

    # Diagnosis
    data_available: bool = True

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class ExpectationGapEngine:
    """6-S.15.2: Expectation Gap Engine.

    Computes how much the market underreacted to earnings improvement.
    Uses the earnings_event_reaction table (6-S.15.1) for price_reaction data.

    The engine does NOT gate - it scores. Candidate ranking uses gap_score.
    This is the opposite of the RS gate which filtered OUT high-alpha stocks.
    """

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db
        # Z-score normalization requires cross-sectional context.
        # We cache the distribution per trade_date (or nearest available).
        self._distribution_cache: dict[str, dict] = {}

    def compute(self, security_id: str, trade_date: str) -> ExpectationGapScore:
        """Compute expectation gap for a stock at a decision date.

        Args:
            security_id: bare stock code
            trade_date: the episode's decision date (ISO)

        Returns: ExpectationGapScore
        """
        security_id = str(security_id).zfill(6)
        result = ExpectationGapScore(
            security_id=security_id,
            trade_date=trade_date,
        )

        # 1. Find the most recent earnings announcement before trade_date
        event_data = self._load_event_reaction(security_id, trade_date)
        if event_data is None:
            result.data_available = False
            result.confidence = 0.0
            return result

        # 2. Extract raw inputs
        result.earnings_acceleration = event_data["earnings_acceleration"]
        result.earnings_acceleration_2nd = event_data.get("earnings_acceleration_2nd")
        result.price_reaction = event_data.get("sector_adjusted_t5")  # PRIMARY
        result.price_reaction_20d = event_data.get("sector_adjusted_t20")
        result.available_date = event_data["available_date"]
        result.frm_direction = event_data.get("frm_direction")
        result.earnings_yoy_current = event_data.get("earnings_yoy_current")
        result.earnings_yoy_previous = event_data.get("earnings_yoy_previous")

        # 3. Need both EA and PR to compute gap
        if result.earnings_acceleration is None or result.price_reaction is None:
            result.data_available = False
            result.confidence = 0.3
            result.gap_score = None
            return result

        # 4. Compute z-scored gap (cross-sectional, within same trade_date universe)
        # The z-scores are computed against the distribution of all stocks
        # that had announcements around the same time.
        z_ea = self._zscore_ea(result.earnings_acceleration, trade_date)
        z_pr = self._zscore_pr(result.price_reaction, trade_date)

        if z_ea is None or z_pr is None:
            # Fallback: use raw difference if no distribution available
            result.gap_score = result.earnings_acceleration - result.price_reaction
            result.confidence = 0.5
        else:
            # FROZEN FORMULA: gap_score = z(EA) - z(PR)
            # High EA (fast improvement) - High PR (market noticed) = low gap
            # High EA (fast improvement) - Low PR (market didn't notice) = high gap
            result.gap_score = float(z_ea - z_pr)
            result.confidence = 0.8

        # 5. Percentile (for decile analysis)
        result.gap_percentile = self._compute_percentile(result.gap_score, trade_date)

        return result

    # ------------------------------------------------------------------
    # Event reaction data loading
    # ------------------------------------------------------------------

    def _load_event_reaction(self, code: str, trade_date: str) -> dict | None:
        """Load the most recent earnings_event_reaction before trade_date.

        Joins earnings_event_reaction with akshare_financials to find the
        announcement closest to (but before) trade_date.
        """
        conn = sqlite3.connect(self.cache_db)
        try:
            # Find the most recent available_date for this stock before trade_date
            row = conn.execute(
                """SELECT eer.*, eer.available_date
                   FROM earnings_event_reaction eer
                   WHERE eer.security_id = ?
                     AND eer.available_date <= ?
                   ORDER BY eer.available_date DESC
                   LIMIT 1""",
                (code, trade_date),
            ).fetchone()

            if row is None:
                # Not in earnings_event_reaction table yet.
                # Fallback: find from akshare_financials directly
                ann = conn.execute(
                    "SELECT available_date FROM akshare_financials "
                    "WHERE code = ? AND available_date IS NOT NULL "
                    "AND available_date <= ? "
                    "ORDER BY available_date DESC LIMIT 1",
                    (code, trade_date),
                ).fetchone()
                if not ann or not ann[0]:
                    return None
                # Compute on the fly using EventReactionCalculator
                from src.thesis.event_reaction import EventReactionCalculator

                calc = EventReactionCalculator(self.cache_db)
                result = calc.compute(code, ann[0])
                return result.to_dict()

            cols = [
                d[0]
                for d in conn.execute("SELECT * FROM earnings_event_reaction LIMIT 0").description
            ]
            return dict(zip(cols, row, strict=False))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Z-score normalization (cross-sectional)
    # ------------------------------------------------------------------

    def _zscore_ea(self, ea: float, trade_date: str) -> float | None:
        """Z-score earnings_acceleration against same-period distribution."""
        dist = self._get_distribution(trade_date)
        if dist is None or dist["ea_mean"] is None:
            return None
        std = dist["ea_std"]
        if std is None or std == 0:
            return 0.0
        return (ea - dist["ea_mean"]) / std

    def _zscore_pr(self, pr: float, trade_date: str) -> float | None:
        """Z-score price_reaction against same-period distribution."""
        dist = self._get_distribution(trade_date)
        if dist is None or dist["pr_mean"] is None:
            return None
        std = dist["pr_std"]
        if std is None or std == 0:
            return 0.0
        return (pr - dist["pr_mean"]) / std

    def _get_distribution(self, trade_date: str) -> dict | None:
        """Get cross-sectional distribution of EA and PR for a trade_date.

        Uses all earnings_event_reaction rows where available_date is within
        90 days before trade_date (typical quarter window).
        """
        if trade_date in self._distribution_cache:
            return self._distribution_cache[trade_date]

        conn = sqlite3.connect(self.cache_db)
        try:
            # Window: announcements in the 90 days before trade_date
            rows = conn.execute(
                """SELECT earnings_acceleration, sector_adjusted_t5
                   FROM earnings_event_reaction
                   WHERE available_date <= ?
                     AND available_date >= date(?, '-90 days')
                     AND earnings_acceleration IS NOT NULL
                     AND sector_adjusted_t5 IS NOT NULL""",
                (trade_date, trade_date),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 5:
            # Insufficient for z-score; return None (fallback to raw)
            self._distribution_cache[trade_date] = None
            return None

        ea_vals = np.array([r[0] for r in rows])
        pr_vals = np.array([r[1] for r in rows])

        dist = {
            "n": len(rows),
            "ea_mean": float(np.mean(ea_vals)),
            "ea_std": float(np.std(ea_vals)),
            "pr_mean": float(np.mean(pr_vals)),
            "pr_std": float(np.std(pr_vals)),
        }
        self._distribution_cache[trade_date] = dist
        return dist

    def _compute_percentile(self, gap_score: float, trade_date: str) -> float | None:
        """Compute percentile of gap_score within the distribution."""
        # For percentile, we need the gap distribution
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                """SELECT earnings_acceleration, sector_adjusted_t5
                   FROM earnings_event_reaction
                   WHERE available_date <= ?
                     AND available_date >= date(?, '-90 days')
                     AND earnings_acceleration IS NOT NULL
                     AND sector_adjusted_t5 IS NOT NULL""",
                (trade_date, trade_date),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 5:
            return None

        dist = self._get_distribution(trade_date)
        if dist is None:
            return None

        # Compute gap for all, then find percentile
        gaps = []
        ea_mean, ea_std = dist["ea_mean"], dist["ea_std"]
        pr_mean, pr_std = dist["pr_mean"], dist["pr_std"]
        for r in rows:
            z_ea = (r[0] - ea_mean) / ea_std if ea_std > 0 else 0
            z_pr = (r[1] - pr_mean) / pr_std if pr_std > 0 else 0
            gaps.append(z_ea - z_pr)

        gaps = np.array(gaps)
        return float(np.mean(gaps <= gap_score))
