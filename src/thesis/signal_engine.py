"""
Thesis Signal Engine - Commit 6-Q.1.

Generates non-factor signals that represent genuine investment judgment,
not just factor exposure. These signals answer "why buy THIS stock NOW?"
not just "is this stock cheap/quality/momentum".

Three signals (per user decision A+B+C):
  1. fundamental_acceleration: Δ(revenue_growth) + Δ(earnings_growth) + Δ(ROE)
     - Is the company's fundamentals ACCELERATING (not just good)?
  2. mispricing_gap: earnings_acceleration - price_performance
     - Are fundamentals improving but price hasn't caught up?
  3. catalyst_proxy: fixed_asset_growth + capex_change
     - Is the company investing in expansion (capacity catalyst)?

Key design: these signals use data NOT in the 6 factor families.
- Factors use point-in-time snapshots (current PE/ROE/momentum)
- Thesis signals use DELTAS (change in growth, change in assets)
  which are orthogonal to level-based factors.

Usage:
    from src.thesis.signal_engine import ThesisSignalEngine
    engine = ThesisSignalEngine()
    signals = engine.compute_signals("600519", "2026-05-27")
    # signals = {"fundamental_acceleration": 0.15, "mispricing_gap": 0.08, ...}
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ThesisSignals:
    """Non-factor investment signals for a single stock."""

    code: str
    fundamental_acceleration: float = 0.0  # Δ(rev growth) + Δ(earnings growth) + Δ(ROE)
    mispricing_gap: float = 0.0  # earnings_accel - price_performance
    catalyst_proxy: float = 0.0  # asset growth + capex change
    thesis_score: float = 0.0  # weighted composite
    confidence: float = 0.0  # 0-1, based on data completeness

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "fundamental_acceleration": self.fundamental_acceleration,
            "mispricing_gap": self.mispricing_gap,
            "catalyst_proxy": self.catalyst_proxy,
            "thesis_score": self.thesis_score,
            "confidence": self.confidence,
        }


class ThesisSignalEngine:
    """Computes non-factor thesis signals from multi-period financials.

    Data source: akshare_financials table (9 quarters, populated by
    backfill_akshare.py) + market_index_daily for price performance.
    """

    # Signal weights (configurable per doctrine in v2)
    W_ACCELERATION = 0.4
    W_MISPRICING = 0.4
    W_CATALYST = 0.2

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db

    def compute_signals(
        self, code: str, trade_date: str, price_performance: float | None = None
    ) -> ThesisSignals:
        """Compute all thesis signals for a stock.

        Args:
            code: bare stock code (e.g. "600519")
            trade_date: ISO date (for determining which financial periods to use)
            price_performance: optional forward price return (for mispricing_gap).
                              If None, looks up from local K-line.

        Returns: ThesisSignals with 3 signals + composite thesis_score
        """
        code = str(code).zfill(6)

        # 1. Fundamental acceleration
        accel, accel_confidence = self._compute_acceleration(code, trade_date)

        # 2. Mispricing gap
        if price_performance is None:
            price_perf = self._compute_price_performance(code, trade_date)
        else:
            price_perf = price_performance
        mispricing = accel - price_perf if accel is not None else 0.0

        # 3. Catalyst proxy
        catalyst, catalyst_confidence = self._compute_catalyst(code, trade_date)

        # Composite thesis score
        thesis_score = (
            self.W_ACCELERATION * (accel or 0)
            + self.W_MISPRICING * mispricing
            + self.W_CATALYST * (catalyst or 0)
        )

        # Confidence: based on data availability
        confidence = (accel_confidence + catalyst_confidence) / 2

        return ThesisSignals(
            code=code,
            fundamental_acceleration=accel or 0.0,
            mispricing_gap=mispricing,
            catalyst_proxy=catalyst or 0.0,
            thesis_score=thesis_score,
            confidence=confidence,
        )

    def compute_signals_batch(self, codes: list[str], trade_date: str) -> dict[str, ThesisSignals]:
        """Compute thesis signals for a batch of stocks.

        Returns: {code: ThesisSignals}
        """
        results = {}
        for code in codes:
            try:
                signals = self.compute_signals(code, trade_date)
                results[code] = signals
            except Exception as e:
                logger.debug("thesis_signal: %s failed: %s", code, e)
        return results

    def _compute_acceleration(self, code: str, trade_date: str) -> tuple[float | None, float]:
        """Fundamental acceleration: change in growth rates across quarters.

        acceleration = Δ(revenue_yoy) + Δ(earnings_yoy) + Δ(ROE)

        Uses the last 4 quarters of akshare_financials to compute deltas.
        Returns: (acceleration_score, confidence 0-1)
        """
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT report_date, revenue_yoy, earnings_yoy, roe "
                "FROM akshare_financials WHERE code=? "
                "ORDER BY report_date DESC LIMIT 4",
                (code,),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 3:
            return None, 0.0

        # Compute deltas: latest - previous (improvement in growth rate)
        rev_yoy = [r[1] for r in rows if r[1] is not None]
        earn_yoy = [r[2] for r in rows if r[2] is not None]
        roes = [r[3] for r in rows if r[3] is not None]

        deltas = []
        if len(rev_yoy) >= 2:
            deltas.append((rev_yoy[0] - rev_yoy[-1]) / 100.0)  # Δ revenue growth
        if len(earn_yoy) >= 2:
            deltas.append((earn_yoy[0] - earn_yoy[-1]) / 100.0)  # Δ earnings growth
        if len(roes) >= 2:
            deltas.append(roes[0] - roes[-1])  # Δ ROE (absolute)

        if not deltas:
            return None, 0.0

        acceleration = float(np.mean(deltas))
        confidence = min(1.0, len(rows) / 4.0)
        return acceleration, confidence

    def _compute_price_performance(self, code: str, trade_date: str) -> float:
        """Compute trailing 60-day price return for mispricing gap."""
        from datetime import date, timedelta

        from src.data.local_provider import LocalDataProvider

        local = LocalDataProvider()
        end_date = trade_date
        start_date = (date.fromisoformat(trade_date) - timedelta(days=90)).isoformat()

        try:
            kline = local.get_daily_kline(code, start_date, end_date)
            if kline is not None and not kline.empty and len(kline) >= 2:
                close = kline["close"].values
                return float((close[-1] - close[0]) / close[0])
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)
        return 0.0

    def _compute_catalyst(self, code: str, trade_date: str) -> tuple[float | None, float]:
        """Catalyst proxy: asset growth + equity change (capacity expansion signal).

        Uses balance sheet data from akshare_financials:
        - total_assets growth = capacity expansion
        - equity growth = reinvestment

        Returns: (catalyst_score, confidence 0-1)
        """
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT report_date, total_assets, equity "
                "FROM akshare_financials WHERE code=? "
                "ORDER BY report_date DESC LIMIT 4",
                (code,),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 2:
            return None, 0.0

        # Compute asset growth rate (latest vs 4 quarters ago)
        latest_assets = rows[0][1]
        past_assets = rows[-1][1]
        if latest_assets and past_assets and past_assets > 0:
            asset_growth = (latest_assets - past_assets) / past_assets
        else:
            asset_growth = 0.0

        # Equity growth
        latest_equity = rows[0][2]
        past_equity = rows[-1][2]
        if latest_equity and past_equity and past_equity > 0:
            equity_growth = (latest_equity - past_equity) / past_equity
        else:
            equity_growth = 0.0

        # Catalyst = average of asset + equity growth (capacity expansion signal)
        catalyst = (asset_growth + equity_growth) / 2
        confidence = min(1.0, len(rows) / 4.0)
        return float(catalyst), confidence
