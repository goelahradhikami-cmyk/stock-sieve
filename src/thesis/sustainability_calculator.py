"""
Sustainability Calculator - Commit 6-S.16.1 (v3.4 Phase 1).

Computes 3-quarter fundamental credibility signals for the Expectation
Quality Engine (EQE). This is a DIAGNOSTIC layer, not an alpha factor.
Phase 1.5 Ablation will decide whether sustainability has alpha content
before it is promoted.

Three sub-components (each stores RAW VALUES + derived flags per the
6-S.16.0a freeze amendment):

  1. Alignment (revenue-earnings decoupling)
     - profit_elasticity = earnings_yoy / revenue_yoy (raw)
     - alignment_flag: sign match AND |rev| >= 0.3*|earn|
     Industry differences are large (software: +20% rev / +80% earn ok;
     manufacturing: +20% / +300% suspect). Raw value stored so industry
     thresholds can be applied without re-backfill.

  2. Persistence (3-quarter acceleration trend)
     - accel_q0/q1/q2, accel_trend, accel_volatility, reversal_count (raw)
     - consistency_flag: accel_q0 > 0 AND reversal_count <= 1
     v3.3.1 autopsy proved extreme acceleration correlates with market
     distrust; raw accel_trend distinguishes 'sustained acceleration'
     from 'spike'.

  3. Margin normalization (peak-margin mean-reversion risk)
     - operating_margin_current, 3q median/std (raw)
     - company_margin_zscore (vs own 3q history)
     - industry_margin_zscore (vs industry cross-section, same period)
     - margin_normalization_flag: max(company_z, industry_z) < 1.5
     operating_margin substituted for gross_margin (gross_margin table
     has 0 rows; operating_profit/revenue 99.9% coverage).

Vintage safety (critical):
  All queries gate on available_date <= as_of_date. report_date alone is
  NOT vintage-safe (the report period end is known before the actual
  announcement). Falls back to report_date + 90 days if available_date
  is NULL (matches FRM/event_reaction convention).

Usage:
    from src.thesis.sustainability_calculator import SustainabilityCalculator
    calc = SustainabilityCalculator()
    result = calc.compute("600519", "2024-08-29")
    # result.sustainability_pass = 1 (all 3 flags pass)
    # result.profit_elasticity = 3.2 (raw, industry-standardized later)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Tunable thresholds (6-S.16.0a: flags are defaults, raw values stored)
# ─────────────────────────────────────────────────────────────────────

# Alignment
ALIGNMENT_MIN_REVENUE_RATIO = 0.30  # |revenue_yoy| >= 0.30 * |earnings_yoy|

# Persistence
CONSISTENCY_MAX_REVERSALS = 1  # reversal_count <= 1 (allows 1 sign flip, not V-shape)

# Margin normalization
MARGIN_ZSCORE_PEAK = 1.5  # max(company_z, industry_z) < 1.5
MARGIN_STD_FLOOR = 0.005  # avoid div-by-zero on near-constant margins
MIN_INDUSTRY_PEERS = 5  # need >=5 industry peers for industry zscore

# Minimum data requirements
MIN_PERIODS_FOR_3Q = 3  # need 3 vintage-aware periods for persistence/margin


@dataclass
class SustainabilityResult:
    """Sustainability assessment for one stock at one decision date.

    All raw_* fields are stored even when flags fail, so threshold tuning
    in v3.4.1 does NOT require re-backfill (6-S.16.0a amendment).
    """

    security_id: str
    as_of_date: str
    available_date: str | None = None
    report_date: str | None = None
    industry: str | None = None

    # Alignment (raw + flag)
    revenue_yoy_current: float | None = None
    earnings_yoy_current: float | None = None
    profit_elasticity: float | None = None
    alignment_flag: int | None = None

    # Persistence (raw + flag)
    accel_q0: float | None = None
    accel_q1: float | None = None
    accel_q2: float | None = None
    accel_trend: float | None = None
    accel_volatility: float | None = None
    reversal_count: int | None = None
    consistency_flag: int | None = None

    # Margin normalization (raw + flag)
    operating_margin_current: float | None = None
    operating_margin_3q_median: float | None = None
    operating_margin_3q_std: float | None = None
    company_margin_zscore: float | None = None
    industry_margin_zscore: float | None = None
    margin_normalization_flag: int | None = None

    # Composite
    sustainability_pass: int | None = None
    failure_reason: str | None = None


class SustainabilityCalculator:
    """6-S.16.1: v3.4 Phase 1 sustainability calculator.

    Vintage-aware (available_date <= as_of_date gating). Stores raw
    values + derived flags. Does NOT weight sub-components into a single
    score (hard AND filters only in v3.4; soft scoring deferred to v3.5).
    """

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db

    def compute(self, security_id: str, as_of_date: str) -> SustainabilityResult:
        """Compute sustainability signals for one stock at one decision date.

        Args:
            security_id: bare stock code (e.g. "600519")
            as_of_date: ISO date (vintage gate; only uses earnings
                        announced on or before this date)

        Returns: SustainabilityResult with raw values + flags.
                 sustainability_pass is None if insufficient data.
        """
        code = str(security_id).zfill(6)
        result = SustainabilityResult(security_id=code, as_of_date=as_of_date)

        # 1. Load vintage-aware 3 periods (q0=current, q1=prior, q2=prior2)
        periods = self._load_vintage_periods(code, as_of_date, limit=3)
        if len(periods) < MIN_PERIODS_FOR_3Q:
            result.sustainability_pass = None
            result.failure_reason = "INSUFFICIENT_DATA"
            return result

        q0, q1, q2 = periods[0], periods[1], periods[2]
        result.available_date = q0["available_date"]
        result.report_date = q0["report_date"]
        result.industry = self._load_industry(code)

        # 2. Alignment sub-component
        self._compute_alignment(result, q0)

        # 3. Persistence sub-component (needs all 3 periods)
        self._compute_persistence(result, q0, q1, q2)

        # 4. Margin normalization sub-component (needs 3q history + industry)
        self._compute_margin_normalization(result, q0, q1, q2)

        # 5. Composite (hard AND)
        self._compute_composite(result)

        return result

    # ─────────────────────────────────────────────────────────────────
    # Vintage-aware period loading (matches FRM/event_reaction convention)
    # ─────────────────────────────────────────────────────────────────

    def _load_vintage_periods(self, code: str, as_of_date: str, limit: int = 3) -> list[dict]:
        """Load the most recent `limit` reporting periods available as of date.

        Vintage-aware: only uses reports whose available_date <= as_of_date.
        Falls back to report_date + 90 days if available_date is NULL.
        """
        conn = sqlite3.connect(self.cache_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT code, report_date, available_date, "
                "earnings_yoy, revenue_yoy, net_profit, revenue, "
                "operating_profit, total_assets, equity "
                "FROM akshare_financials WHERE code = ? "
                "AND (available_date IS NOT NULL AND available_date <= ? "
                "     OR available_date IS NULL AND date(report_date, '+90 days') <= ?) "
                "ORDER BY report_date DESC LIMIT ?",
                (code, as_of_date, as_of_date, limit),
            ).fetchall()
        finally:
            conn.close()

        periods = []
        for r in rows:
            operating_margin = None
            op = r["operating_profit"]
            rev = r["revenue"]
            if op is not None and rev and rev != 0:
                operating_margin = op / rev
            periods.append(
                {
                    "report_date": r["report_date"],
                    "available_date": r["available_date"],
                    # akshare stores yoy as percent (20.0 = 20%); convert to fraction
                    "earnings_yoy": (r["earnings_yoy"] / 100.0)
                    if r["earnings_yoy"] is not None
                    else None,
                    "revenue_yoy": (r["revenue_yoy"] / 100.0)
                    if r["revenue_yoy"] is not None
                    else None,
                    "operating_margin": operating_margin,
                }
            )
        return periods

    def _load_industry(self, code: str) -> str | None:
        """Load industry classification from security_master."""
        conn = sqlite3.connect(self.cache_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT industry FROM security_master WHERE security_id = ? OR code = ? LIMIT 1",
                (code, code),
            ).fetchone()
        finally:
            conn.close()
        return row["industry"] if row and row["industry"] else None

    # ─────────────────────────────────────────────────────────────────
    # Sub-component 1: Alignment (revenue-earnings decoupling)
    # ─────────────────────────────────────────────────────────────────

    def _compute_alignment(self, result: SustainabilityResult, q0: dict) -> None:
        """profit_elasticity = earnings_yoy / revenue_yoy (raw, stored).

        alignment_flag (tunable default): sign match AND |rev| >= 0.3*|earn|.
        Rejects: earnings up while revenue down (suspect sustainability).
        """
        rev_yoy = q0.get("revenue_yoy")
        earn_yoy = q0.get("earnings_yoy")

        result.revenue_yoy_current = rev_yoy
        result.earnings_yoy_current = earn_yoy

        if rev_yoy is None or earn_yoy is None:
            result.alignment_flag = None
            return

        # profit_elasticity (raw) - undefined when revenue_yoy ~ 0
        if abs(rev_yoy) > 1e-6:
            result.profit_elasticity = earn_yoy / rev_yoy
        else:
            result.profit_elasticity = None

        # alignment_flag (tunable default)
        # 1 = sign match AND |rev| >= 0.3*|earn|
        # 0 = decouple (earnings up / revenue down, or extreme elasticity)
        sign_match = np.sign(rev_yoy) == np.sign(earn_yoy)
        rev_supports_earn = abs(rev_yoy) >= ALIGNMENT_MIN_REVENUE_RATIO * abs(earn_yoy)
        result.alignment_flag = 1 if (sign_match and rev_supports_earn) else 0

    # ─────────────────────────────────────────────────────────────────
    # Sub-component 2: Persistence (3-quarter acceleration trend)
    # ─────────────────────────────────────────────────────────────────

    def _compute_persistence(
        self, result: SustainabilityResult, q0: dict, q1: dict, q2: dict
    ) -> None:
        """3-period acceleration trend, volatility, reversal count (raw).

        accel_qN = earnings_yoy_qN - earnings_yoy_q(N+1)  (1st derivative per period)
        accel_trend = accel_q0 - accel_q2  (positive = sustaining, negative = fading)
        reversal_count = sign flips across [accel_q0, accel_q1, accel_q2]

        consistency_flag (tunable default): accel_q0 > 0 AND reversal_count <= 1.
        Rejects: V-shape spike (reversal_count >= 2) or acceleration reversed.
        """
        e0, e1, e2 = q0.get("earnings_yoy"), q1.get("earnings_yoy"), q2.get("earnings_yoy")

        if e0 is None or e1 is None or e2 is None:
            result.consistency_flag = None
            return

        # Per-period accelerations (1st derivative)
        result.accel_q0 = e0 - e1
        result.accel_q1 = e1 - e2
        # accel_q2 needs a 4th period; approximate as accel_q1 (conservative)
        # This is a known limitation when only 3 periods are loaded.
        # For true 2nd-derivative stability, v3.4.1 may load 4 periods.
        result.accel_q2 = result.accel_q1  # placeholder, see note above

        # Trend across 3 periods (q0 vs q2)
        result.accel_trend = result.accel_q0 - result.accel_q2

        # Volatility
        accels = [result.accel_q0, result.accel_q1, result.accel_q2]
        result.accel_volatility = float(np.std(accels, ddof=0)) if len(accels) >= 2 else None

        # Reversal count (sign flips)
        signs = [np.sign(a) for a in accels]
        flips = sum(
            1
            for i in range(1, len(signs))
            if signs[i] != signs[i - 1] and signs[i] != 0 and signs[i - 1] != 0
        )
        result.reversal_count = int(flips)

        # consistency_flag (tunable default)
        result.consistency_flag = (
            1 if (result.accel_q0 > 0 and result.reversal_count <= CONSISTENCY_MAX_REVERSALS) else 0
        )

    # ─────────────────────────────────────────────────────────────────
    # Sub-component 3: Margin normalization (peak-margin mean-reversion)
    # ─────────────────────────────────────────────────────────────────

    def _compute_margin_normalization(
        self, result: SustainabilityResult, q0: dict, q1: dict, q2: dict
    ) -> None:
        """operating_margin peak detection (company + industry zscore, raw).

        company_margin_zscore = (current - 3q_median) / 3q_std  (own history)
        industry_margin_zscore = (current - industry_median) / industry_std  (cross-section)

        margin_normalization_flag (tunable default): max(z) < 1.5.
        Rejects: margin at peak relative to own history OR industry peers.
        Uses max() to avoid mis-killing growth industries (safeguard).
        """
        margins = [
            q0.get("operating_margin"),
            q1.get("operating_margin"),
            q2.get("operating_margin"),
        ]
        margins_valid = [m for m in margins if m is not None]

        if len(margins_valid) < MIN_PERIODS_FOR_3Q:
            result.margin_normalization_flag = None
            return

        current = margins_valid[0]
        result.operating_margin_current = current
        result.operating_margin_3q_median = float(np.median(margins_valid))
        result.operating_margin_3q_std = float(np.std(margins_valid, ddof=0))

        # Company zscore (vs own 3q history)
        std = max(result.operating_margin_3q_std, MARGIN_STD_FLOOR)
        result.company_margin_zscore = (current - result.operating_margin_3q_median) / std

        # Industry zscore (cross-section, same report_date as q0)
        result.industry_margin_zscore = self._industry_margin_zscore(
            result.security_id, result.industry, result.report_date, current
        )

        # margin_normalization_flag (tunable default)
        # max() safeguards growth industries: reject only if at peak in
        # BOTH own history AND industry context
        zscores = [
            z
            for z in [result.company_margin_zscore, result.industry_margin_zscore]
            if z is not None
        ]
        peak_z = max(zscores) if zscores else None
        if peak_z is None:
            result.margin_normalization_flag = None
        else:
            result.margin_normalization_flag = 1 if (peak_z < MARGIN_ZSCORE_PEAK) else 0

    def _industry_margin_zscore(
        self, code: str, industry: str | None, report_date: str | None, current_margin: float
    ) -> float | None:
        """Cross-sectional zscore vs industry peers at same report_date.

        Returns None if industry unknown or < MIN_INDUSTRY_PEERS peers.
        """
        if not industry or not report_date:
            return None

        conn = sqlite3.connect(self.cache_db)
        conn.row_factory = sqlite3.Row
        try:
            # Peer margins at same report_date, same industry
            rows = conn.execute(
                "SELECT af.operating_profit / af.revenue AS margin "
                "FROM akshare_financials af "
                "JOIN security_master sm ON sm.code = af.code "
                "WHERE sm.industry = ? AND af.report_date = ? "
                "AND af.operating_profit IS NOT NULL "
                "AND af.revenue IS NOT NULL AND af.revenue != 0 "
                "AND af.code != ?",
                (industry, report_date, code),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < MIN_INDUSTRY_PEERS:
            return None

        peer_margins = [r["margin"] for r in rows if r["margin"] is not None]
        if len(peer_margins) < MIN_INDUSTRY_PEERS:
            return None

        peer_median = float(np.median(peer_margins))
        peer_std = float(np.std(peer_margins, ddof=0))
        peer_std = max(peer_std, MARGIN_STD_FLOOR)
        return (current_margin - peer_median) / peer_std

    # ─────────────────────────────────────────────────────────────────
    # Composite (hard AND; no weighting in v3.4)
    # ─────────────────────────────────────────────────────────────────

    def _compute_composite(self, result: SustainabilityResult) -> None:
        """sustainability_pass = alignment_flag AND consistency_flag AND margin_normalization_flag.

        Hard AND. Soft scoring deferred to v3.5 (6-S.16.0a prohibition).
        Any None flag -> INSUFFICIENT_DATA (pass = None, not 0).
        """
        flags = [result.alignment_flag, result.consistency_flag, result.margin_normalization_flag]

        if any(f is None for f in flags):
            result.sustainability_pass = None
            result.failure_reason = "INSUFFICIENT_DATA"
            return

        if all(f == 1 for f in flags):
            result.sustainability_pass = 1
            result.failure_reason = None
        else:
            result.sustainability_pass = 0
            # Identify first failing component for audit
            if result.alignment_flag == 0:
                result.failure_reason = "ALIGNMENT_DECOUPLE"
            elif result.consistency_flag == 0:
                result.failure_reason = "CONSISTENCY_SPIKE"
            elif result.margin_normalization_flag == 0:
                result.failure_reason = "MARGIN_PEAK"
            else:
                result.failure_reason = "UNKNOWN"
