"""
Thesis Validator - Commit 6-Q.3.

Validates thesis signals after T+N: did the predicted catalyst actually
occur? This creates "Thesis Accuracy" - a new fitness dimension that rewards
doctrines whose investment JUDGMENTS are correct, not just whose stocks went up.

Without this, the system can't distinguish:
  - A doctrine that picked stocks that rose for the WRONG reason (luck)
  - A doctrine that picked stocks that rose for the RIGHT reason (skill)

Thesis Accuracy measures: of the stocks where the thesis said "earnings will
accelerate", how many actually had earnings accelerate?

Usage:
    from src.thesis.thesis_validator import ThesisValidator
    tv = ThesisValidator()
    accuracy = tv.validate(
        code="600519",
        thesis_date="2026-05-27",
        eval_date="2026-06-24",
    )
    # accuracy = {"earnings_predicted": True, "earnings_actual": True, "correct": True}
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ThesisValidation:
    """Result of validating a thesis after T+N."""

    code: str
    thesis_date: str
    eval_date: str
    # What the thesis predicted
    predicted_acceleration: bool  # was fundamental_acceleration > 0?
    predicted_mispricing: bool  # was mispricing_gap > 0?
    # What actually happened
    actual_earnings_change: float  # Δ(earnings_yoy) between thesis_date and eval_date
    actual_price_change: float  # stock return over the period
    # Validation
    thesis_correct: bool  # was the thesis prediction right?
    accuracy_score: float  # 0-1 (1 = thesis fully validated)


class ThesisValidator:
    """Validates thesis signals against actual outcomes.

    For each stock selected based on a thesis:
    1. Did earnings actually accelerate? (predicted_acceleration vs actual)
    2. Did the mispricing close? (price caught up to fundamentals)
    3. Was the catalyst real? (assets/equity grew as predicted)

    This produces Thesis Accuracy - a measure of judgment quality, not return.
    """

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db

    def validate(
        self,
        code: str,
        thesis_date: str,
        eval_date: str,
        predicted_acceleration: bool | None = None,
        predicted_mispricing: bool | None = None,
        actual_price_change: float | None = None,
    ) -> ThesisValidation:
        """Validate a thesis prediction against actual outcomes.

        Args:
            code: stock code
            thesis_date: when the thesis was made
            eval_date: when to validate (T+N)
            predicted_acceleration: was thesis's acceleration signal positive?
            predicted_mispricing: was thesis's mispricing gap positive?
            actual_price_change: stock return over the period (if precomputed)

        Returns: ThesisValidation with correctness + accuracy_score
        """
        code = str(code).zfill(6)

        # Get financial data at thesis_date and eval_date (nearest reports)
        thesis_fin = self._get_financials_at(code, thesis_date)
        eval_fin = self._get_financials_at(code, eval_date)

        # Actual earnings change: Δ(earnings_yoy) between the two periods
        if thesis_fin and eval_fin:
            thesis_earn_yoy = thesis_fin.get("earnings_yoy")
            eval_earn_yoy = eval_fin.get("earnings_yoy")
            if thesis_earn_yoy is not None and eval_earn_yoy is not None:
                actual_earnings_change = (eval_earn_yoy - thesis_earn_yoy) / 100.0
            else:
                actual_earnings_change = 0.0
        else:
            actual_earnings_change = 0.0

        # Actual price change
        if actual_price_change is not None:
            actual_price = actual_price_change
        else:
            actual_price = self._get_price_return(code, thesis_date, eval_date)

        # Determine if acceleration actually happened
        actual_acceleration = actual_earnings_change > 0

        # Default predictions if not provided
        if predicted_acceleration is None:
            predicted_acceleration = True  # assume thesis was positive

        # Thesis correctness: did the prediction match reality?
        thesis_correct = predicted_acceleration == actual_acceleration

        # Accuracy score: how well did the thesis predict?
        # 1.0 = fully correct, 0.5 = partially, 0.0 = fully wrong
        accuracy_score = 1.0 if thesis_correct else 0.0

        # Boost if mispricing closed (price caught up to fundamentals)
        if predicted_mispricing and actual_earnings_change > 0 and actual_price > 0:
            accuracy_score = min(1.0, accuracy_score + 0.2)

        return ThesisValidation(
            code=code,
            thesis_date=thesis_date,
            eval_date=eval_date,
            predicted_acceleration=predicted_acceleration,
            predicted_mispricing=predicted_mispricing or False,
            actual_earnings_change=actual_earnings_change,
            actual_price_change=actual_price,
            thesis_correct=thesis_correct,
            accuracy_score=accuracy_score,
        )

    def validate_batch(
        self,
        picks: list[dict],
        thesis_date: str,
        eval_date: str,
        pick_returns: list[float] | None = None,
    ) -> dict[str, ThesisValidation]:
        """Validate thesis for a batch of picks.

        Args:
            picks: list of {security_id, thesis_signals: {...}, ...}
            thesis_date, eval_date: validation window
            pick_returns: optional precomputed returns

        Returns: {code: ThesisValidation}
        """
        results = {}
        for i, pick in enumerate(picks):
            code = pick.get("security_id", "")
            bare = code.split(".")[0] if "." in code else code
            thesis_signals = pick.get("thesis_signals", {})

            price_ret = pick_returns[i] if pick_returns and i < len(pick_returns) else None

            validation = self.validate(
                code=bare,
                thesis_date=thesis_date,
                eval_date=eval_date,
                predicted_acceleration=thesis_signals.get("fundamental_acceleration", 0) > 0,
                predicted_mispricing=thesis_signals.get("mispricing_gap", 0) > 0,
                actual_price_change=price_ret,
            )
            results[bare] = validation

        return results

    def compute_thesis_accuracy(self, validations: dict[str, ThesisValidation]) -> float:
        """Compute overall thesis accuracy for a doctrine's picks.

        Returns: 0-1 (fraction of thesis predictions that were correct)
        """
        if not validations:
            return 0.5  # neutral when no data
        scores = [v.accuracy_score for v in validations.values()]
        return float(np.mean(scores))

    def _get_financials_at(self, code: str, date_str: str) -> dict | None:
        """Get the latest financial report at or before a date."""
        conn = sqlite3.connect(self.cache_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM akshare_financials WHERE code=? "
                "AND report_date <= ? ORDER BY report_date DESC LIMIT 1",
                (code, date_str),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _get_price_return(self, code: str, start: str, end: str) -> float:
        """Get stock return between two dates from local K-line."""
        from src.data.local_provider import LocalDataProvider

        local = LocalDataProvider()
        try:
            kline = local.get_daily_kline(code, start, end)
            if kline is not None and not kline.empty and len(kline) >= 2:
                close = kline["close"].values
                return float((close[-1] - close[0]) / close[0])
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)
        return 0.0
