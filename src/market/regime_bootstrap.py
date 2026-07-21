"""
Market Regime Bootstrap - Commit 6-L.7 Phase 1.1.

Provides historical market regime labels for the Doctrine Survival Arena.
6-L.7 needs to know "what market environment was each backtest date in" so
that doctrine fitness can be grouped by regime (bull/bear/crash/sideway/
high_volatility) - otherwise the evolution engine only learns "who made
money" not "who adapts to what".

Design (per user decision):
  * NOT a full Regime Engine (that's 6-N / HMM). This is a lightweight
    labeler that reads 000300 from market_index_daily and applies simple
    60-day return + 20-day volatility rules.
  * Writes to the EXISTING market_regime_snapshots table (obs_date +
    regime_type columns) so it becomes the system-level regime source.
    daily_run is also wired to persist regime going forward.
  * Crash is checked FIRST so an early-stage plunge isn't mislabeled bear.
  * high_volatility is a distinct regime (not bear): A-share liquidity
    shocks (2020 pandemic, 2022 credit crisis) kill quant strategies
    without being simple bear markets.

Usage:
    from src.market.regime_bootstrap import RegimeClassifier, RegimeBootstrap
    RegimeBootstrap().backfill_history('2024-01-01')
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)


class RegimeClassifier:
    """Rule-based 5-state regime classifier (lightweight, no HMM).

    Order matters: crash is checked first so a nascent crash (return < -20%)
    isn't swallowed by the bear branch. high_volatility captures liquidity
    shocks that aren't simple downtrends.
    """

    # Thresholds (Commit 6-L.7 v1, tunable)
    CRASH_RETURN = -0.20
    BEAR_RETURN = -0.10
    BULL_RETURN = 0.15
    # volatility percentile threshold (computed dynamically per series)
    HIGH_VOL_PERCENTILE = 0.90

    REGIMES = ("crash", "bear", "bull", "high_volatility", "sideway")

    @classmethod
    def classify(cls, return_60d: float, volatility_20d: float,
                 vol_percentile_threshold: Optional[float] = None) -> str:
        """Classify a single observation.

        Args:
            return_60d: 60-trading-day cumulative return (e.g. -0.12 = -12%)
            volatility_20d: 20-day annualized volatility (e.g. 0.28 = 28%)
            vol_percentile_threshold: the 90th percentile of the vol series
                (for high_volatility detection). If None, uses a fixed 0.30.
        """
        # 1. Crash first (early-stage plunge protection)
        if return_60d < cls.CRASH_RETURN:
            return "crash"
        # 2. Bear
        if return_60d < cls.BEAR_RETURN:
            return "bear"
        # 3. Bull
        if return_60d > cls.BULL_RETURN:
            return "bull"
        # 4. High volatility (liquidity shock, not a downtrend)
        vol_thresh = vol_percentile_threshold if vol_percentile_threshold is not None else 0.30
        if volatility_20d > vol_thresh:
            return "high_volatility"
        # 5. Sideway (default)
        return "sideway"


class RegimeBootstrap:
    """Backfill market_regime_snapshots from 000300 history.

    Reads close prices from market_index_daily, computes 60-day return and
    20-day annualized volatility per trading day, classifies regime, and
    writes to market_regime_snapshots (the system-level regime fact source).
    """

    def __init__(self, cache_db: str = "data/cache.db",
                 eval_db: str = "data/evaluation.db",
                 index_code: str = "000300"):
        self.cache_db = cache_db
        self.eval_db = eval_db
        self.index_code = index_code

    def _load_index_close(self, start_date: str) -> pd.DataFrame:
        """Load 000300 daily close from market_index_daily."""
        conn = sqlite3.connect(self.cache_db)
        try:
            df = pd.read_sql_query(
                "SELECT trade_date AS date, close FROM market_index_daily "
                "WHERE index_code=? AND trade_date >= ? ORDER BY trade_date",
                conn, params=(self.index_code, start_date),
            )
        finally:
            conn.close()
        df["date"] = pd.to_datetime(df["date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        return df

    def backfill_history(self, start_date: str = "2024-01-01",
                         end_date: str | None = None) -> int:
        """Compute and persist regime labels for every trading day.

        Returns: number of rows written to market_regime_snapshots.
        """
        df = self._load_index_close(start_date)
        if len(df) < 60:
            logger.warning("regime_bootstrap: only %d rows since %s (need >=60)", len(df), start_date)
            return 0

        if end_date:
            df = df[df["date"] <= pd.Timestamp(end_date)]

        # 60-day cumulative return
        df["return_60d"] = df["close"].pct_change(60)
        # 20-day annualized volatility (std of daily log returns * sqrt(252))
        daily_ret = np.log(df["close"] / df["close"].shift(1))
        df["vol_20d"] = daily_ret.rolling(20).std() * np.sqrt(252)

        # 90th percentile of vol for the high_volatility threshold
        vol_series = df["vol_20d"].dropna()
        vol_p90 = float(np.percentile(vol_series, 90)) if len(vol_series) > 10 else 0.30
        logger.info("regime_bootstrap: vol_p90=%.3f, %d days to classify", vol_p90, len(df))

        # Classify each day (skip first 60 - no return_60d)
        conn = sqlite3.connect(self.eval_db)
        written = 0
        try:
            for _, row in df.iterrows():
                if pd.isna(row["return_60d"]) or pd.isna(row["vol_20d"]):
                    continue
                regime = RegimeClassifier.classify(
                    float(row["return_60d"]),
                    float(row["vol_20d"]),
                    vol_percentile_threshold=vol_p90,
                )
                obs_date = row["date"].strftime("%Y-%m-%d")
                conn.execute("""
                    INSERT OR REPLACE INTO market_regime_snapshots
                    (obs_date, regime_type, risk_score, indicators_json, created_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    obs_date, regime,
                    float(row["vol_20d"]) * 100,  # risk_score = vol*100 (rough)
                    f'{{"return_60d":{row["return_60d"]:.4f},"vol_20d":{row["vol_20d"]:.4f}}}',
                ))
                written += 1
            conn.commit()
        finally:
            conn.close()

        logger.info("regime_bootstrap: wrote %d regime labels", written)
        return written

    def get_regime(self, obs_date: str) -> str | None:
        """Read regime for a date (system-level fact source)."""
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT regime_type FROM market_regime_snapshots WHERE obs_date=?",
                (obs_date,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def get_regime_distribution(self) -> dict[str, int]:
        """Count of each regime across all labeled dates."""
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT regime_type, COUNT(*) FROM market_regime_snapshots "
                "GROUP BY regime_type ORDER BY COUNT(*) DESC"
            ).fetchall()
            return {r[0]: r[1] for r in rows}
        finally:
            conn.close()
