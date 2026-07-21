"""
Thesis Ledger + Kill Criteria - Commit 6-S.3.

Records every investment thesis decision (discovery -> context -> underwriting
-> decision -> outcome) so the system builds an "experience database".

6-S.2 showed 85% PASS rate - too high. Real investment committees reject
most deals. Kill Criteria adds hard vetoes that override PASS:

  quality_kill: ROE declining AND margin declining AND debt rising
  value_kill:   PE not compressed AND ROE declining
  contrarian_kill: drawdown < -15% AND margin declining AND debt > 2.0

These are not "scores" - they are "this deal is dead, stop looking."

The Ledger stores both successes AND failures, with failure_type taxonomy:
  - macro_risk: market was selling risk premium, not business value
  - value_trap: company actually deteriorating, market was right
  - earnings_deterioration: margin/ROE collapsed after entry
  - timing_error: right thesis, wrong timing (market didn't recover)

Usage:
    from src.thesis.thesis_ledger import ThesisLedger
    ledger = ThesisLedger()
    ledger.record_thesis(anomaly, market_state, underwriting, action)
    ledger.record_outcome(thesis_id, actual_return)
    stats = ledger.get_stats()
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from src.thesis.market_anomaly import MispricingObject
from src.thesis.market_recovery import MarketState
from src.thesis.doctrine_underwriting import UnderwritingResult
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class KillCriteriaResult:
    """Result of kill criteria check."""
    killed: bool
    kill_reason: str
    kill_doctrine: str  # which doctrine's kill criteria triggered


class KillCriteria:
    """Hard veto conditions that override PASS verdicts.

    These are absolute deal-breakers, not soft scores. If any triggers,
    the thesis is REJECTED regardless of other checks.
    """

    @staticmethod
    def check(anomaly: MispricingObject) -> KillCriteriaResult:
        """Check all kill criteria. Returns first kill if any.

        Order matters: most dangerous traps first.
        """
        # Kill 1: Quality deterioration (business is broken)
        # ROE low AND margin declining AND debt high = structural problem
        if (anomaly.roe < 0.05
            and anomaly.margin_change is not None
            and anomaly.margin_change < -0.05
            and anomaly.debt_ratio is not None
            and anomaly.debt_ratio > 2.0):
            return KillCriteriaResult(
                killed=True,
                kill_reason="quality_deterioration: ROE<0.05 + margin declining + debt>2.0",
                kill_doctrine="quality_compounder",
            )

        # Kill 2: Value trap (cheap for a reason)
        # PE not compressed AND earnings declining = market correctly pricing decline
        if (anomaly.pe_compression is not None
            and anomaly.pe_compression > 0.9
            and anomaly.margin_change is not None
            and anomaly.margin_change < -0.03):
            return KillCriteriaResult(
                killed=True,
                kill_reason="value_trap: PE not compressed + margin declining",
                kill_doctrine="value_purist",
            )

        # Kill 3: Contrarian trap (catching falling knife)
        # Price dropping BUT margin collapsing AND debt spiraling
        if (anomaly.price_drawdown_12m < -0.30
            and anomaly.margin_change is not None
            and anomaly.margin_change < -0.10
            and anomaly.debt_ratio is not None
            and anomaly.debt_ratio > 2.5):
            return KillCriteriaResult(
                killed=True,
                kill_reason="falling_knife: price<-30% + margin<-10% + debt>2.5",
                kill_doctrine="contrarian",
            )

        # Kill 4: Cashflow deterioration (profit is fake)
        # ROE positive but cashflow_trend strongly negative
        if (anomaly.roe > 0.10
            and anomaly.cashflow_trend is not None
            and anomaly.cashflow_trend < -0.30):
            return KillCriteriaResult(
                killed=True,
                kill_reason="cashflow_divergence: ROE>10% but cashflow declining >30%",
                kill_doctrine="quality_compounder",
            )

        return KillCriteriaResult(killed=False, kill_reason="", kill_doctrine="")


class ThesisLedger:
    """Records investment thesis decisions + outcomes.

    The "experience database" for the AI investment committee. Every
    underwriting decision (PASS/REJECT/KILL) is recorded with full context,
    then updated with T+N outcome.

    This enables:
      1. Failure taxonomy (what type of anomaly fails most?)
      2. Doctrine calibration (which doctrine's PASS is most reliable?)
      3. Kill criteria tuning (which kills were correct?)
    """

    def __init__(self, eval_db: str = "data/evaluation.db"):
        self.eval_db = eval_db

    def record_thesis(self, anomaly: MispricingObject,
                       market_state: MarketState,
                       underwriting: dict[str, UnderwritingResult],
                       kill_result: KillCriteriaResult,
                       action: str,
                       eval_date: str = None) -> str:
        """Record a thesis decision to the ledger.

        Args:
            anomaly: the mispricing object
            market_state: market recovery state
            underwriting: {doctrine: UnderwritingResult}
            kill_result: kill criteria check result
            action: "BUY" / "REJECT" / "HOLD"
            eval_date: T+N date for outcome validation

        Returns: thesis_id
        """
        thesis_id = f"T{anomaly.trade_date.replace('-','')}_{anomaly.code}"

        # Extract underwriting verdicts
        q_uw = underwriting.get("quality_compounder")
        c_uw = underwriting.get("contrarian")
        v_uw = underwriting.get("value_purist")

        kill_triggered = kill_result.kill_reason if kill_result.killed else None

        conn = sqlite3.connect(self.eval_db)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO thesis_ledger
                (thesis_id, trade_date, eval_date, code,
                 anomaly_type, price_drawdown_12m, roe, margin_change,
                 market_pessimism, business_strength, divergence_score,
                 recovery_probability, market_regime,
                 quality_verdict, quality_confidence,
                 contrarian_verdict, contrarian_confidence,
                 value_verdict, value_confidence,
                 consensus, kill_criteria_triggered,
                 action, thesis_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')
            """, (
                thesis_id, anomaly.trade_date, eval_date, anomaly.code,
                anomaly.divergence_type, anomaly.price_drawdown_12m,
                anomaly.roe, anomaly.margin_change,
                anomaly.market_pessimism, anomaly.business_strength,
                anomaly.divergence_score,
                market_state.recovery_probability, market_state.state_label,
                q_uw.verdict if q_uw else None, q_uw.confidence if q_uw else None,
                c_uw.verdict if c_uw else None, c_uw.confidence if c_uw else None,
                v_uw.verdict if v_uw else None, v_uw.confidence if v_uw else None,
                "KILLED" if kill_result.killed else "REVIEWED",
                kill_triggered,
                action,
            ))
            conn.commit()
        finally:
            conn.close()

        return thesis_id

    def record_outcome(self, thesis_id: str, actual_return: float,
                        failure_type: str = None,
                        failure_reason: str = None) -> None:
        """Update a thesis with T+N outcome.

        Args:
            thesis_id: the thesis to update
            actual_return: T+N forward return
            failure_type: if failed, what type (macro_risk/value_trap/etc)
            failure_reason: human-readable failure explanation
        """
        # Determine thesis status
        if actual_return is None:
            status = "no_data"
        elif actual_return > 0:
            status = "validated"
        else:
            status = "failed"

        conn = sqlite3.connect(self.eval_db)
        try:
            conn.execute("""
                UPDATE thesis_ledger SET
                actual_return=?, thesis_status=?, failure_type=?, failure_reason=?
                WHERE thesis_id=?
            """, (actual_return, status, failure_type, failure_reason, thesis_id))
            conn.commit()
        finally:
            conn.close()

    def classify_failure(self, thesis_id: str, actual_return: float,
                          market_regime: str, recovery_prob: float) -> str:
        """Classify failure type based on context + outcome.

        Failure taxonomy:
          - macro_risk: market was in panic, recovery_prob < 0.4
          - value_trap: business strength was low (< 0.4) despite "anomaly"
          - earnings_deterioration: margin_change was negative
          - timing_error: recovery was uncertain (0.4-0.5) and market didn't recover
          - unknown: can't classify
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT business_strength, margin_change, market_pessimism "
                "FROM thesis_ledger WHERE thesis_id=?",
                (thesis_id,),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return "unknown"

        strength, margin_chg, pessimism = row

        if recovery_prob < 0.4:
            return "macro_risk"
        if strength is not None and strength < 0.4:
            return "value_trap"
        if margin_chg is not None and margin_chg < -0.03:
            return "earnings_deterioration"
        if 0.4 <= recovery_prob <= 0.5:
            return "timing_error"
        return "unknown"

    def get_stats(self) -> dict:
        """Get aggregate statistics from the ledger."""
        conn = sqlite3.connect(self.eval_db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM thesis_ledger").fetchone()[0]
            validated = conn.execute("SELECT COUNT(*) FROM thesis_ledger WHERE thesis_status='validated'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM thesis_ledger WHERE thesis_status='failed'").fetchone()[0]
            killed = conn.execute("SELECT COUNT(*) FROM thesis_ledger WHERE kill_criteria_triggered IS NOT NULL").fetchone()[0]

            # Failure type distribution
            failure_types = {}
            for row in conn.execute(
                "SELECT failure_type, COUNT(*) FROM thesis_ledger "
                "WHERE thesis_status='failed' GROUP BY failure_type"
            ).fetchall():
                failure_types[row[0] or "unclassified"] = row[1]

            # Avg return by action
            avg_buy = conn.execute(
                "SELECT AVG(actual_return) FROM thesis_ledger WHERE action='BUY' AND actual_return IS NOT NULL"
            ).fetchone()[0]
            avg_reject = conn.execute(
                "SELECT AVG(actual_return) FROM thesis_ledger WHERE action='REJECT' AND actual_return IS NOT NULL"
            ).fetchone()[0]

            # Doctrine accuracy
            doctrine_stats = {}
            for doctrine, col in [("quality", "quality_verdict"), ("contrarian", "contrarian_verdict"), ("value", "value_verdict")]:
                passes = conn.execute(
                    f"SELECT COUNT(*) FROM thesis_ledger WHERE {col}='PASS' AND actual_return IS NOT NULL"
                ).fetchone()[0]
                pass_win = conn.execute(
                    f"SELECT COUNT(*) FROM thesis_ledger WHERE {col}='PASS' AND actual_return > 0"
                ).fetchone()[0]
                doctrine_stats[doctrine] = {
                    "pass_count": passes,
                    "pass_win_rate": pass_win / passes if passes > 0 else 0,
                }
        finally:
            conn.close()

        return {
            "total": total,
            "validated": validated,
            "failed": failed,
            "killed": killed,
            "validation_rate": validated / total if total > 0 else 0,
            "failure_types": failure_types,
            "avg_buy_return": avg_buy,
            "avg_reject_return": avg_reject,
            "doctrine_accuracy": doctrine_stats,
        }
