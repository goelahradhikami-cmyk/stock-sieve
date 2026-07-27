"""
Portfolio Decision Policy DNA — Behavioral decision rules for portfolio genome.

Commit 6-H.2 Fix 1: Encodes "when to trust yourself, when to doubt"
as a heritable genome component.

Structure:
  - confidence_policy: score thresholds → max position
  - uncertainty_response: model disagreement → action
  - loss_response: consecutive losses → defensive action
  - regime_transition: regime changes → exposure adjustment
"""

import json

from src.data.db import managed_connect


class PortfolioDecisionDNA:
    """Decision behavior genome — heritable policy rules."""

    # Default DNA — conservative baseline
    DEFAULT = {
        "confidence_policy": {
            "high": {"score": 90, "max_position": 0.18},
            "medium": {"score": 70, "max_position": 0.10},
            "low": {"score": 50, "max_position": 0.03},
        },
        "uncertainty_response": {
            "model_disagreement": 0.3,
            "action": "reduce_exposure",
            "reduce_pct": 0.25,
        },
        "loss_response": {
            "three_consecutive_losses": "reduce_20_percent",
            "factor_failure": "freeze_factor",
            "regime_change_detected": "rebuild_portfolio",
        },
        "regime_transition": {
            "rotation_to_bear": "increase_cash_15pct",
            "bull_to_rotation": "reduce_momentum_exposure",
            "any_to_crisis": "reduce_to_min_positions",
        },
    }

    @classmethod
    def create_default(cls) -> dict:
        return json.loads(json.dumps(cls.DEFAULT))

    @classmethod
    def create_child(cls, parent_a: dict, parent_b: dict, alpha: float = 0.5) -> dict:
        """Crossover: interpolate policy thresholds."""
        child = {}
        for key in cls.DEFAULT:
            if key in parent_a and key in parent_b:
                if key == "confidence_policy":
                    child[key] = cls._interpolate_confidence(parent_a[key], parent_b[key], alpha)
                elif key == "uncertainty_response":
                    child[key] = parent_a[key] if alpha > 0.5 else parent_b[key]
                    child[key]["reduce_pct"] = round(
                        parent_a[key].get("reduce_pct", 0.25) * alpha
                        + parent_b[key].get("reduce_pct", 0.25) * (1 - alpha),
                        2,
                    )
                else:
                    child[key] = parent_a[key] if alpha > 0.5 else parent_b[key]
            else:
                child[key] = parent_a.get(key, parent_b.get(key, cls.DEFAULT.get(key, {})))
        return child

    @classmethod
    def _interpolate_confidence(cls, a: dict, b: dict, alpha: float) -> dict:
        """Smooth interpolation of confidence thresholds."""
        result = {}
        for level in ("high", "medium", "low"):
            if level in a and level in b:
                result[level] = {
                    "score": int(
                        a[level].get("score", 70) * alpha + b[level].get("score", 70) * (1 - alpha)
                    ),
                    "max_position": round(
                        a[level].get("max_position", 0.10) * alpha
                        + b[level].get("max_position", 0.10) * (1 - alpha),
                        2,
                    ),
                }
        return result

    @classmethod
    def mutate(cls, parent: dict) -> dict:
        """Small random perturbation to policy thresholds."""
        import random

        child = json.loads(json.dumps(parent))
        for level in ("high", "medium", "low"):
            if level in child.get("confidence_policy", {}):
                delta = random.uniform(-0.02, 0.02)
                child["confidence_policy"][level]["max_position"] = max(
                    0.01, min(0.30, child["confidence_policy"][level]["max_position"] + delta)
                )
        return child


class RegimeProbabilityPolicy:
    """Probability-driven exposure rules for portfolio genome."""

    DEFAULT = {
        "bull_prob_80_to_100": {"equity_exposure": 0.95},
        "bull_prob_50_to_80": {"equity_exposure": 0.85},
        "bear_prob_60_to_100": {"equity_exposure": 0.60},
        "crisis_prob_over_60": {"equity_exposure": 0.30},
        "default": {"equity_exposure": 0.75},
    }

    @classmethod
    def get_exposure(cls, probs: dict, policy: dict | None = None) -> float:
        """Determine equity exposure from regime probabilities and policy."""
        if policy is None:
            policy = cls.DEFAULT

        bull_p = probs.get("bull", 0)
        bear_p = probs.get("bear", 0)
        crisis_p = probs.get("crisis", 0)

        if crisis_p > 0.60:
            return policy.get("crisis_prob_over_60", {}).get("equity_exposure", 0.30)
        if bull_p > 0.80:
            return policy.get("bull_prob_80_to_100", {}).get("equity_exposure", 0.95)
        if bull_p > 0.50:
            return policy.get("bull_prob_50_to_80", {}).get("equity_exposure", 0.85)
        if bear_p > 0.60:
            return policy.get("bear_prob_60_to_100", {}).get("equity_exposure", 0.60)

        return policy.get("default", {}).get("equity_exposure", 0.75)


class PortfolioFitnessEvaluator:
    """Fitness function with Alpha Efficiency (Commit 6-H.2 Fix 2)."""

    def evaluate(self, genome: dict, decision_policy: dict, backtest: dict) -> float:
        perf = backtest.get("performance", {})
        risk = backtest.get("risk", {})

        # Alpha efficiency: excess return per unit of factor exposure
        factor_exposure = risk.get("factor_exposure", 1.0)
        alpha_efficiency = perf.get("annual_return", 0) / max(1.0, factor_exposure)

        # Decision policy effectiveness
        decision_score = self._evaluate_decision_policy(decision_policy, backtest)

        fitness = (
            (perf.get("sharpe", 0) or 0) * 0.20
            + alpha_efficiency * 0.15
            + (perf.get("calmar", 0) or 0) * 0.15
            + (1 - abs(risk.get("max_drawdown", 0) or 0)) * 0.15
            + (risk.get("regime_consistency", 0) or 0) * 0.15
            + decision_score * 0.10
            + (risk.get("diversity_score", 0) or 0) * 0.10
        )
        return max(0, fitness)

    def _evaluate_decision_policy(self, policy: dict, backtest: dict) -> float:
        events = backtest.get("decision_events", [])
        if not events:
            return 0.5
        correct = sum(1 for e in events if e.get("outcome") == "positive")
        return correct / len(events)


class GeneAgeTracker:
    """Portfolio gene aging — age increases mutation rate (Commit 6-H.2 Fix 6)."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)

    def update_daily(self):
        self.db.execute("UPDATE portfolio_gene_age SET age_days = age_days + 1")
        self.db.commit()

    def get_mutation_multiplier(self, genome_id: str) -> float:
        """Higher age or performance decay → higher mutation rate."""
        row = self.db.execute(
            "SELECT age_days, performance_decay FROM portfolio_gene_age WHERE genome_id=?",
            (genome_id,),
        ).fetchone()
        if not row:
            return 1.0

        age_days, decay = row
        multiplier = 1.0
        if age_days and age_days > 365:
            multiplier *= 1.5
        if decay and decay > 0.2:
            multiplier *= 2.0
        return min(3.0, multiplier)
