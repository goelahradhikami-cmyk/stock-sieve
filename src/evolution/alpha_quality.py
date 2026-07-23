"""
Alpha Quality Calculator - Commit 6-L.8.

Filters out "fake alpha" that residual_alpha alone would let through. A
doctrine can show +5% residual from one lucky small-cap spike, but if that
return came from a single stock (low breadth), is unstable across windows
(low stability), or can't scale (low capacity), it's not real skill.

Formula:
    alpha_quality = residual_alpha × stability × breadth × capacity

Each multiplier is 0.0-1.0, so alpha_quality is always <= residual_alpha
in absolute terms. A doctrine needs ALL FOUR to score high:
  - residual_alpha: true edge after beta + sector (from 6-L.7 attribution)
  - stability: consistent across multiple evaluation windows
  - breadth: return spread across picks, not one stock carrying all
  - capacity: enough picks + liquid stocks to actually deploy capital

Why this matters (per user review): without this, evolution breeds on a
fitness function that still has holes. "自然选择需要一个可信的适应度函数,
不是先让生物进化再发现评分系统有问题。"

Usage:
    from src.evolution.alpha_quality import AlphaQualityCalculator
    calc = AlphaQualityCalculator()
    quality = calc.calculate(residual_alpha=0.015, pick_returns=[...], n_picks=20)
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


class AlphaQualityCalculator:
    """Compute alpha_quality = residual × stability × breadth × capacity."""

    def __init__(self, eval_db: str = "data/evaluation.db"):
        self.eval_db = eval_db

    def calculate(
        self,
        residual_alpha: float,
        pick_returns: list[float] | None = None,
        n_picks: int = 0,
        doctrine_id: str | None = None,
    ) -> float:
        """Compute alpha quality for a single backtest observation.

        Args:
            residual_alpha: the residual after attribution (e.g. 0.015 = +1.5%)
            pick_returns: per-stock returns for this observation (for breadth)
            n_picks: number of stocks picked
            doctrine_id: if provided, uses historical records for stability

        Returns: alpha_quality (same units as residual_alpha, but attenuated)
        """
        # 1. Stability: consistency across this doctrine's historical residuals.
        # If only 1 observation, stability = 0.5 (neutral, uncertain).
        stability = self._stability_score(doctrine_id) if doctrine_id else 0.5

        # 2. Breadth: effective number of positions (Herfindahl-based).
        # 1 stock carrying everything -> breadth ≈ 0; evenly spread -> ≈ 1.
        breadth = self._breadth_score(pick_returns)

        # 3. Capacity: enough picks to be meaningful (not a 2-stock fluke).
        capacity = self._capacity_score(n_picks)

        quality = residual_alpha * stability * breadth * capacity
        return quality

    def calculate_doctrine_quality(self, doctrine_id: str) -> dict | None:
        """Compute average alpha_quality for a doctrine across all history.

        Returns: {avg_quality, avg_residual, stability, breadth, capacity, n}
        """
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT residual_alpha, pick_returns_json, alpha_quality "
                "FROM doctrine_fitness_history WHERE doctrine_id=? "
                "ORDER BY trade_date DESC LIMIT 200",
                (doctrine_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return None

        qualities = []
        residuals = []
        breadths = []
        for row in rows:
            if row["alpha_quality"] is not None:
                qualities.append(row["alpha_quality"])
            if row["residual_alpha"] is not None:
                residuals.append(row["residual_alpha"])
            # Reconstruct breadth from pick_returns_json
            if row["pick_returns_json"]:
                try:
                    returns = json.loads(row["pick_returns_json"])
                    breadths.append(self._breadth_score(returns))
                except (json.JSONDecodeError, TypeError):
                    pass

        n = len(rows)
        return {
            "avg_quality": float(np.mean(qualities)) if qualities else 0.0,
            "avg_residual": float(np.mean(residuals)) if residuals else 0.0,
            "stability": self._stability_score(doctrine_id),
            "avg_breadth": float(np.mean(breadths)) if breadths else 0.5,
            "capacity": self._capacity_score(n),  # n observations = reliability
            "n": n,
        }

    def _stability_score(self, doctrine_id: str) -> float:
        """Stability: 1 - normalized_std of historical residual_alphas.

        High std (erratic) -> low stability. Consistent -> high.
        With <3 samples, returns 0.5 (neutral - not enough to judge).
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT residual_alpha FROM doctrine_fitness_history "
                "WHERE doctrine_id=? AND residual_alpha IS NOT NULL "
                "ORDER BY trade_date",
                (doctrine_id,),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 3:
            return 0.5  # insufficient data, neutral

        alphas = [r[0] for r in rows if r[0] is not None]
        if not alphas:
            return 0.5

        std = float(np.std(alphas))
        # Normalize: std of 0 -> stability 1.0; std of 0.10 (10%) -> stability 0.0
        # Use a soft sigmoid so reasonable alphas aren't over-penalized.
        stability = 1.0 / (1.0 + std * 15)  # 15 = sensitivity
        return max(0.1, min(1.0, stability))

    @staticmethod
    def _breadth_score(pick_returns: list[float] | None) -> float:
        """Breadth: effective number of positions via inverse Herfindahl.

        If one stock dominates returns, effective_N ≈ 1 -> breadth ≈ 0.
        If returns are evenly spread across 20 stocks, effective_N ≈ 20 ->
        breadth ≈ 1.

        Uses absolute return contributions (not signed) so a stock that
        contributed -5% still counts as "contributing" (it was a real pick,
        not a free-rider on one winner).
        """
        if not pick_returns or len(pick_returns) < 2:
            return 0.3  # single stock = low breadth

        # Weight = |return| (absolute contribution)
        abs_returns = [abs(r) for r in pick_returns]
        total = sum(abs_returns)
        if total <= 0:
            # All returns ~0 - technically infinite breadth but meaningless.
            return 0.5

        weights = [r / total for r in abs_returns]
        herfindahl = sum(w * w for w in weights)

        # Effective N = 1 / H. Normalize: effective_N / N_picks -> 0-1.
        effective_n = 1.0 / herfindahl if herfindahl > 0 else len(pick_returns)
        breadth = effective_n / len(pick_returns)

        # If one stock is 90% of |returns|, effective_n ≈ 1.1, breadth ≈ 0.05.
        # If perfectly even, effective_n = N, breadth = 1.0.
        return max(0.0, min(1.0, breadth))

    @staticmethod
    def _capacity_score(n_picks: int) -> float:
        """Capacity: enough picks to be deployable, not a 2-stock fluke.

        <5 picks -> 0.3 (too few, likely a fluke)
        10+ picks -> 1.0 (enough for a real portfolio)
        Linear in between.
        """
        if n_picks >= 10:
            return 1.0
        if n_picks <= 0:
            return 0.0
        return 0.3 + 0.7 * (n_picks / 10.0)
