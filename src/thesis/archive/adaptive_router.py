"""
ARCHIVED 2026-07-27 — zero production callers; see src/thesis/archive/__init__.py.

Adaptive Doctrine Router - Commit 6-Q.5b.

Replaces single-doctrine weight multiplier with multi-doctrine softmax
confidence allocation. The router doesn't adjust one doctrine's weights -
it allocates capital across MULTIPLE doctrines based on factor climate.

Architecture:
  1. Factor Momentum Engine computes factor climate (which factors are hot)
  2. Each doctrine's confidence = Σ(factor_bias × factor_momentum)
  3. Softmax(confidence) -> allocation across doctrines
  4. Final portfolio = weighted blend of each doctrine's Top 20

Example:
  Factor climate: value momentum +6%, growth momentum -2%, momentum -3%

  value_purist (bias: value 0.35, quality 0.35):
    confidence = 0.35×0.06 + 0.35×0.02 = 0.028  -> high
  growth_hunter (bias: growth 0.45, momentum 0.15):
    confidence = 0.45×(-0.02) + 0.15×(-0.03) = -0.013 -> low

  Softmax -> value_purist 35%, growth_hunter 15%, quant_nerd 30%, ...

  Final portfolio = 35% × value_top20 + 15% × growth_top20 + ...

This is more stable than multiplier (softmax normalizes naturally) and
matches how real fund-of-funds allocate across managers.

Usage:
    from src.thesis.adaptive_router import AdaptiveRouter
    router = AdaptiveRouter()
    result = router.allocate(
        doctrines=[doctrine_a, doctrine_b, ...],
        trade_date="2026-05-27",
    )
    # result = {doctrine_id: allocation_weight}
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.agents.doctrine_engine import DoctrineGenome
from src.factors.snapshot_builder import FactorSnapshotBuilder
from src.thesis.factor_momentum import FactorClimate, FactorMomentumEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RouterDecision:
    """One routing decision for a date."""

    trade_date: str
    market_regime: str
    doctrine_allocations: dict[str, float]  # {doctrine_id: weight 0-1}
    doctrine_confidences: dict[str, float]  # {doctrine_id: raw confidence}
    factor_climate_summary: dict  # {factor: momentum_60d}
    strongest_factor: str
    weakest_factor: str
    # For memory (6-Q.5d)
    decision_reason: str = ""


class AdaptiveRouter:
    """Allocates capital across doctrines based on factor climate.

    Uses softmax confidence: doctrines whose factor biases align with
    current factor momentum get higher allocation. This is more stable
    than per-stock weight multipliers (6-Q.3/Q.4) because:
      1. Softmax normalizes naturally (no extreme weights)
      2. Allocation is at doctrine level (coarse, robust)
      3. Factor momentum is real time-series L-S return (not dispersion)
    """

    # Softmax temperature: higher = more uniform, lower = more concentrated
    TEMPERATURE = 15.0

    # Which momentum window to use for allocation
    MOMENTUM_WINDOW = "momentum_60d"

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db
        self.fme = FactorMomentumEngine(eval_db=eval_db, cache_db=cache_db)

    def allocate(self, doctrines: list[DoctrineGenome], trade_date: str) -> RouterDecision:
        """Compute doctrine allocation for a date.

        Args:
            doctrines: list of doctrines to allocate across
            trade_date: current date

        Returns: RouterDecision with allocations + confidences
        """
        # 1. Compute factor climate
        climate = self.fme.compute_factor_climate(trade_date)

        # 2. Compute each doctrine's confidence
        confidences = {}
        for doctrine in doctrines:
            conf = self._compute_confidence(doctrine, climate)
            confidences[doctrine.doctrine_id] = conf

        # 3. Softmax -> allocations
        conf_values = np.array(list(confidences.values()))
        # Center around mean for numerical stability
        conf_centered = conf_values - np.mean(conf_values)
        # Softmax with temperature
        exp_vals = np.exp(conf_centered * self.TEMPERATURE)
        softmax = exp_vals / np.sum(exp_vals)

        allocations = {
            doctrine_id: float(softmax[i]) for i, doctrine_id in enumerate(confidences.keys())
        }

        # 4. Build climate summary
        climate_summary = {
            f: climate.factors.get(f, {}).get(self.MOMENTUM_WINDOW, 0)
            for f in FactorMomentumEngine.FACTOR_FAMILIES
        }

        # 5. Decision reason
        strongest = climate.get_strongest_factor(self.MOMENTUM_WINDOW)
        weakest = climate.get_weakest_factor(self.MOMENTUM_WINDOW)
        top_doctrine = max(allocations, key=allocations.get)

        reason = (
            f"regime={climate.market_regime}, "
            f"strongest={strongest}, weakest={weakest}, "
            f"top_doctrine={top_doctrine}({allocations[top_doctrine]:.0%})"
        )

        return RouterDecision(
            trade_date=trade_date,
            market_regime=climate.market_regime,
            doctrine_allocations=allocations,
            doctrine_confidences=confidences,
            factor_climate_summary=climate_summary,
            strongest_factor=strongest,
            weakest_factor=weakest,
            decision_reason=reason,
        )

    def _compute_confidence(self, doctrine: DoctrineGenome, climate: FactorClimate) -> float:
        """Compute a doctrine's confidence based on factor alignment.

        confidence = Σ(factor_bias[f] × factor_momentum[f])

        If the doctrine is tilted toward factors that are currently
        trending up (positive L-S return), its confidence is high.
        """
        bias = doctrine.factor_bias
        total_conf = 0.0

        for factor in FactorMomentumEngine.FACTOR_FAMILIES:
            weight = bias.get(factor, 0.0)
            momentum = climate.factors.get(factor, {}).get(self.MOMENTUM_WINDOW, 0.0)
            total_conf += weight * momentum

        return total_conf

    def build_portfolio(
        self, doctrines: list[DoctrineGenome], decision: RouterDecision, top_n: int = 20
    ) -> dict[str, float]:
        """Build blended portfolio from doctrine allocations.

        Returns: {security_id: weight} blended across doctrines.
        """
        builder = FactorSnapshotBuilder()
        blended: dict[str, float] = {}

        for doctrine in doctrines:
            alloc = decision.doctrine_allocations.get(doctrine.doctrine_id, 0)
            if alloc < 0.01:  # skip very small allocations
                continue

            picks = builder.score_universe(decision.trade_date, doctrine.factor_bias, top_n=top_n)
            if not picks:
                continue

            # Equal weight within doctrine, scaled by allocation
            per_stock_weight = alloc / len(picks)
            for pick in picks:
                sec_id = pick["security_id"]
                blended[sec_id] = blended.get(sec_id, 0) + per_stock_weight

        # Normalize to sum to 1.0
        total = sum(blended.values())
        if total > 0:
            blended = {k: v / total for k, v in blended.items()}

        return blended
