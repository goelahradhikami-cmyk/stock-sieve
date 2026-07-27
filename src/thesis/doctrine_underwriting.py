"""
Doctrine Underwriting Engine - Commit 6-S.2.

Each doctrine acts as an underwriter reviewing the same anomaly from its
own perspective. This is NOT factor scoring - it's investment committee
review. Different doctrines ask different questions about the same
mispricing opportunity.

6-S.1 proved: anomaly + recovery gate produces +0.37% incremental alpha.
6-S.2 adds: "is THIS anomaly worth buying?" - company-level judgment.

Three doctrines (Phase 1):
  1. quality_compounder: "Has the business deteriorated?"
     - PASS if: ROIC stable, margin expanding, cashflow positive
     - REJECT if: margin declining, debt rising, cashflow negative
  2. contrarian: "Is the market excessively pessimistic?"
     - PASS if: drawdown extreme (<-30%), fundamentals not collapsing
     - REJECT if: drawdown mild (just normal volatility)
  3. value_purist: "Is the price truly cheap?"
     - PASS if: PE/PB below historical, FCF yield high
     - REJECT if: valuation not actually compressed

Key: same anomaly, different verdicts. A stock can PASS quality but
REJECT contrarian (good company, but not extreme enough selloff).

Usage:
    from src.thesis.doctrine_underwriting import DoctrineUnderwriter
    uw = DoctrineUnderwriter()
    result = uw.underwrite(anomaly, doctrine_type="quality_compounder")
    # result = UnderwritingResult(verdict="PASS", confidence=0.72, reasons=[...])
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.thesis.market_anomaly import MispricingObject
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class UnderwritingResult:
    """One doctrine's underwriting decision on one anomaly."""

    doctrine_type: str
    verdict: str  # "PASS" / "REJECT" / "CONDITIONAL"
    confidence: float  # 0-1 (how sure the doctrine is)
    reasons: list[str]  # why PASS or REJECT
    red_flags: list[str]  # specific concerns found
    key_questions: dict  # the questions this doctrine asked + answers

    def to_dict(self) -> dict:
        return {
            "doctrine": self.doctrine_type,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "reasons": self.reasons,
            "red_flags": self.red_flags,
            "questions": self.key_questions,
        }


class DoctrineUnderwriter:
    """Investment committee underwriting for anomaly candidates.

    Each doctrine type asks different questions about the same anomaly.
    The result is a structured underwriting decision, not a score.

    Architecture:
      Anomaly (mispricing candidate)
        ↓
      Recovery Gate (market state) - 6-S.1
        ↓
      Doctrine Underwriting (company judgment) - 6-S.2
        ↓
      Final Portfolio
    """

    def underwrite(self, anomaly: MispricingObject, doctrine_type: str) -> UnderwritingResult:
        """Underwrite an anomaly from a specific doctrine's perspective.

        Args:
            anomaly: MispricingObject from MarketAnomalyDetector
            doctrine_type: one of "quality_compounder", "contrarian", "value_purist"

        Returns: UnderwritingResult with verdict + reasons
        """
        if doctrine_type == "quality_compounder":
            return self._underwrite_quality(anomaly)
        elif doctrine_type == "contrarian":
            return self._underwrite_contrarian(anomaly)
        elif doctrine_type == "value_purist":
            return self._underwrite_value(anomaly)
        else:
            return UnderwritingResult(
                doctrine_type=doctrine_type,
                verdict="REJECT",
                confidence=0.0,
                reasons=["Unknown doctrine type"],
                red_flags=[],
                key_questions={},
            )

    def underwrite_all(self, anomaly: MispricingObject) -> dict[str, UnderwritingResult]:
        """Underwrite from all three doctrines' perspectives.

        Returns: {doctrine_type: UnderwritingResult}
        """
        return {
            "quality_compounder": self._underwrite_quality(anomaly),
            "contrarian": self._underwrite_contrarian(anomaly),
            "value_purist": self._underwrite_value(anomaly),
        }

    def _underwrite_quality(self, a: MispricingObject) -> UnderwritingResult:
        """Quality compounder: "Has the business deteriorated?"

        Core question: Is the company STILL a good business despite the
        price drop? A quality investor doesn't buy cheap garbage - they
        buy good companies that are temporarily mispriced.

        PASS conditions:
          - ROE > 0.08 (still profitable, not just "not losing")
          - Margin stable or improving (margin_change > -0.03)
          - Debt manageable (debt_to_equity < 2.0)
          - ROE stability > 0.4 (not volatile)

        REJECT conditions:
          - ROE declining sharply (margin_change < -0.05)
          - Debt spiraling (debt_to_equity > 3.0)
          - ROE very low (< 0.03) - business may be broken
        """
        questions: dict[str, dict[str, Any]] = {}
        reasons = []
        red_flags = []
        pass_count = 0
        total_checks = 4

        # Q1: Is ROE still healthy?
        roe_healthy = a.roe > 0.08
        questions["roe_healthy"] = {"answer": roe_healthy, "value": a.roe}
        if roe_healthy:
            reasons.append(f"ROE={a.roe:.3f} still healthy (>0.08)")
            pass_count += 1
        else:
            red_flags.append(f"ROE={a.roe:.3f} too low, business may be broken")

        # Q2: Is margin stable or improving?
        margin_ok = a.margin_change is not None and a.margin_change > -0.03
        questions["margin_stable"] = {"answer": margin_ok, "value": a.margin_change}
        if margin_ok:
            reasons.append(f"Margin change={a.margin_change:+.4f} stable/improving")
            pass_count += 1
        else:
            red_flags.append(f"Margin change={a.margin_change:+.4f} declining sharply")

        # Q3: Is debt manageable?
        debt_ok = a.debt_ratio is not None and a.debt_ratio < 2.0
        questions["debt_manageable"] = {"answer": debt_ok, "value": a.debt_ratio}
        if debt_ok:
            reasons.append(f"Debt/equity={a.debt_ratio:.2f} manageable")
            pass_count += 1
        else:
            red_flags.append(f"Debt/equity={a.debt_ratio:.2f} excessive")

        # Q4: Is ROE stable over time?
        roe_stable = a.roe_stability is not None and a.roe_stability > 0.4
        questions["roe_stable"] = {"answer": roe_stable, "value": a.roe_stability}
        if roe_stable:
            reasons.append(f"ROE stability={a.roe_stability:.2f} consistent")
            pass_count += 1
        else:
            red_flags.append(f"ROE stability={a.roe_stability:.2f} volatile")

        # Verdict
        if pass_count >= 3:
            verdict = "PASS"
        elif pass_count >= 2:
            verdict = "CONDITIONAL"
        else:
            verdict = "REJECT"

        confidence = pass_count / total_checks

        return UnderwritingResult(
            doctrine_type="quality_compounder",
            verdict=verdict,
            confidence=confidence,
            reasons=reasons,
            red_flags=red_flags,
            key_questions=questions,
        )

    def _underwrite_contrarian(self, a: MispricingObject) -> UnderwritingResult:
        """Contrarian: "Is the market EXCESSIVELY pessimistic?"

        Core question: Is the selloff extreme enough to be irrational?
        A contrarian doesn't buy -10% dips - they buy -40%+ panics where
        the price has disconnected from reality.

        PASS conditions:
          - Price drawdown < -25% (extreme, not normal volatility)
          - Market pessimism > 0.6 (significant fear)
          - Business strength > 0.5 (fundamentals not collapsing)
          - Divergence score > 0.2 (clear gap between price and reality)

        REJECT conditions:
          - Drawdown > -15% (not extreme enough)
          - Business strength < 0.4 (company actually deteriorating)
        """
        questions: dict[str, dict[str, Any]] = {}
        reasons = []
        red_flags = []
        pass_count = 0
        total_checks = 4

        # Q1: Is the selloff extreme?
        extreme_drop = a.price_drawdown_12m < -0.25
        questions["extreme_selloff"] = {"answer": extreme_drop, "value": a.price_drawdown_12m}
        if extreme_drop:
            reasons.append(f"Price drawdown={a.price_drawdown_12m:+.1%} extreme (<-25%)")
            pass_count += 1
        else:
            red_flags.append(f"Price drawdown={a.price_drawdown_12m:+.1%} not extreme enough")

        # Q2: Is market fear significant?
        fear_significant = a.market_pessimism > 0.5
        questions["market_fear"] = {"answer": fear_significant, "value": a.market_pessimism}
        if fear_significant:
            reasons.append(f"Market pessimism={a.market_pessimism:.2f} significant")
            pass_count += 1
        else:
            red_flags.append(f"Market pessimism={a.market_pessimism:.2f} mild")

        # Q3: Are fundamentals still intact?
        fundamentals_ok = a.business_strength > 0.5
        questions["fundamentals_intact"] = {"answer": fundamentals_ok, "value": a.business_strength}
        if fundamentals_ok:
            reasons.append(f"Business strength={a.business_strength:.2f} intact")
            pass_count += 1
        else:
            red_flags.append(f"Business strength={a.business_strength:.2f} deteriorating")

        # Q4: Is there clear divergence?
        clear_divergence = a.divergence_score > 0.2
        questions["clear_divergence"] = {"answer": clear_divergence, "value": a.divergence_score}
        if clear_divergence:
            reasons.append(f"Divergence={a.divergence_score:.2f} clearly mispriced")
            pass_count += 1
        else:
            red_flags.append(f"Divergence={a.divergence_score:.2f} ambiguous")

        if pass_count >= 3:
            verdict = "PASS"
        elif pass_count >= 2:
            verdict = "CONDITIONAL"
        else:
            verdict = "REJECT"

        confidence = pass_count / total_checks

        return UnderwritingResult(
            doctrine_type="contrarian",
            verdict=verdict,
            confidence=confidence,
            reasons=reasons,
            red_flags=red_flags,
            key_questions=questions,
        )

    def _underwrite_value(self, a: MispricingObject) -> UnderwritingResult:
        """Value purist: "Is the price truly cheap vs history?"

        Core question: Has valuation actually compressed enough to offer
        a margin of safety? A value investor needs quantifiable cheapness,
        not just "price went down."

        PASS conditions:
          - PE compression < 0.7 (PE dropped >30% from 1y ago)
          - ROE > 0.05 (not value trap - still profitable)
          - Debt < 2.0 (balance sheet survivable)
          - Margin not collapsing (margin_change > -0.10)

        REJECT conditions:
          - PE compression > 0.9 (barely cheaper)
          - ROE < 0.02 (value trap - cheap for a reason)
        """
        questions: dict[str, dict[str, Any]] = {}
        reasons = []
        red_flags = []
        pass_count = 0
        total_checks = 4

        # Q1: Has valuation compressed significantly?
        pe_compressed = a.pe_compression is not None and a.pe_compression < 0.7
        questions["pe_compressed"] = {"answer": pe_compressed, "value": a.pe_compression}
        if pe_compressed:
            reasons.append(f"PE compression={a.pe_compression:.2f} significant (<0.7)")
            pass_count += 1
        elif a.pe_compression is not None:
            red_flags.append(f"PE compression={a.pe_compression:.2f} not enough")
        else:
            red_flags.append("PE compression unknown (no historical PE data)")

        # Q2: Still profitable? (not a value trap)
        still_profitable = a.roe > 0.05
        questions["still_profitable"] = {"answer": still_profitable, "value": a.roe}
        if still_profitable:
            reasons.append(f"ROE={a.roe:.3f} still profitable (>0.05)")
            pass_count += 1
        else:
            red_flags.append(f"ROE={a.roe:.3f} too low, possible value trap")

        # Q3: Survivable balance sheet?
        survivable = a.debt_ratio is not None and a.debt_ratio < 2.0
        questions["survivable"] = {"answer": survivable, "value": a.debt_ratio}
        if survivable:
            reasons.append(f"Debt/equity={a.debt_ratio:.2f} survivable")
            pass_count += 1
        else:
            red_flags.append(f"Debt/equity={a.debt_ratio:.2f} risky")

        # Q4: Margin not collapsing?
        margin_ok = a.margin_change is not None and a.margin_change > -0.10
        questions["margin_ok"] = {"answer": margin_ok, "value": a.margin_change}
        if margin_ok:
            reasons.append(f"Margin change={a.margin_change:+.4f} not collapsing")
            pass_count += 1
        else:
            red_flags.append(f"Margin change={a.margin_change:+.4f} collapsing")

        if pass_count >= 3:
            verdict = "PASS"
        elif pass_count >= 2:
            verdict = "CONDITIONAL"
        else:
            verdict = "REJECT"

        confidence = pass_count / total_checks

        return UnderwritingResult(
            doctrine_type="value_purist",
            verdict=verdict,
            confidence=confidence,
            reasons=reasons,
            red_flags=red_flags,
            key_questions=questions,
        )

    def consensus(self, results: dict[str, UnderwritingResult]) -> dict:
        """Compute committee consensus from multiple doctrines.

        Returns: {
            "consensus": "PASS"/"REJECT"/"SPLIT",
            "pass_count": N,
            "reject_count": N,
            "avg_confidence": float,
            "agreed_doctrines": [doctrine_names that agree],
        }
        """
        passes = [k for k, v in results.items() if v.verdict == "PASS"]
        rejects = [k for k, v in results.items() if v.verdict == "REJECT"]
        conditionals = [k for k, v in results.items() if v.verdict == "CONDITIONAL"]
        confidences = [v.confidence for v in results.values()]

        if len(passes) >= 2:
            consensus = "PASS"
        elif len(rejects) >= 2:
            consensus = "REJECT"
        else:
            consensus = "SPLIT"

        return {
            "consensus": consensus,
            "pass_count": len(passes),
            "reject_count": len(rejects),
            "conditional_count": len(conditionals),
            "avg_confidence": sum(confidences) / len(confidences) if confidences else 0,
            "passing_doctrines": passes,
            "rejecting_doctrines": rejects,
        }
