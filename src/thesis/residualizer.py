"""
Thesis Residualizer - Commit 6-Q.2.

Orthogonalizes thesis signals against factor exposure, so that what enters
portfolio selection is GENUINELY new information, not factor in disguise.

The Benchmark Test (6-O.6) proved: raw factor_bias -> Top20 produces
selection_alpha ≈ 0. The Factor Neutralization (6-P) proved: 100% of return
is factor exposure. This module ensures thesis signals don't repeat that
mistake - it strips any factor-correlated component from thesis signals
BEFORE they influence stock selection.

Method:
  1. For each stock, we have factor scores (quality/value/growth/...) AND
     thesis signals (acceleration/mispricing/catalyst).
  2. Regress thesis_signal on factor_scores: T = βF + residual
  3. The residual (T_residual) is the factor-orthogonal thesis signal.
  4. Doctrine selection uses T_residual, not raw T.

This is the "Residualized Thesis Signal" from user's 6-Q design:
> "原始 thesis score T,回归已有 factor: T = βF + residual,
>  得到 T_residual,这才是新信息。"

Usage:
    from src.thesis.residualizer import ThesisResidualizer
    rz = ThesisResidualizer()
    residual = rz.orthogonalize(
        thesis_score=0.15,
        factor_scores={"quality": 0.85, "value": 0.30, ...},
        all_stocks_thesis=[...],
        all_stocks_factors=[...],
    )
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ResidualizedSignal:
    """Thesis signal after removing factor-correlated component."""

    raw_thesis: float  # original thesis_score
    factor_component: float  # βF (what factors can explain)
    residual: float  # T - βF (genuinely new information)
    orthogonality: float  # 1 - |factor_component / raw_thesis| (0=fully factor, 1=fully new)


class ThesisResidualizer:
    """Removes factor-correlated component from thesis signals.

    For a universe of stocks, regresses each thesis signal on the 6 factor
    families and keeps only the residual. This ensures thesis-driven selection
    produces factor-neutral alpha, not disguised factor exposure.
    """

    FACTOR_FAMILIES = ["quality", "value", "growth", "momentum", "risk", "sentiment"]

    def __init__(self, eval_db: str = "data/evaluation.db"):
        self.eval_db = eval_db

    def orthogonalize_universe(
        self, trade_date: str, thesis_signals: dict[str, dict]
    ) -> dict[str, float]:
        """Orthogonalize thesis signals for all stocks on a date.

        Args:
            trade_date: for loading factor scores
            thesis_signals: {code: {"thesis_score": float, ...}}

        Returns: {code: residualized_thesis_score}
        """
        if not thesis_signals:
            return {}

        # Load factor scores for all stocks on this date
        factor_scores = self._load_factor_scores(trade_date)

        # Build matched arrays: only stocks with BOTH thesis + factor data
        codes = []
        thesis_values = []
        factor_matrix = []

        for code, ts in thesis_signals.items():
            if code in factor_scores and "thesis_score" in ts:
                codes.append(code)
                thesis_values.append(ts["thesis_score"])
                factor_matrix.append(
                    [factor_scores[code].get(f, 50.0) / 100.0 for f in self.FACTOR_FAMILIES]
                )

        if len(codes) < 10:
            # Too few for regression - return raw thesis (can't orthogonalize)
            return {code: ts.get("thesis_score", 0) for code, ts in thesis_signals.items()}

        T = np.array(thesis_values)
        F = np.array(factor_matrix)

        # OLS: T = βF + ε (no intercept - we want to remove factor component)
        try:
            betas = np.linalg.lstsq(F, T, rcond=None)[0]
            T_predicted = F @ betas
            T_residual = T - T_predicted
        except np.linalg.LinAlgError:
            # Regression failed - return raw thesis
            return {code: ts.get("thesis_score", 0) for code, ts in thesis_signals.items()}

        # Return residualized signals
        return {codes[i]: float(T_residual[i]) for i in range(len(codes))}

    def orthogonalize_single(
        self,
        thesis_score: float,
        factor_scores: dict[str, float],
        universe_betas: np.ndarray | None = None,
    ) -> ResidualizedSignal:
        """Orthogonalize a single stock's thesis signal.

        Uses pre-computed universe betas (from orthogonalize_universe) to
        strip the factor component. If no betas provided, returns raw signal.

        Args:
            thesis_score: raw thesis signal
            factor_scores: {factor: score} for this stock (0-100, normalized to 0-1)
            universe_betas: regression coefficients from orthogonalize_universe

        Returns: ResidualizedSignal with raw/factor/residual/orthogonality
        """
        if universe_betas is None:
            return ResidualizedSignal(
                raw_thesis=thesis_score,
                factor_component=0.0,
                residual=thesis_score,
                orthogonality=1.0,
            )

        F = np.array([factor_scores.get(f, 50.0) / 100.0 for f in self.FACTOR_FAMILIES])
        factor_component = float(F @ universe_betas)
        residual = thesis_score - factor_component

        if abs(thesis_score) > 1e-6:
            orthogonality = max(0.0, 1.0 - abs(factor_component / thesis_score))
        else:
            orthogonality = 1.0

        return ResidualizedSignal(
            raw_thesis=thesis_score,
            factor_component=factor_component,
            residual=residual,
            orthogonality=orthogonality,
        )

    def _load_factor_scores(self, trade_date: str) -> dict[str, dict[str, float]]:
        """Load factor scores for all stocks on a date from stock_factor_snapshot."""
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT security_id, quality_score, value_score, growth_score, "
                "momentum_score, risk_score, sentiment_score "
                "FROM stock_factor_snapshot WHERE trade_date=?",
                (trade_date,),
            ).fetchall()
            return {
                r["security_id"]: {
                    "quality": r["quality_score"] or 50,
                    "value": r["value_score"] or 50,
                    "growth": r["growth_score"] or 50,
                    "momentum": r["momentum_score"] or 50,
                    "risk": r["risk_score"] or 50,
                    "sentiment": r["sentiment_score"] or 50,
                }
                for r in rows
            }
        finally:
            conn.close()
