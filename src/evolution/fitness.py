"""
Fitness Calculator - Commit 6-L.7 Phase 3.1.

Unified doctrine fitness computation. Replaces the two drifted copies of the
fitness formula in engine_v1._calculate_fitness and sandbox._calculate_fitness
(DRY violation flagged in 6-L.7 exploration).

New fitness (per user decision):
    fitness = 0.5 * residual_alpha_normalized
            + 0.2 * regime_adaptation
            + 0.15 * drawdown_score
            + 0.1 * capacity_score
            + 0.05 * diversity_bonus

Key design (per user decision):
  * residual_alpha (NOT total_return) - strips market beta + sector so
    evolution doesn't breed high-beta monsters.
  * regime_adaptation is DATA-DRIVEN with Bayesian shrinkage, NOT hand-set
    priors. Formula: adaptation = (historical * n + 0.5 * 20) / (n + 20).
    Prior = 0.5 (neutral). As samples accumulate, becomes data-driven.
  * diversity_bonus prevents winner-takes-all: if a doctrine type is
    overrepresented in the population (>30%), penalize; if rare (<10%), reward.

Usage:
    from src.evolution.fitness import FitnessCalculator
    calc = FitnessCalculator()
    fitness = calc.calculate_doctrine_fitness("deep_value_purist")
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


# Bayesian shrinkage constant (per user decision: prior=0.5, prior_strength=20)
REGIME_PRIOR = 0.5
REGIME_PRIOR_STRENGTH = 20  # equivalent to 20 "pseudo-samples" of prior

# Diversity thresholds
DIVERSITY_OVERREPRESENTED = 0.30  # doctrine type >30% of population -> penalize
DIVERSITY_RARE = 0.10  # doctrine type <10% -> reward


@dataclass
class FitnessResult:
    """Doctrine fitness breakdown."""

    doctrine_id: str
    fitness: float
    residual_alpha_normalized: float
    regime_adaptation: float
    drawdown_score: float
    capacity_score: float
    diversity_bonus: float
    sample_count: int

    def to_dict(self) -> dict:
        return {
            "doctrine_id": self.doctrine_id,
            "fitness": self.fitness,
            "residual_alpha_normalized": self.residual_alpha_normalized,
            "regime_adaptation": self.regime_adaptation,
            "drawdown_score": self.drawdown_score,
            "capacity_score": self.capacity_score,
            "diversity_bonus": self.diversity_bonus,
            "sample_count": self.sample_count,
        }


class FitnessCalculator:
    """Unified doctrine fitness calculator (replaces DRY-violating copies).

    Commit 6-M: Fitness v2 uses Alpha Origin Attribution (origin_quality,
    selection_alpha, factor_independence, timing_quality) instead of raw
    residual_alpha. This prevents evolution from rewarding factor-beta
    disguised as alpha (e.g. momentum doctrine's +11.97% residual was 86%
    factor exposure, only 1.67% true selection skill).

    Fitness v2 formula (per user decision):
      fitness = 0.35×origin_quality + 0.25×selection_alpha_norm
              + 0.15×factor_independence + 0.10×timing_quality
              + 0.10×stability + 0.05×diversity - luck_penalty×0.10
    """

    # Fitness v2 weights (Commit 6-M)
    W_ORIGIN_QUALITY = 0.35
    W_SELECTION_ALPHA = 0.25
    W_FACTOR_INDEPENDENCE = 0.15
    W_TIMING = 0.10
    W_STABILITY = 0.10
    W_DIVERSITY = 0.05
    # luck_penalty is subtracted (max -0.10)

    MIN_SAMPLES = 5  # below this, fitness is uncertain (return low confidence)

    def __init__(self, eval_db: str = "data/evaluation.db"):
        self.eval_db = eval_db

    def calculate_doctrine_fitness(
        self, doctrine_id: str, population_doctrine_counts: dict[str, int] | None = None
    ) -> FitnessResult | None:
        """Compute fitness v2 for a doctrine (Commit 6-M).

        Uses Alpha Origin Attribution: rewards selection_alpha + factor_independence
        + timing, penalizes luck. This prevents factor-beta disguised as alpha
        from dominating evolution.

        Args:
            doctrine_id: the doctrine to evaluate
            population_doctrine_counts: for diversity_bonus

        Returns: FitnessResult, or None if no fitness_history records.
        """
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT residual_alpha, alpha_quality, drawdown, market_regime, "
                "total_return, origin_quality, selection_alpha, "
                "factor_independence, timing_quality, luck_penalty "
                "FROM doctrine_fitness_history WHERE doctrine_id=? "
                "ORDER BY trade_date DESC LIMIT 200",
                (doctrine_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return None

        sample_count = len(rows)

        # 6-M: Alpha Origin components
        origin_qualities = [r["origin_quality"] for r in rows if r["origin_quality"] is not None]
        selection_alphas = [r["selection_alpha"] for r in rows if r["selection_alpha"] is not None]
        factor_indeps = [
            r["factor_independence"] for r in rows if r["factor_independence"] is not None
        ]
        timing_qualities = [r["timing_quality"] for r in rows if r["timing_quality"] is not None]
        luck_penalties = [r["luck_penalty"] for r in rows if r["luck_penalty"] is not None]
        drawdowns = [r["drawdown"] for r in rows if r["drawdown"] is not None]

        # Averages
        avg_origin_quality = float(np.mean(origin_qualities)) if origin_qualities else 0.5
        avg_selection_alpha = float(np.mean(selection_alphas)) if selection_alphas else 0.0
        avg_factor_indep = float(np.mean(factor_indeps)) if factor_indeps else 0.5
        avg_timing = float(np.mean(timing_qualities)) if timing_qualities else 0.5
        avg_luck = float(np.mean(luck_penalties)) if luck_penalties else 0.3

        # Stability (from residual_alpha variance)
        residual_alphas = [r["residual_alpha"] for r in rows if r["residual_alpha"] is not None]
        if len(residual_alphas) >= 3:
            std = float(np.std(residual_alphas))
            stability = max(0.1, min(1.0, 1.0 / (1.0 + std * 15)))
        else:
            stability = 0.5

        # Diversity bonus
        diversity_bonus = self._diversity_bonus(doctrine_id, population_doctrine_counts)

        # Drawdown score (for FitnessResult compatibility)
        avg_drawdown = float(np.mean(drawdowns)) if drawdowns else -0.15
        drawdown_score = max(0.0, min(1.0, 1.0 + avg_drawdown))

        # Fitness v2 (Commit 6-M)
        selection_alpha_norm = self._sigmoid_normalize(avg_selection_alpha)
        fitness_v2 = (
            self.W_ORIGIN_QUALITY * avg_origin_quality
            + self.W_SELECTION_ALPHA * selection_alpha_norm
            + self.W_FACTOR_INDEPENDENCE * avg_factor_indep
            + self.W_TIMING * avg_timing
            + self.W_STABILITY * stability
            + self.W_DIVERSITY * diversity_bonus
            - avg_luck * 0.10  # luck penalty (max -0.10)
        )

        # Commit 6-N.4 + 6-N.2d: Fitness v4 = Origin Alpha × Uniqueness × Survival
        # Instead of subtracting ecology penalty, use multiplicative model:
        #   fitness_v4 = fitness_v2 × uniqueness_factor × survival_factor
        # This is more biologically accurate: a crowded doctrine doesn't just
        # lose some fitness, its entire reproductive potential degrades.
        try:
            from src.evolution.alpha_ecology import AlphaEcology
            from src.evolution.competition import CompetitionEngine

            ecology = AlphaEcology(eval_db=self.eval_db)
            comp = CompetitionEngine(eval_db=self.eval_db)

            # Uniqueness: 1 - crowding (0=crowded, 1=unique)
            conn = sqlite3.connect(self.eval_db)
            try:
                row = conn.execute(
                    "SELECT crowding_score FROM alpha_decay_history "
                    "WHERE doctrine_id=? ORDER BY generation DESC LIMIT 1",
                    (doctrine_id,),
                ).fetchone()
            finally:
                conn.close()
            crowding = row[0] if row and row[0] is not None else 0.0
            uniqueness = max(0.3, 1.0 - crowding)  # floor at 0.3

            # Survival: based on half-life (longer half-life = higher survival)
            half_life = comp.estimate_half_life(doctrine_id)
            if half_life > 0:
                # half_life=10 gen -> survival=0.7, half_life=50 gen -> survival=0.93
                survival = min(0.95, 0.5 + 0.5 * (1 - math.exp(-half_life / 20)))
            else:
                survival = 0.7  # neutral when no half-life data

            # Decay penalty (still subtractive for sharp decay)
            eco_penalty = ecology.ecology_penalty(doctrine_id)

            # Fitness v4: multiplicative + residual decay penalty
            fitness = max(0.0, fitness_v2 * uniqueness * survival - eco_penalty * 0.10)
        except Exception:
            fitness = max(0.0, fitness_v2)

        return FitnessResult(
            doctrine_id=doctrine_id,
            fitness=fitness,
            residual_alpha_normalized=avg_origin_quality,  # now origin_quality
            regime_adaptation=avg_factor_indep,  # repurposed: factor independence
            drawdown_score=drawdown_score,
            capacity_score=stability,  # repurposed: stability
            diversity_bonus=diversity_bonus,
            sample_count=sample_count,
        )

    def _regime_adaptation(self, doctrine_id: str) -> float:
        """Bayesian regime adaptation score.

        adaptation = (historical_score * n + prior * prior_strength) / (n + prior_strength)

        prior = 0.5 (neutral), prior_strength = 20.
        As n (samples) grows, becomes data-driven; with few samples, regresses to 0.5.

        historical_score = average of sigmoid(avg_residual_alpha) across regimes,
        weighted by sample count per regime.
        """
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT regime, sample_count, avg_residual_alpha "
                "FROM doctrine_regime_statistics WHERE doctrine_id=?",
                (doctrine_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return REGIME_PRIOR  # no data -> neutral prior

        # Bayesian-weighted average across regimes
        total_weight = 0.0
        weighted_score = 0.0
        for row in rows:
            n = row["sample_count"] or 0
            avg_alpha = row["avg_residual_alpha"] or 0.0
            # Per-regime Bayesian: (sigmoid(alpha) * n + 0.5 * 20) / (n + 20)
            hist_score = self._sigmoid_normalize(avg_alpha)
            regime_score = (hist_score * n + REGIME_PRIOR * REGIME_PRIOR_STRENGTH) / (
                n + REGIME_PRIOR_STRENGTH
            )
            weighted_score += regime_score * n
            total_weight += n

        if total_weight == 0:
            return REGIME_PRIOR

        return weighted_score / total_weight

    def _diversity_bonus(self, doctrine_id: str, population_counts: dict[str, int] | None) -> float:
        """Penalize overrepresented doctrines, reward rare ones.

        >30% of population -> 0.2 (penalty)
        <10% of population -> 0.8 (reward)
        in between -> linear
        """
        if not population_counts:
            return 0.5  # neutral when no population info

        total = sum(population_counts.values())
        if total == 0:
            return 0.5

        # Count doctrines of the same "type" (same prefix before _gen)
        # e.g. "deep_value_purist" and "deep_aggr_gen1_xxx" share "deep" type
        doctrine_type = doctrine_id.split("_")[0]
        type_count = sum(
            c for did, c in population_counts.items() if did.split("_")[0] == doctrine_type
        )
        type_share = type_count / total

        if type_share > DIVERSITY_OVERREPRESENTED:
            return 0.2  # overrepresented -> penalty
        elif type_share < DIVERSITY_RARE:
            return 0.8  # rare -> reward
        else:
            # Linear interpolation between 0.8 (at 10%) and 0.2 (at 30%)
            return (
                0.8
                - (type_share - DIVERSITY_RARE) / (DIVERSITY_OVERREPRESENTED - DIVERSITY_RARE) * 0.6
            )

    @staticmethod
    def _sigmoid_normalize(x: float, scale: float = 10.0) -> float:
        """Normalize a return (e.g. +0.15 = +15%) to 0-1 via sigmoid.

        +15% -> ~0.82, 0% -> 0.5, -15% -> ~0.18.
        scale=10 means +10% maps to ~0.73 (reasonable sensitivity).
        """
        return 1.0 / (1.0 + np.exp(-x * scale))

    def update_regime_statistics(self, doctrine_id: str) -> int:
        """Recompute doctrine_regime_statistics from doctrine_fitness_history.

        Groups all fitness_history records by market_regime, computes
        avg_residual_alpha + win_rate + count per regime, upserts into
        doctrine_regime_statistics. Called after each SurvivalArena cycle.

        Returns: number of (doctrine, regime) rows updated.
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT market_regime, COUNT(*) as n, "
                "AVG(residual_alpha) as avg_alpha, "
                "AVG(CASE WHEN residual_alpha > 0 THEN 1.0 ELSE 0.0 END) as win_rate "
                "FROM doctrine_fitness_history WHERE doctrine_id=? AND market_regime IS NOT NULL "
                "GROUP BY market_regime",
                (doctrine_id,),
            ).fetchall()

            for row in rows:
                regime, n, avg_alpha, win_rate = row
                conn.execute(
                    """
                    INSERT OR REPLACE INTO doctrine_regime_statistics
                    (doctrine_id, regime, sample_count, avg_residual_alpha, win_rate, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                    (doctrine_id, regime, n, avg_alpha or 0.0, win_rate or 0.0),
                )
            conn.commit()
            return len(rows)
        finally:
            conn.close()
