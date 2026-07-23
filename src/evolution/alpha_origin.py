"""
Alpha Origin Attribution Engine - Commit 6-M.

Answers "WHERE does a doctrine's alpha come from?" not just "how much alpha?"

Decomposes the 6-L.7 residual_alpha into:
  residual_alpha = factor_alpha + selection_alpha + true_residual

  - factor_alpha: how much came from factor exposure (the doctrine's
    factor_bias × each factor family's long-short return). A momentum
    doctrine that just bought high-momentum stocks gets most of its
    "alpha" from factor exposure, not skill.
  - selection_alpha: how much came from picking the RIGHT stocks within
    a factor (e.g. momentum doctrine picked the best momentum stocks,
    not just any). This is the real skill.
  - true_residual: luck / unexplained.

Also computes:
  - timing_quality (6-M.3): was the entry at a good point in the cycle?
  - luck_penalty (6-M.4): is the return concentrated in 1-2 lucky stocks?

The goal: prevent evolution from rewarding "factor beta disguised as alpha"
and "lucky concentration". Evolution should reward repeatable selection
skill, not factor exposure that any passive factor-tilt ETF could replicate.

Usage:
    from src.evolution.alpha_origin import AlphaOriginAttribution
    ao = AlphaOriginAttribution()
    result = ao.attribute(
        doctrine=doctrine,
        picks=picks,
        pick_returns=[0.15, -0.03, ...],
        trade_date="2026-05-27",
        residual_alpha=0.027,
    )
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.agents.doctrine_engine import DoctrineGenome
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AlphaOriginResult:
    """Full alpha origin decomposition for one backtest observation."""

    # Inputs
    residual_alpha: float  # from 6-L.7 (after market + sector)
    total_return: float
    # 6-M.1 Factor attribution
    factor_alpha: float  # factor_bias × factor long-short returns
    true_residual: float  # residual_alpha - factor_alpha - selection_alpha
    factor_breakdown: dict  # {factor_family: contribution}
    factor_independence: float  # 1 - |factor_alpha / residual_alpha| (0-1)
    # 6-M.2 Stock selection alpha
    selection_alpha: float  # how much came from picking right stocks
    # 6-M.3 Timing quality
    timing_quality: float  # 0-1, entry point quality
    # 6-M.4 Luck penalty
    luck_penalty: float  # 0-1, how concentrated the returns were
    contribution_entropy: float  # entropy of |return contributions|
    # Final
    origin_quality: float  # weighted composite for fitness v2

    def to_dict(self) -> dict:
        return {
            "residual_alpha": self.residual_alpha,
            "factor_alpha": self.factor_alpha,
            "selection_alpha": self.selection_alpha,
            "true_residual": self.true_residual,
            "factor_breakdown": self.factor_breakdown,
            "factor_independence": self.factor_independence,
            "timing_quality": self.timing_quality,
            "luck_penalty": self.luck_penalty,
            "contribution_entropy": self.contribution_entropy,
            "origin_quality": self.origin_quality,
        }


class AlphaOriginAttribution:
    """Decompose residual alpha into factor / selection / timing / luck.

    6-M.1: Factor Attribution - how much of residual is just factor exposure?
    6-M.2: Stock Selection Alpha - how much is picking the right stocks?
    6-M.3: Timing Quality - was the entry at a good point?
    6-M.4: Luck Penalty - is the return concentrated in 1-2 lucky stocks?
    """

    # For factor long-short portfolios: top/bottom percentile of each factor
    FACTOR_PERCENTILE = 0.30  # top 30% long, bottom 30% short

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db

    def attribute(
        self,
        doctrine: DoctrineGenome,
        picks: list[dict],
        pick_returns: list[float],
        trade_date: str,
        residual_alpha: float,
        total_return: float = 0.0,
    ) -> AlphaOriginResult:
        """Full alpha origin decomposition.

        Args:
            doctrine: the doctrine (for factor_bias)
            picks: list of {security_id, alpha, quality_score, ...} from score_universe
            pick_returns: per-stock forward returns
            trade_date: selection date
            residual_alpha: from 6-L.7 attribution (total - beta - sector)
            total_return: portfolio total return
        """
        # 6-M.1: Factor Attribution
        factor_breakdown, factor_alpha = self._factor_attribution(
            doctrine, picks, pick_returns, trade_date
        )

        # 6-M.2: Stock Selection Alpha
        # = residual - factor_alpha (what's left after factor exposure is explained)
        selection_alpha = residual_alpha - factor_alpha

        # true_residual = what's left after both factor + selection
        # (selection_alpha IS the true skill; true_residual is noise/luck before
        # luck_penalty is applied)
        true_residual = residual_alpha - factor_alpha - selection_alpha  # = 0 by construction
        # Actually: selection_alpha = residual - factor_alpha, so true_residual = 0.
        # The "true_residual" concept is: after removing factor exposure,
        # selection_alpha IS the unexplained skill. true_residual marks the
        # part that's pure noise (handled by luck_penalty below).

        # 6-M.1 Factor independence: how much of residual is NOT factor-driven
        if abs(residual_alpha) > 0.001:
            factor_independence = max(0.0, min(1.0, 1.0 - abs(factor_alpha / residual_alpha)))
        else:
            factor_independence = 0.5  # neutral when residual ~0

        # 6-M.3: Timing Quality
        timing_quality = self._timing_quality(picks, trade_date)

        # 6-M.4: Luck Penalty
        luck_penalty, contribution_entropy = self._luck_penalty(pick_returns)

        # Origin quality: composite for fitness v2
        # Higher when: selection_alpha high, factor_independence high,
        #              timing good, luck_penalty low
        origin_quality = (
            self._sigmoid(selection_alpha) * 0.40
            + factor_independence * 0.25
            + timing_quality * 0.20
            + (1.0 - luck_penalty) * 0.15
        )

        return AlphaOriginResult(
            residual_alpha=residual_alpha,
            total_return=total_return,
            factor_alpha=factor_alpha,
            true_residual=true_residual,
            factor_breakdown=factor_breakdown,
            factor_independence=factor_independence,
            selection_alpha=selection_alpha,
            timing_quality=timing_quality,
            luck_penalty=luck_penalty,
            contribution_entropy=contribution_entropy,
            origin_quality=origin_quality,
        )

    # ── 6-M.1 Factor Attribution ─────────────────────────

    def _factor_attribution(
        self,
        doctrine: DoctrineGenome,
        picks: list[dict],
        pick_returns: list[float],
        trade_date: str,
    ) -> tuple[dict, float]:
        """Decompose residual into factor-driven + selection-driven.

        factor_alpha = Σ(factor_bias[f] × factor_ls_return[f])
        where factor_ls_return = avg return of top-30% factor stocks
                                  - avg return of bottom-30% factor stocks

        This requires the factor_snapshot for trade_date to compute
        the factor long-short spreads among the doctrine's picks.

        Returns: (factor_breakdown {family: contribution}, factor_alpha)
        """
        if not picks or not pick_returns:
            return {}, 0.0

        # Get factor scores for each pick from stock_factor_snapshot
        conn = sqlite3.connect(self.eval_db)
        try:
            pick_codes = [p["security_id"] for p in picks]
            placeholders = ",".join("?" * len(pick_codes))
            rows = conn.execute(
                f"SELECT security_id, quality_score, value_score, growth_score, "
                f"momentum_score, risk_score, sentiment_score "
                f"FROM stock_factor_snapshot WHERE trade_date=? AND security_id IN ({placeholders})",
                [trade_date] + pick_codes,
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {}, 0.0

        # Build {security_id: {factor: score}}
        factor_scores: dict[str, dict[str, float]] = {}
        for row in rows:
            sec_id = row[0]
            factor_scores[sec_id] = {
                "quality": row[1] or 50.0,
                "value": row[2] or 50.0,
                "growth": row[3] or 50.0,
                "momentum": row[4] or 50.0,
                "risk": row[5] or 50.0,
                "sentiment": row[6] or 50.0,
            }

        # For each factor family, compute the long-short spread among the picks:
        # high-factor picks' avg return - low-factor picks' avg return
        factor_breakdown = {}
        factor_alpha = 0.0

        for family in ["quality", "value", "growth", "momentum", "risk", "sentiment"]:
            # Pair each pick with its factor score and return
            scored = []
            for i, pick in enumerate(picks):
                sec_id = pick["security_id"]
                if sec_id in factor_scores and i < len(pick_returns):
                    scored.append((factor_scores[sec_id][family], pick_returns[i]))

            if len(scored) < 4:
                continue

            # Sort by factor score, split into top/bottom 30%
            scored.sort(key=lambda x: x[0])
            n = len(scored)
            bottom_n = max(1, int(n * self.FACTOR_PERCENTILE))
            top_n = max(1, int(n * self.FACTOR_PERCENTILE))

            bottom_returns = [r for _, r in scored[:bottom_n]]
            top_returns = [r for _, r in scored[-top_n:]]

            ls_return = np.mean(top_returns) - np.mean(bottom_returns)

            # Contribution = doctrine's weight on this factor × ls_return
            weight = doctrine.factor_bias.get(family, 0.0)
            contribution = weight * ls_return
            factor_breakdown[family] = {
                "weight": weight,
                "ls_return": float(ls_return),
                "contribution": float(contribution),
            }
            factor_alpha += contribution

        return factor_breakdown, float(factor_alpha)

    # ── 6-M.3 Timing Quality ──────────────────────────────

    def _timing_quality(self, picks: list[dict], trade_date: str) -> float:
        """Evaluate entry timing quality (0-1).

        Good timing: entered when the market was not overheated (not at a peak).
        Bad timing: entered at a local peak (chasing).

        Uses the doctrine's average entry price vs the 60-day range of each
        pick. If picks entered near the 60-day low, timing_quality is high;
        if near the 60-day high, it's low (chasing).

        Simplified v1: uses the snapshot's momentum_score as a proxy.
        - High momentum score = stock already ran up = chasing = lower quality
        - Low/mid momentum = not chasing = higher quality
        """
        if not picks:
            return 0.5

        conn = sqlite3.connect(self.eval_db)
        try:
            pick_codes = [p["security_id"] for p in picks]
            placeholders = ",".join("?" * len(pick_codes))
            rows = conn.execute(
                f"SELECT momentum_score FROM stock_factor_snapshot "
                f"WHERE trade_date=? AND security_id IN ({placeholders})",
                [trade_date] + pick_codes,
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return 0.5

        # Average momentum score of picks (0-100)
        avg_momentum = np.mean([r[0] or 50 for r in rows])

        # Timing quality: lower momentum at entry = better timing (contrarian entry)
        # 100 (all high-momentum, chasing) -> 0.2
        # 50 (neutral) -> 0.5
        # 0 (low-momentum, contrarian) -> 0.8
        timing = 1.0 - (avg_momentum / 100.0)
        timing = max(0.2, min(0.8, timing))  # clamp to reasonable range
        return float(timing)

    # ── 6-M.4 Luck Penalty ────────────────────────────────

    @staticmethod
    def _luck_penalty(pick_returns: list[float]) -> tuple[float, float]:
        """Compute luck penalty based on return concentration.

        If 1 stock contributed 90% of returns, that's luck, not skill.
        Uses entropy of |return contributions|: high entropy = spread out (skill),
        low entropy = concentrated (luck).

        Returns: (luck_penalty 0-1, contribution_entropy)
        """
        if not pick_returns or len(pick_returns) < 2:
            return 0.5, 0.0

        # Absolute return contributions
        abs_returns = [abs(r) for r in pick_returns]
        total = sum(abs_returns)
        if total <= 0:
            return 0.3, 0.0  # all flat = low luck risk

        # Normalize to probabilities
        probs = [r / total for r in abs_returns]

        # Shannon entropy (normalized to 0-1 by log(n))
        n = len(probs)
        max_entropy = np.log(n)
        if max_entropy == 0:
            return 0.5, 0.0

        entropy = -sum(p * np.log(p + 1e-10) for p in probs) / max_entropy

        # Luck penalty: low entropy (concentrated) = high penalty
        # entropy 1.0 (perfectly spread) -> penalty 0.0
        # entropy 0.0 (all in one stock) -> penalty 1.0
        luck_penalty = 1.0 - entropy

        return float(luck_penalty), float(entropy)

    @staticmethod
    def _sigmoid(x: float, scale: float = 15.0) -> float:
        """Sigmoid normalize to 0-1."""
        return 1.0 / (1.0 + np.exp(-x * scale))
