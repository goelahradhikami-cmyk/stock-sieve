"""
Bayesian Doctrine Allocation Engine - Commit 6-S.6.2.

Allocates doctrine weights per market state using Bayesian updating.

Three components:
  1. Beta-Binomial: P(success | state, doctrine) from thesis ledger
  2. Return Quality: win_rate × avg_return / volatility (not just win rate)
  3. Time Decay: older thesis weighted less (markets change)

Output: per market state, a doctrine allocation (softmax of adjusted scores)

Architecture:
  Market State -> Bayesian Engine -> Doctrine Allocation -> Underwriting -> Portfolio

Key principle: "not who earns most, but who is most reliable in this state"

Usage:
    from src.thesis.bayesian_allocation import BayesianAllocationEngine
    engine = BayesianAllocationEngine()
    alloc = engine.compute_allocation("CONFIRMED_RECOVERY")
    # alloc = {"quality": 0.40, "value": 0.35, "contrarian": 0.25}
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DoctrineBayesianStats:
    """Bayesian statistics for one doctrine in one market state."""

    doctrine: str
    market_state: str
    # Raw counts
    n_theses: int
    n_success: int
    n_failure: int
    # Returns
    avg_return: float
    return_std: float
    # Bayesian posterior
    alpha: float  # successes + prior
    beta: float  # failures + prior
    posterior_win_prob: float  # alpha / (alpha + beta)
    # Adjusted score
    return_quality: float  # sharpe-like
    time_weighted_score: float  # with decay
    # Final allocation
    allocation_weight: float


class BayesianAllocationEngine:
    """Bayesian doctrine allocation per market state.

    Uses Beta-Binomial model with return quality adjustment and time decay.

    Prior: Beta(2, 2) = uniform-ish, slightly informative
    Update: alpha += successes, beta += failures
    Return quality: posterior × (avg_return / return_std) -- Sharpe-like
    Time decay: exponential, half-life = 365 days
    Allocation: softmax(time_weighted_score) across doctrines
    """

    PRIOR_ALPHA = 2.0  # Beta prior (slightly optimistic)
    PRIOR_BETA = 2.0
    TIME_DECAY_HALF_LIFE_DAYS = 365  # half-life
    SOFTMAX_TEMPERATURE = 10.0  # higher = more uniform

    DOCTRINES = ["quality", "contrarian", "value"]

    def __init__(self, eval_db: str = "data/evaluation.db"):
        self.eval_db = eval_db

    def compute_allocation(self, market_state: str, as_of_date: str = None) -> dict[str, float]:
        """Compute doctrine allocation for a market state.

        Args:
            market_state: one of PANIC/STABILIZING/EARLY_RECOVERY/CONFIRMED_RECOVERY
            as_of_date: for time decay (default = today)

        Returns: {doctrine: allocation_weight} summing to 1.0
        """
        if as_of_date is None:
            from datetime import date

            as_of_date = date.today().isoformat()

        stats = {}
        for doctrine in self.DOCTRINES:
            s = self._compute_doctrine_stats(doctrine, market_state, as_of_date)
            if s:
                stats[doctrine] = s

        if not stats:
            # No data: uniform allocation
            n = len(self.DOCTRINES)
            return {d: 1.0 / n for d in self.DOCTRINES}

        # Softmax of time_weighted_score
        scores = []
        doctrines_ordered = []
        for d in self.DOCTRINES:
            if d in stats:
                scores.append(stats[d].time_weighted_score)
                doctrines_ordered.append(d)
            else:
                # No data for this doctrine: use prior
                scores.append(0.0)  # neutral
                doctrines_ordered.append(d)

        # Softmax
        scores_arr = np.array(scores)
        scores_centered = scores_arr - np.mean(scores_arr)
        exp_vals = np.exp(scores_centered * self.SOFTMAX_TEMPERATURE)
        softmax = exp_vals / np.sum(exp_vals)

        allocation = {d: float(softmax[i]) for i, d in enumerate(doctrines_ordered)}
        return allocation

    def _compute_doctrine_stats(
        self, doctrine: str, market_state: str, as_of_date: str
    ) -> DoctrineBayesianStats | None:
        """Compute Bayesian stats for one doctrine in one state."""
        col = f"{doctrine}_verdict"

        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT {col} as verdict, actual_return, trade_date "
                f"FROM thesis_ledger WHERE {col} IS NOT NULL "
                f"AND market_state=? AND actual_return IS NOT NULL",
                (market_state,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return None

        passes = [r for r in rows if r["verdict"] == "PASS"]
        if not passes:
            return None

        returns = [r["actual_return"] for r in passes]
        n_success = sum(1 for r in returns if r > 0)
        n_failure = len(returns) - n_success

        # Bayesian posterior
        alpha = self.PRIOR_ALPHA + n_success
        beta = self.PRIOR_BETA + n_failure
        posterior = alpha / (alpha + beta)

        # Return quality (Sharpe-like)
        avg_return = float(np.mean(returns)) if returns else 0.0
        return_std = float(np.std(returns)) if len(returns) > 1 else 0.1
        return_std = max(return_std, 0.01)  # avoid division by zero

        # Sharpe-like: avg_return / std, scaled
        sharpe = avg_return / return_std
        # Return quality: posterior × (1 + sharpe) -- boosts doctrines with good risk-adjusted returns
        return_quality = posterior * (1.0 + max(-0.5, min(2.0, sharpe)))

        # Time decay: weight recent theses more
        from datetime import date

        as_of = date.fromisoformat(as_of_date)
        decay_lambda = math.log(2) / self.TIME_DECAY_HALF_LIFE_DAYS

        time_weights = []
        for r in passes:
            try:
                thesis_date = date.fromisoformat(r["trade_date"])
                days_ago = (as_of - thesis_date).days
                weight = math.exp(-decay_lambda * max(0, days_ago))
                time_weights.append(weight)
            except (ValueError, TypeError):
                time_weights.append(0.5)

        # Time-weighted average return
        total_weight = sum(time_weights)
        if total_weight > 0:
            weighted_return = (
                sum(w * r for w, r in zip(time_weights, returns, strict=False)) / total_weight
            )
            weighted_std = max(
                0.01,
                np.sqrt(
                    sum(
                        w * (r - weighted_return) ** 2
                        for w, r in zip(time_weights, returns, strict=False)
                    )
                    / total_weight
                ),
            )
            weighted_sharpe = weighted_return / weighted_std
            time_weighted_score = posterior * (1.0 + max(-0.5, min(2.0, weighted_sharpe)))
        else:
            time_weighted_score = return_quality

        return DoctrineBayesianStats(
            doctrine=doctrine,
            market_state=market_state,
            n_theses=len(passes),
            n_success=n_success,
            n_failure=n_failure,
            avg_return=avg_return,
            return_std=return_std,
            alpha=alpha,
            beta=beta,
            posterior_win_prob=posterior,
            return_quality=return_quality,
            time_weighted_score=time_weighted_score,
            allocation_weight=0.0,  # filled by softmax
        )

    def get_full_allocation_table(self, as_of_date: str = None) -> dict[str, dict]:
        """Get allocation for all market states.

        Returns: {market_state: {doctrine: weight, ...}}
        """
        result = {}
        for state in ["PANIC", "STABILIZING", "EARLY_RECOVERY", "CONFIRMED_RECOVERY"]:
            alloc = self.compute_allocation(state, as_of_date)
            # Get stats for display
            stats = {}
            for d in self.DOCTRINES:
                s = self._compute_doctrine_stats(d, state, as_of_date or "2026-07-17")
                if s:
                    stats[d] = {
                        "n": s.n_theses,
                        "win_rate": s.posterior_win_prob,
                        "avg_return": s.avg_return,
                        "sharpe": s.avg_return / max(s.return_std, 0.01),
                        "allocation": alloc.get(d, 0),
                    }
                else:
                    stats[d] = {
                        "n": 0,
                        "win_rate": 0.5,
                        "avg_return": 0,
                        "sharpe": 0,
                        "allocation": alloc.get(d, 0),
                    }
            result[state] = {"allocation": alloc, "stats": stats}
        return result
