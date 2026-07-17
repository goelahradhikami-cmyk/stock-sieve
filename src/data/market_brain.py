"""
MarketBrain — Market state classifier and regime detector.

Classifies market into: bull / bear / crisis / rotation
Fills market_regime_snapshots table in evaluation_db.

Uses indicators:
  - Index trend (MA comparison, rate of change)
  - Volatility (VIX proxy from CSI 300 options or historical vol)
  - Liquidity (turnover, volume trends)
  - Breadth (advance/decline ratio)
  - Valuation (PE/PB percentiles)
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import numpy as np


@dataclass
class RegimeResult:
    """Market regime classification output."""
    date: str
    regime_type: str          # bull / bear / crisis / rotation / unknown
    risk_score: float         # 0-100, higher = riskier
    growth_env_score: float
    value_env_score: float
    momentum_env_score: float
    defensive_env_score: float
    liquidity_score: float
    market_pe_percentile: Optional[float] = None
    market_pb_percentile: Optional[float] = None
    indicators: dict = None

    def __post_init__(self):
        if self.indicators is None:
            self.indicators = {}


class MarketBrain:
    """Classifies current market regime and computes environment scores."""

    # ── Regime thresholds ───────────────────────────────

    # Bull: uptrend, low vol, high liquidity
    BULL_MA_TREND = 0.03        # 3% above MA60
    BULL_VOL_MAX = 0.20         # annualized vol < 20%
    BULL_LIQUIDITY_MIN = 60

    # Bear: downtrend, moderate vol
    BEAR_MA_TREND = -0.03
    BEAR_VOL_MAX = 0.35

    # Crisis: extreme vol, liquidity collapse
    CRISIS_VOL_MIN = 0.35
    CRISIS_LIQUIDITY_MAX = 30

    def __init__(self, db_path: str = "data/cache.db"):
        self.db_path = db_path

    def classify(self, index_data: pd.DataFrame) -> RegimeResult:
        """Classify market regime from index price/volume data.

        Args:
            index_data: DataFrame with columns: date, close, volume, pe_ttm, pb
                        (typically CSI 300 data)

        Returns:
            RegimeResult with regime_type and environment scores.
        """
        if index_data.empty or len(index_data) < 60:
            return RegimeResult(
                date=date.today().isoformat(),
                regime_type="unknown",
                risk_score=50.0,
                growth_env_score=50.0,
                value_env_score=50.0,
                momentum_env_score=50.0,
                defensive_env_score=50.0,
                liquidity_score=50.0,
            )

        df = index_data.copy()
        df = df.sort_values("date")

        # ── 1. Compute derived indicators ────────────────

        # Trend: current close vs 60-day MA
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        current_close = df["close"].iloc[-1]
        ma60 = df["ma60"].iloc[-1]
        trend_pct = (current_close / ma60 - 1) if pd.notna(ma60) and ma60 > 0 else 0

        # Momentum: 20-day rate of change
        if len(df) >= 20:
            roc_20d = (df["close"].iloc[-1] / df["close"].iloc[-20] - 1)
        else:
            roc_20d = 0

        # Volatility: 20-day annualized
        df["ret"] = df["close"].pct_change()
        vol_20d = df["ret"].tail(20).std() * math.sqrt(252) if len(df) >= 20 else 0.25

        # Max drawdown (recent)
        peak = df["close"].rolling(60).max()
        dd = (df["close"] - peak) / peak
        max_dd = dd.tail(60).min() if len(dd) >= 60 else 0

        # Liquidity: average turnover trend
        if "turnover" in df.columns:
            avg_turnover = df["turnover"].tail(20).mean()
            turnover_ratio = avg_turnover / df["turnover"].tail(120).mean() if len(df) >= 120 else 1.0
        else:
            # Use volume as proxy
            avg_vol = df["volume"].tail(20).mean() if "volume" in df.columns else 1
            avg_vol_120 = df["volume"].tail(120).mean() if "volume" in df.columns and len(df) >= 120 else avg_vol
            turnover_ratio = avg_vol / avg_vol_120 if avg_vol_120 > 0 else 1.0

        # ── 2. Determine regime ──────────────────────────

        regime = "rotation"  # default

        if vol_20d >= self.CRISIS_VOL_MIN:
            regime = "crisis"
        elif trend_pct >= self.BULL_MA_TREND and vol_20d <= self.BULL_VOL_MAX:
            regime = "bull"
        elif trend_pct <= self.BEAR_MA_TREND and vol_20d <= self.BEAR_VOL_MAX:
            regime = "bear"

        # ── 3. Compute environment scores (0-100) ────────

        # Risk score: composite of vol, drawdown, trend reversal risk
        risk_score = self._normalize(vol_20d * 100, 30, 10)
        risk_score += self._normalize(abs(max_dd) * 100, 20, 5)
        risk_score = min(100, risk_score / 2)

        # Growth environment: favorable when uptrend + low vol
        growth_env = 50 + trend_pct * 500 - vol_20d * 50
        growth_env = max(0, min(100, growth_env))

        # Value environment: favorable when downtrend (bargains) + moderate vol
        value_env = 50 - trend_pct * 300 + (0.25 - vol_20d) * 100
        value_env = max(0, min(100, value_env))

        # Momentum environment: favorable when trending strongly
        momentum_env = 50 + abs(trend_pct) * 400 - vol_20d * 80
        momentum_env = max(0, min(100, momentum_env))

        # Defensive environment: favorable when volatile/downtrend
        defensive_env = 50 + vol_20d * 150 - trend_pct * 200
        defensive_env = max(0, min(100, defensive_env))

        # Liquidity score
        liquidity_score = min(100, max(0, turnover_ratio * 50))

        # ── 4. PE/PB percentile (if data available) ──────
        pe_pct = None
        pb_pct = None
        if "pe_ttm" in df.columns and df["pe_ttm"].notna().any():
            pe_current = df["pe_ttm"].iloc[-1]
            pe_pct = (df["pe_ttm"] < pe_current).mean()
        if "pb" in df.columns and df["pb"].notna().any():
            pb_current = df["pb"].iloc[-1]
            pb_pct = (df["pb"] < pb_current).mean()

        return RegimeResult(
            date=date.today().isoformat(),
            regime_type=regime,
            risk_score=round(risk_score, 1),
            growth_env_score=round(growth_env, 1),
            value_env_score=round(value_env, 1),
            momentum_env_score=round(momentum_env, 1),
            defensive_env_score=round(defensive_env, 1),
            liquidity_score=round(liquidity_score, 1),
            market_pe_percentile=round(pe_pct, 3) if pe_pct is not None else None,
            market_pb_percentile=round(pb_pct, 3) if pb_pct is not None else None,
            indicators={
                "trend_pct": round(trend_pct, 4),
                "roc_20d": round(roc_20d, 4),
                "vol_20d": round(vol_20d, 4),
                "max_dd_60d": round(max_dd, 4) if max_dd else None,
                "turnover_ratio": round(turnover_ratio, 4),
            }
        )

    def _normalize(self, value: float, reference: float, scale: float) -> float:
        """Normalize a value to 0-100 score relative to reference."""
        if pd.isna(value):
            return 50.0
        return min(100, max(0, (value / reference) * 50 * scale))

    def should_hysteresis(self, current: str, previous: str, weeks: int = 2) -> bool:
        """Check if regime change needs hysteresis (2 weeks confirmation)."""
        return current != previous and weeks < 2
