"""
A/B Counterfactual Engine - Commit 6-Q.2b.

The most direct test of thesis value: does thesis-driven selection beat
factor-driven selection on the SAME stocks, SAME date, SAME horizon?

Control (factor-only):   factor_bias weighted -> Top 20
Treatment (thesis):      factor constraint + thesis residual -> Top 20

Incremental Thesis Alpha = Treatment Return - Control Return

If >0: thesis adds value (Agent has opinions that make money)
If ~0: thesis is noise (pretty story generator, no investment value)
If <0: thesis hurts (overcomplicates selection)

This is the gate: Evolution v4 should NOT proceed until Incremental
Thesis Alpha > 0.5% on at least 100 historical dates.

Usage:
    from src.thesis.counterfactual import CounterfactualEngine
    cf = CounterfactualEngine()
    result = cf.run_ab_test(
        doctrine=doctrine,
        trade_date="2026-05-27",
        horizon=20,
    )
    # result.incremental_alpha = treatment_return - control_return
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np

from src.agents.doctrine_engine import DoctrineGenome
from src.data.local_provider import LocalDataProvider
from src.factors.snapshot_builder import FactorSnapshotBuilder
from src.thesis.residualizer import ThesisResidualizer
from src.thesis.signal_engine import ThesisSignalEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ABTestResult:
    """A/B test result for one date."""

    trade_date: str
    eval_date: str
    # Control (factor-only)
    control_picks: list[str] = field(default_factory=list)
    control_return: float = 0.0
    # Treatment (thesis)
    treatment_picks: list[str] = field(default_factory=list)
    treatment_return: float = 0.0
    # Difference
    incremental_alpha: float = 0.0  # treatment - control
    overlap_rate: float = 0.0  # how many stocks are in both portfolios
    # Thesis accuracy
    thesis_accuracy: float = 0.0


class CounterfactualEngine:
    """A/B test: factor-only vs thesis-driven selection.

    For each date:
      Control:   factor_bias weighted score -> Top 20 (old method)
      Treatment: factor constraint (quality>30) + thesis residual score -> Top 20

    Incremental Alpha = treatment_return - control_return
    """

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db
        self.builder = FactorSnapshotBuilder()
        self.local = LocalDataProvider()
        self.signal_engine = ThesisSignalEngine(cache_db=cache_db)
        self.residualizer = ThesisResidualizer(eval_db=eval_db)

    def run_ab_test(
        self, doctrine: DoctrineGenome, trade_date: str, horizon: int = 20
    ) -> ABTestResult:
        """Run one A/B test for a doctrine on a date.

        Returns: ABTestResult with control/treatment returns + incremental alpha
        """
        # Find eval date
        eval_date = self._get_eval_date(trade_date, horizon)
        if not eval_date:
            return ABTestResult(trade_date=trade_date, eval_date="")

        # Get all stocks' factor scores for this date (use Top 200 pool)
        all_picks = self.builder.score_universe(trade_date, doctrine.factor_bias, top_n=200)
        if not all_picks:
            return ABTestResult(trade_date=trade_date, eval_date=eval_date)

        # === CONTROL: factor_bias Top 20 ===
        control_picks = all_picks[:20]  # already sorted by factor alpha
        control_returns = self._compute_returns(control_picks, trade_date, eval_date)
        control_return = float(np.mean(control_returns)) if control_returns else 0.0

        # === TREATMENT: factor constraint + thesis residual Top 20 ===
        # 1. Compute thesis signals for all stocks in pool
        all_codes = [self._bare_code(p["security_id"]) for p in all_picks]
        thesis_signals = self.signal_engine.compute_signals_batch(all_codes, trade_date)

        # 2. Orthogonalize thesis signals against factors
        ts_dict = {code: {"thesis_score": sig.thesis_score} for code, sig in thesis_signals.items()}
        residualized = self.residualizer.orthogonalize_universe(trade_date, ts_dict)

        # 3. Factor constraint: keep only quality > 30 (risk filter, not selection)
        # Then sort by thesis residual (NOT factor score)
        candidates = []
        for pick in all_picks:
            bare = self._bare_code(pick["security_id"])
            if pick.get("quality_score", 50) > 30:
                res_score = residualized.get(bare, 0)
                candidates.append((pick, res_score, bare))

        candidates.sort(key=lambda x: x[1], reverse=True)
        treatment_picks = [c[0] for c in candidates[:20]]
        treatment_codes = [c[2] for c in candidates[:20]]

        treatment_returns = self._compute_returns(treatment_picks, trade_date, eval_date)
        treatment_return = float(np.mean(treatment_returns)) if treatment_returns else 0.0

        # Overlap: how many stocks are in both portfolios?
        control_codes = {self._bare_code(p["security_id"]) for p in control_picks}
        treatment_code_set = set(treatment_codes)
        overlap = len(control_codes & treatment_code_set) / max(1, len(control_codes))

        # Incremental alpha
        incremental = treatment_return - control_return

        return ABTestResult(
            trade_date=trade_date,
            eval_date=eval_date,
            control_picks=list(control_codes),
            control_return=control_return,
            treatment_picks=treatment_codes,
            treatment_return=treatment_return,
            incremental_alpha=incremental,
            overlap_rate=overlap,
        )

    def run_batch(self, doctrine: DoctrineGenome, dates: list[str], horizon: int = 20) -> dict:
        """Run A/B test across multiple dates.

        Returns: summary with avg incremental alpha + stats
        """
        results = []
        for d in dates:
            r = self.run_ab_test(doctrine, d, horizon)
            if r.eval_date:
                results.append(r)

        if not results:
            return {"n": 0, "incremental_alpha": 0, "verdict": "no_data"}

        increments = [r.incremental_alpha for r in results]
        avg_increment = float(np.mean(increments))
        std_increment = float(np.std(increments))
        win_rate = float(np.mean([1 if x > 0 else 0 for x in increments]))
        avg_overlap = float(np.mean([r.overlap_rate for r in results]))

        # T-statistic (is incremental alpha significantly > 0?)
        if std_increment > 0:
            t_stat = avg_increment / (std_increment / np.sqrt(len(increments)))
        else:
            t_stat = 0

        verdict = (
            "THESIS_ADDS_VALUE"
            if avg_increment > 0.005 and t_stat > 1.5
            else "THESIS_NEUTRAL"
            if abs(avg_increment) < 0.005
            else "THESIS_HURTS"
        )

        return {
            "n": len(results),
            "avg_control_return": float(np.mean([r.control_return for r in results])),
            "avg_treatment_return": float(np.mean([r.treatment_return for r in results])),
            "incremental_alpha": avg_increment,
            "std": std_increment,
            "win_rate": win_rate,
            "t_stat": t_stat,
            "avg_overlap": avg_overlap,
            "verdict": verdict,
        }

    def _compute_returns(self, picks: list[dict], start: str, end: str) -> list[float]:
        """Compute forward returns for a list of picks."""
        returns = []
        for pick in picks:
            code = self._bare_code(pick["security_id"])
            try:
                kline = self.local.get_daily_kline(code, start, end)
                if kline is not None and not kline.empty and len(kline) >= 2:
                    close = kline["close"].values
                    returns.append((close[-1] - close[0]) / close[0])
            except Exception as exc:
                logger.warning("operation failed (was silently ignored): %s", exc)
        return returns

    def _get_eval_date(self, trade_date: str, horizon: int) -> str | None:
        conn = sqlite3.connect(self.cache_db)
        try:
            row = conn.execute(
                "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
                "AND trade_date > ? ORDER BY trade_date LIMIT 1 OFFSET ?",
                (trade_date, horizon - 1),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    @staticmethod
    def _bare_code(security_id: str) -> str:
        return security_id.split(".")[0] if "." in security_id else security_id
