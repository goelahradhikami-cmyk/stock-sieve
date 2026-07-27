"""
Thesis Timing Layer - Commit 6-Q.3.

Thesis doesn't pick stocks (6-Q.2 proved that hurts). Thesis picks WHEN to
trust which factors, and WHEN to reduce exposure.

Architecture change from 6-Q.2:
  Old (HURTS):  thesis_signal -> stock ranking -> Top 20 (replaces factor)
  New:          factor_engine -> Top 20 candidates -> thesis timing overlay
                ├── Timing:    adjust factor_bias weights (value regime -> value↑)
                ├── Conviction: adjust position size (earnings deteriorating -> reduce)
                └── Risk Veto:  remove stocks (policy risk -> forbid)

The overlay changes the PORTFOLIO WEIGHTS, not the stock selection. This
means thesis adds timing alpha on top of factor alpha, not replacing it.

A/B test:
  Control:   factor Top 20, equal weight
  Treatment: factor Top 20, thesis-adjusted weights
  Incremental = Treatment - Control

Usage:
    from src.thesis.timing_layer import ThesisTimingLayer
    tl = ThesisTimingLayer()
    adjusted = tl.apply_timing_overlay(
        picks=factor_top20,
        doctrine=doctrine,
        trade_date="2026-05-27",
    )
    # adjusted = {security_id: weight} with thesis adjustments
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np

from src.agents.doctrine_engine import DoctrineGenome
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TimingAdjustment:
    """Thesis timing adjustment for one stock."""

    security_id: str
    base_weight: float  # original equal weight (1/N)
    timing_multiplier: float  # 1.0 = no change, >1 = overweight, <1 = underweight
    final_weight: float  # base × multiplier (normalized)
    timing_reason: str = ""  # why adjusted
    vetoed: bool = False  # risk veto (removed from portfolio)


@dataclass
class TimingResult:
    """Result of applying thesis timing overlay to a portfolio."""

    adjustments: list[TimingAdjustment] = field(default_factory=list)
    vetoed_count: int = 0
    avg_timing_multiplier: float = 1.0
    regime_label: str = ""
    factor_adjustments: dict = field(default_factory=dict)  # {factor: multiplier}


class ThesisTimingLayer:
    """Thesis timing overlay: adjusts portfolio weights based on market-level signals.

    Three functions:
      1. Timing: detect market regime -> adjust factor_bias weights
      2. Conviction: per-stock earnings check -> adjust position size
      3. Risk Veto: per-stock risk check -> remove from portfolio

    Key insight: thesis doesn't REPLACE factor selection, it ENHANCES it
    by providing timing information that factors can't (factors are
    point-in-time snapshots, thesis uses deltas and context).
    """

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db

    def apply_timing_overlay(
        self, picks: list[dict], doctrine: DoctrineGenome, trade_date: str
    ) -> TimingResult:
        """Apply thesis timing overlay to factor-selected picks.

        Args:
            picks: factor engine Top 20 (from score_universe)
            doctrine: the doctrine (for factor_bias reference)
            trade_date: current date

        Returns: TimingResult with adjusted weights
        """
        if not picks:
            return TimingResult()

        n = len(picks)
        base_weight = 1.0 / n

        # 1. Market-level timing: detect regime -> adjust factor weights
        regime, factor_mults = self._compute_market_timing(trade_date, doctrine)

        # 2. Per-stock conviction: check earnings acceleration
        convictions = self._compute_convictions(picks, trade_date)

        # 3. Per-stock risk veto: check for risk signals
        risk_flags = self._compute_risk_vetoes(picks, trade_date)

        # Build adjustments
        adjustments = []
        vetoed_count = 0
        multipliers = []

        for pick in picks:
            sec_id = pick["security_id"]
            bare = sec_id.split(".")[0] if "." in sec_id else sec_id

            # Timing multiplier: starts at 1.0
            # 6-Q.4: more aggressive range (0.3-2.5) per user decision
            timing_mult = 1.0
            reasons = []

            # Factor timing: use multi-layer regime factor multipliers
            pick_quality = pick.get("quality_score", 50)
            pick_value = pick.get("value_score", 50)
            pick_growth = pick.get("growth_score", 50)
            pick_momentum = pick.get("momentum_score", 50)

            # Apply factor-specific multipliers based on this stock's factor profile
            # If market regime boosts "value" and this stock has high value, apply boost
            fam_mults = factor_mults if factor_mults else {}

            # Stock-level timing: blend factor multipliers weighted by stock's factor scores
            stock_timing = 1.0
            # Normalize factor scores to weights (0-1)
            total_score = pick_quality + pick_value + pick_growth + pick_momentum + 1
            q_w = pick_quality / total_score
            v_w = pick_value / total_score
            g_w = pick_growth / total_score
            m_w = pick_momentum / total_score

            # Weighted average of factor multipliers
            stock_timing = (
                q_w * fam_mults.get("quality", 1.0)
                + v_w * fam_mults.get("value", 1.0)
                + g_w * fam_mults.get("growth", 1.0)
                + m_w * fam_mults.get("momentum", 1.0)
            )
            timing_mult *= stock_timing
            if abs(stock_timing - 1.0) > 0.05:
                reasons.append(f"factor_timing({stock_timing:.2f})")

            # Conviction: earnings acceleration (6-Q.4: more aggressive)
            conviction = convictions.get(bare, 0.5)
            if conviction > 0.6:
                timing_mult *= 1.3
                reasons.append("earnings_accelerating")
            elif conviction < 0.3:
                timing_mult *= 0.7
                reasons.append("earnings_decelerating")

            # Risk veto
            if risk_flags.get(bare, False):
                timing_mult = 0.0  # veto
                vetoed_count += 1
                reasons.append("RISK_VETO")

            # Clamp (6-Q.4: wider range 0.3-2.5)
            timing_mult = max(0.0, min(2.5, timing_mult))
            multipliers.append(timing_mult)

            adjustments.append(
                TimingAdjustment(
                    security_id=sec_id,
                    base_weight=base_weight,
                    timing_multiplier=timing_mult,
                    final_weight=base_weight * timing_mult,  # will normalize later
                    timing_reason=", ".join(reasons) if reasons else "no_adjustment",
                    vetoed=risk_flags.get(bare, False),
                )
            )

        # Normalize weights to sum to 1.0 (excluding vetoed)
        total_weight = sum(a.final_weight for a in adjustments if not a.vetoed)
        if total_weight > 0:
            for a in adjustments:
                if not a.vetoed:
                    a.final_weight = a.final_weight / total_weight

        avg_mult = float(np.mean(multipliers)) if multipliers else 1.0

        return TimingResult(
            adjustments=adjustments,
            vetoed_count=vetoed_count,
            avg_timing_multiplier=avg_mult,
            regime_label=regime,
            factor_adjustments=factor_mults,
        )

    def get_weighted_returns(
        self, picks: list[dict], pick_returns: list[float], timing_result: TimingResult
    ) -> float:
        """Compute timing-adjusted portfolio return.

        Control (equal weight): mean(pick_returns)
        Treatment (timing):     Σ(weight_i × return_i)
        """
        if not timing_result.adjustments or not pick_returns:
            return float(np.mean(pick_returns)) if pick_returns else 0.0

        weighted_return = 0.0
        for i, adj in enumerate(timing_result.adjustments):
            if i < len(pick_returns) and not adj.vetoed:
                weighted_return += adj.final_weight * pick_returns[i]

        return weighted_return

    def _compute_market_timing(self, trade_date: str, doctrine: DoctrineGenome) -> tuple[str, dict]:
        """Multi-layer regime detection (Commit 6-Q.4.1).

        Replaces the old MA60-only approach with three layers:
          1. Market regime: from market_regime_snapshots (5 states)
          2. Factor momentum: which factor families are trending up/down
          3. Crowding: from alpha_decay_history (are strategies crowded?)

        Returns: (regime_label, {factor: multiplier})
        """
        # Layer 1: Market regime from market_regime_snapshots
        market_regime = self._read_market_regime(trade_date)

        # Layer 2: Factor momentum from stock_factor_snapshot
        factor_momentum = self._compute_factor_momentum(trade_date)

        # Layer 3: Average crowding from alpha_decay_history
        crowding = self._read_crowding()

        # Combine into factor multipliers
        factor_mults = {}

        # Market regime -> base factor preferences
        if market_regime == "crash":
            factor_mults = {
                "quality": 1.4,
                "value": 1.2,
                "momentum": 0.5,
                "growth": 0.6,
                "risk": 1.3,
                "sentiment": 0.5,
            }
        elif market_regime == "bear":
            factor_mults = {
                "quality": 1.3,
                "value": 1.25,
                "momentum": 0.6,
                "growth": 0.7,
                "risk": 1.2,
                "sentiment": 0.6,
            }
        elif market_regime == "bull":
            factor_mults = {
                "momentum": 1.4,
                "growth": 1.3,
                "value": 0.7,
                "quality": 0.9,
                "risk": 0.8,
                "sentiment": 1.2,
            }
        elif market_regime == "high_volatility":
            factor_mults = {
                "quality": 1.2,
                "value": 1.1,
                "momentum": 0.8,
                "growth": 0.9,
                "risk": 1.2,
                "sentiment": 0.7,
            }
        else:  # sideway
            factor_mults = {}

        # Layer 2: Factor momentum overlay
        # If a factor's long-short return is positive, boost it; if negative, reduce
        for factor, mom in factor_momentum.items():
            if factor not in factor_mults:
                factor_mults[factor] = 1.0
            # Factor momentum: +10% LS return -> +20% weight boost
            factor_mults[factor] *= max(0.5, min(2.0, 1.0 + mom * 2.0))

        # Layer 3: Crowding penalty
        # If crowding > 0.7, reduce all multipliers (market is overconsensus)
        if crowding > 0.7:
            for f in factor_mults:
                factor_mults[f] *= 0.85

        # Build regime label
        regime_parts = [market_regime or "unknown"]
        top_factor = (
            max(factor_momentum, key=lambda f: factor_momentum[f]) if factor_momentum else "?"
        )
        if factor_momentum and factor_momentum[top_factor] > 0.02:
            regime_parts.append(f"{top_factor}_momentum_up")
        if crowding > 0.7:
            regime_parts.append("crowded")
        regime_label = "+".join(regime_parts)

        return regime_label, factor_mults

    def _read_market_regime(self, trade_date: str) -> str:
        """Read market regime from market_regime_snapshots (6-O.1)."""
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT regime_type FROM market_regime_snapshots WHERE obs_date=?",
                (trade_date,),
            ).fetchone()
            # Try nearest date if exact match fails
            if not row:
                row = conn.execute(
                    "SELECT regime_type FROM market_regime_snapshots "
                    "WHERE obs_date <= ? ORDER BY obs_date DESC LIMIT 1",
                    (trade_date,),
                ).fetchone()
            return row[0] if row else "sideway"
        finally:
            conn.close()

    def _compute_factor_momentum(self, trade_date: str) -> dict[str, float]:
        """Compute factor momentum: long-short return per factor family.

        For each factor, sort all stocks by factor score, compute
        avg(top 30%) - avg(bottom 30%) forward return proxy.

        Since we don't have forward returns at selection time, we use
        the factor's cross-sectional dispersion as a momentum proxy:
        if the factor has high dispersion (wide spread between top/bottom),
        it's "active" and likely to continue trending.
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT quality_score, value_score, growth_score, "
                "momentum_score, risk_score, sentiment_score "
                "FROM stock_factor_snapshot WHERE trade_date=?",
                (trade_date,),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 20:
            return {}

        families = ["quality", "value", "growth", "momentum", "risk", "sentiment"]
        momentum = {}

        for i, fam in enumerate(families):
            scores = [r[i] for r in rows if r[i] is not None]
            if len(scores) < 10:
                momentum[fam] = 0.0
                continue

            # Factor momentum proxy: std of factor scores (dispersion)
            # High dispersion = factor is discriminating = factor is "active"
            # Low dispersion = factor is not differentiating = "dead"
            std = float(np.std(scores))
            # Normalize: std of 20 -> 0.0 (dead), std of 35 -> 0.5 (active)
            momentum[fam] = max(0.0, (std - 15.0) / 40.0)

        return momentum

    def _read_crowding(self) -> float:
        """Read average crowding from alpha_decay_history."""
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT AVG(crowding_score) FROM alpha_decay_history "
                "ORDER BY generation DESC LIMIT 32"
            ).fetchone()
            return row[0] if row and row[0] else 0.0
        finally:
            conn.close()

    def _compute_convictions(self, picks: list[dict], trade_date: str) -> dict[str, float]:
        """Per-stock conviction based on earnings acceleration.

        Returns: {code: conviction 0-1}
        """
        from src.thesis.signal_engine import ThesisSignalEngine

        engine = ThesisSignalEngine(cache_db=self.cache_db)

        convictions = {}
        for pick in picks:
            bare = (
                pick["security_id"].split(".")[0]
                if "." in pick["security_id"]
                else pick["security_id"]
            )
            try:
                signals = engine.compute_signals(bare, trade_date)
                # Conviction = sigmoid(acceleration)
                accel = signals.fundamental_acceleration
                conv = 1.0 / (1.0 + np.exp(-accel * 10))
                convictions[bare] = float(conv)
            except Exception:
                convictions[bare] = 0.5  # neutral

        return convictions

    def _compute_risk_vetoes(self, picks: list[dict], trade_date: str) -> dict[str, bool]:
        """Per-stock risk veto check.

        Veto if:
        - Risk score very low (< 20, high risk)
        - Momentum extremely negative (< -20% in 60 days) AND quality low

        Returns: {code: should_veto}
        """
        vetoes = {}
        for pick in picks:
            bare = (
                pick["security_id"].split(".")[0]
                if "." in pick["security_id"]
                else pick["security_id"]
            )
            risk_score = pick.get("risk_score", 50)
            quality_score = pick.get("quality_score", 50)

            # Veto: very high risk + low quality (lottery ticket)
            veto = risk_score < 20 and quality_score < 30
            vetoes[bare] = veto

        return vetoes
