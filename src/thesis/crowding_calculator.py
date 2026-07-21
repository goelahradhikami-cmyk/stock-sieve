"""
Crowding Calculator - Commit 6-S.17.4 (v3.5.1 Phase 1).

Computes crowding diagnostic features for Mechanism Identification
(H3-A vs H3-B fork). This is NOT an alpha factor and NOT a gate.

The question v3.5.1 answers: when the uncertainty zone appears, is
the market already crowded? If yes -> H3-B (crowding avoidance). If
no -> H3-A (uncertainty premium).

RS is NOT rehabilitated as a gate (v3.2.2 disproved). RS-like measures
(momentum, volume) are crowding DIAGNOSTICS only:
  WRONG (v3.2.2): RS_high -> BUY (RS as alpha signal)
  RIGHT (v3.5.1): RS_high -> possibly_crowded (RS as control variable)

Four feature groups (all vintage-safe, from TDX kline BEFORE trade_date):
  momentum:   return_20d, return_60d
  liquidity:  turnover_percentile, volume_ratio
  volatility: realized_vol_20d
  attention:  abnormal_volume, price_gap

Design principle (6-S.17.3 freeze):
  STORE RAW FEATURES, not just composite. Different crowding sources
  may drive H3-B differently. Raw values enable post-hoc decomposition.

  crowding_score_v1 is DIAGNOSTIC ONLY (equal-weight zscore sum).
  Weighted/PCA versions deferred to v3.6 if H3-B is validated.

Vintage safety (critical):
  All features computed from TDX kline data BEFORE trade_date. No
  lookahead. Uses same LocalDataProvider source as event_reaction.py.

Usage:
    from src.thesis.crowding_calculator import CrowdingCalculator
    calc = CrowdingCalculator()
    result = calc.compute("600519", "2024-03-13")
    # result.crowding_score_v1 = 1.35 (high crowding)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from src.data.local_provider import LocalDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Lookback windows (in trading days, BEFORE trade_date - vintage-safe)
LOOKBACK_20D = 20
LOOKBACK_60D = 60
MIN_HISTORY_DAYS = 25  # need >= 20d + buffer for vol/volume stats


@dataclass
class CrowdingResult:
    """Crowding diagnostic for one stock at one decision date.

    All raw_* fields are stored even when composite is computed, so
    post-hoc decomposition (which crowding source drives H3-B?) is
    possible without re-backfill.
    """
    security_id: str
    trade_date: str

    # Momentum crowding (raw)
    return_20d: Optional[float] = None
    return_60d: Optional[float] = None

    # Liquidity crowding (raw)
    turnover_percentile: Optional[float] = None
    volume_ratio: Optional[float] = None

    # Volatility crowding (raw)
    realized_vol_20d: Optional[float] = None

    # Attention crowding (raw)
    abnormal_volume: Optional[float] = None
    price_gap: Optional[float] = None

    # Control variables
    market_cap: Optional[float] = None
    float_mcap: Optional[float] = None

    # Composite (diagnostic only)
    crowding_score_v1: Optional[float] = None


class CrowdingCalculator:
    """6-S.17.4: v3.5.1 Phase 1 crowding calculator.

    Vintage-aware (all features from data BEFORE trade_date). Stores
    raw features + diagnostic composite. Does NOT gate or score
    candidates - this is a CONTROL variable for mechanism identification.
    """

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db
        self.local = LocalDataProvider()

    def compute(self, security_id: str, trade_date: str) -> CrowdingResult:
        """Compute crowding features for one stock at one decision date.

        Args:
            security_id: bare stock code (e.g. "600519")
            trade_date: ISO date (vintage gate; all features from BEFORE this date)

        Returns: CrowdingResult with raw features + diagnostic composite.
        """
        code = str(security_id).zfill(6)
        result = CrowdingResult(security_id=code, trade_date=trade_date)

        # Load TDX kline (need 60 trading days BEFORE trade_date for return_60d)
        # Add buffer for calendar lookup
        kline = self._load_kline_before(code, trade_date, days=LOOKBACK_60D + 10)
        if kline is None or len(kline) < MIN_HISTORY_DAYS:
            logger.debug("crowding: insufficient kline for %s @ %s (got %d rows)",
                         code, trade_date, len(kline) if kline is not None else 0)
            return result

        # ─── Momentum crowding ───
        self._compute_momentum(result, kline, trade_date)

        # ─── Volatility crowding ───
        self._compute_volatility(result, kline, trade_date)

        # ─── Liquidity + Attention crowding (volume-based) ───
        self._compute_volume_features(result, kline, trade_date)

        # ─── Control variables (market_cap, float_mcap) ───
        self._load_market_cap(result, code)

        # ─── Composite (diagnostic only, equal-weight zscore) ───
        # NOTE: zscore is cross-sectional and computed in the backfill script
        # (needs all stocks at same trade_date). Here we leave it None;
        # the backfill computes the composite after all raw features are stored.
        # For single-stock compute(), composite is not available.

        return result

    # ─────────────────────────────────────────────────────────────────
    # Kline loading (vintage-safe: only data BEFORE trade_date)
    # ─────────────────────────────────────────────────────────────────

    def _load_kline_before(self, code: str, trade_date: str,
                           days: int) -> Optional[pd.DataFrame]:
        """Load TDX kline ending BEFORE trade_date.

        Uses LocalDataProvider (same source as event_reaction.py).
        Returns DataFrame sorted by date ascending, last `days` rows
        before trade_date.
        """
        # Load a wide window and filter
        # TDX .day files are fast to read; load 1 year before trade_date
        start = self._offset_date(trade_date, -365)
        df = self.local.get_daily_kline(code, start_date=start, end_date=trade_date)
        if df.empty:
            return None
        # Filter to STRICTLY BEFORE trade_date (vintage-safe)
        df = df[df["date"] < pd.Timestamp(trade_date)].sort_values("date")
        return df.tail(days) if len(df) >= days else df

    def _offset_date(self, date_str: str, offset_days: int) -> str:
        """Simple calendar offset (not trading-day-aware, just for query range)."""
        from datetime import datetime, timedelta
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return (dt + timedelta(days=offset_days)).strftime("%Y-%m-%d")

    # ─────────────────────────────────────────────────────────────────
    # Momentum crowding
    # ─────────────────────────────────────────────────────────────────

    def _compute_momentum(self, result: CrowdingResult,
                          kline: pd.DataFrame, trade_date: str) -> None:
        """return_20d and return_60d (trailing returns BEFORE trade_date).

        High momentum = market has already noticed = crowded.
        """
        closes = kline["close"].values
        if len(closes) >= LOOKBACK_20D + 1:
            result.return_20d = float((closes[-1] / closes[-LOOKBACK_20D - 1]) - 1.0)
        if len(closes) >= LOOKBACK_60D + 1:
            result.return_60d = float((closes[-1] / closes[-LOOKBACK_60D - 1]) - 1.0)

    # ─────────────────────────────────────────────────────────────────
    # Volatility crowding
    # ─────────────────────────────────────────────────────────────────

    def _compute_volatility(self, result: CrowdingResult,
                            kline: pd.DataFrame, trade_date: str) -> None:
        """realized_vol_20d = std(daily_returns, 20d) BEFORE trade_date.

        High vol = event-driven attention = potentially crowded.
        """
        closes = kline["close"].values
        if len(closes) >= LOOKBACK_20D + 1:
            daily_returns = np.diff(np.log(closes[-LOOKBACK_20D - 1:]))
            result.realized_vol_20d = float(np.std(daily_returns, ddof=0))

    # ─────────────────────────────────────────────────────────────────
    # Volume-based features (liquidity + attention crowding)
    # ─────────────────────────────────────────────────────────────────

    def _compute_volume_features(self, result: CrowdingResult,
                                 kline: pd.DataFrame, trade_date: str) -> None:
        """volume_ratio, abnormal_volume, price_gap (volume-based crowding).

        volume_ratio = volume(last_day) / avg(volume, 20d)
        abnormal_volume = (volume_last - avg_20d) / std_20d  (z-score)
        price_gap = |daily_return(last_day)|  (event-driven attention)
        """
        volumes = kline["volume"].values
        closes = kline["close"].values

        if len(volumes) >= LOOKBACK_20D + 1:
            vol_last = volumes[-1]
            vol_20d = volumes[-LOOKBACK_20D - 1:-1]  # 20 days BEFORE last day
            avg_vol = float(np.mean(vol_20d))
            std_vol = float(np.std(vol_20d, ddof=0))

            if avg_vol > 0:
                result.volume_ratio = float(vol_last / avg_vol)
            if std_vol > 0:
                result.abnormal_volume = float((vol_last - avg_vol) / std_vol)

        if len(closes) >= 2:
            result.price_gap = float(abs(closes[-1] / closes[-2] - 1.0))

    # ─────────────────────────────────────────────────────────────────
    # Market cap (control variable)
    # ─────────────────────────────────────────────────────────────────

    def _load_market_cap(self, result: CrowdingResult, code: str) -> None:
        """Load market_cap and float_mcap from security_master (static)."""
        conn = sqlite3.connect(self.cache_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT total_mv, float_mv FROM security_master "
                "WHERE security_id = ? OR code = ? LIMIT 1",
                (code, code),
            ).fetchone()
            if row:
                result.market_cap = row["total_mv"]
                result.float_mcap = row["float_mv"]
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────────────
    # Turnover percentile (cross-sectional, computed in backfill)
    # ─────────────────────────────────────────────────────────────────

    def compute_turnover_percentile(self, code: str, trade_date: str) -> Optional[float]:
        """Cross-sectional turnover percentile at trade_date.

        Uses finance_snapshots.turnover_pct. Returns percentile rank
        (0-100) of this stock's turnover vs all stocks at nearest date.
        """
        conn = sqlite3.connect(self.cache_db)
        conn.row_factory = sqlite3.Row
        try:
            # Find nearest snapshot date <= trade_date
            snap = conn.execute(
                "SELECT MAX(date) AS d FROM finance_snapshots "
                "WHERE code = ? AND date <= ? AND turnover_pct IS NOT NULL",
                (code, trade_date),
            ).fetchone()
            if not snap or not snap["d"]:
                return None
            snap_date = snap["d"]

            # Get this stock's turnover
            row = conn.execute(
                "SELECT turnover_pct FROM finance_snapshots "
                "WHERE code = ? AND date = ?",
                (code, snap_date),
            ).fetchone()
            if not row or row["turnover_pct"] is None:
                return None
            my_turnover = row["turnover_pct"]

            # Cross-sectional percentile
            all_turnovers = conn.execute(
                "SELECT turnover_pct FROM finance_snapshots "
                "WHERE date = ? AND turnover_pct IS NOT NULL",
                (snap_date,),
            ).fetchall()
            if len(all_turnovers) < 10:
                return None
            vals = [r["turnover_pct"] for r in all_turnovers]
            return float(100.0 * sum(1 for v in vals if v <= my_turnover) / len(vals))
        finally:
            conn.close()
