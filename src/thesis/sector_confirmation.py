"""
Sector Confirmation Scorer - Commit 6-S.12.3.

Answers: is the stock outperforming its sector, or just riding it?

This is the Recovery Confirmation layer of Security Analyst v2. It filters
the "Recovery Beta Trap" - stocks that rise during a market recovery solely
because their sector rises, with no genuine stock-specific strength.

Three signals (0-100 each):
  1. relative_strength (50%): stock 20d return vs sector 20d return
  2. sector_strength (30%): sector 20d return vs market 20d return
     (is the sector itself leading the recovery?)
  3. consistency (20%): how many of the last 5 days did the stock
     outperform its sector? (momentum confirmation)

Data constraint: industry_daily_returns covers 2024-06-04 onwards only.
For episodes before that, returns score=50 (neutral) with a flag.

Usage:
    from src.thesis.sector_confirmation import SectorConfirmationScorer
    scorer = SectorConfirmationScorer()
    result = scorer.compute("600519", "2024-08-29")
    # result = SectorConfirmResult(score=62.3, relative_strength=70, ...)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

import numpy as np

from src.data.local_provider import LocalDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SectorConfirmResult:
    """Sector-relative strength confirmation for one stock."""
    code: str
    trade_date: str
    sector: Optional[str] = None

    # Subscores (0-100)
    relative_strength: float = 50.0
    sector_strength: float = 50.0
    consistency: float = 50.0

    # Raw signals
    stock_return_20d: Optional[float] = None
    sector_return_20d: Optional[float] = None
    market_return_20d: Optional[float] = None
    rs_vs_sector: Optional[float] = None       # stock - sector
    sector_vs_market: Optional[float] = None   # sector - market

    # Diagnosis
    data_available: bool = True   # False if sector data missing (pre-2024-06)

    # Composite
    score: float = 50.0  # 0-100

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "trade_date": self.trade_date,
            "sector": self.sector,
            "relative_strength": round(self.relative_strength, 1),
            "sector_strength": round(self.sector_strength, 1),
            "consistency": round(self.consistency, 1),
            "stock_return_20d": self.stock_return_20d,
            "sector_return_20d": self.sector_return_20d,
            "market_return_20d": self.market_return_20d,
            "rs_vs_sector": self.rs_vs_sector,
            "sector_vs_market": self.sector_vs_market,
            "data_available": self.data_available,
            "score": round(self.score, 1),
        }


class SectorConfirmationScorer:
    """6-S.12.3: Sector-relative strength confirmation.

    Filters stocks that merely ride sector beta during a recovery.
    Requires the stock to outperform its own sector, not just the market.
    """

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db
        self.local = LocalDataProvider()

    def compute(self, code: str, trade_date: str,
                lookback_days: int = 20) -> SectorConfirmResult:
        """Compute sector confirmation score.

        Args:
            code: bare stock code
            trade_date: ISO date
            lookback_days: return window (default 20 trading days)

        Returns: SectorConfirmResult
        """
        code = str(code).zfill(6)
        result = SectorConfirmResult(code=code, trade_date=trade_date)

        # Look back lookback_days trading days for the return window
        start_date = self._shift_trading_days(trade_date, -lookback_days)
        if not start_date:
            result.data_available = False
            result.score = 50.0
            return result

        # 1. Stock sector
        result.sector = self._get_sector(code)
        if not result.sector:
            result.data_available = False
            result.score = 50.0
            return result

        # 2. Stock 20d return
        result.stock_return_20d = self._get_stock_return(code, start_date,
                                                          trade_date)
        # 3. Sector 20d cumulative return
        result.sector_return_20d = self._get_sector_cumulative(
            result.sector, start_date, trade_date)
        # 4. Market 20d return
        result.market_return_20d = self._get_market_return(start_date,
                                                           trade_date)

        if result.sector_return_20d is None:
            # Sector data not available for this date range (pre-2024-06)
            result.data_available = False
            result.score = 50.0
            return result

        # Compute relative signals
        if result.stock_return_20d is not None:
            result.rs_vs_sector = result.stock_return_20d - result.sector_return_20d
        if result.market_return_20d is not None:
            result.sector_vs_market = result.sector_return_20d - result.market_return_20d

        # Subscores
        result.relative_strength = self._score_relative_strength(
            result.rs_vs_sector)
        result.sector_strength = self._score_sector_strength(
            result.sector_vs_market)
        result.consistency = self._score_consistency(
            code, result.sector, trade_date)

        # Composite
        result.score = (
            0.50 * result.relative_strength
            + 0.30 * result.sector_strength
            + 0.20 * result.consistency
        )
        result.score = float(min(100.0, max(0.0, result.score)))
        return result

    # ------------------------------------------------------------------
    # Subscore computations
    # ------------------------------------------------------------------

    def _score_relative_strength(self, rs: Optional[float]) -> float:
        """Score stock return minus sector return (50% weight).

        Positive = stock outperforms sector (genuine stock strength).
        """
        if rs is None:
            return 50.0
        if rs > 0.05:
            return 90.0
        elif rs > 0.02:
            return 72.0
        elif rs > 0:
            return 60.0
        elif rs > -0.02:
            return 45.0
        elif rs > -0.05:
            return 30.0
        else:
            return 15.0

    def _score_sector_strength(self, sm: Optional[float]) -> float:
        """Score sector return minus market return (30% weight).

        Positive = sector is leading the market (sector momentum).
        We want stocks in leading sectors, but this alone isn't enough -
        it must combine with relative_strength to confirm stock-specific alpha.
        """
        if sm is None:
            return 50.0
        if sm > 0.03:
            return 80.0
        elif sm > 0.01:
            return 65.0
        elif sm > 0:
            return 55.0
        elif sm > -0.01:
            return 45.0
        elif sm > -0.03:
            return 35.0
        else:
            return 20.0

    def _score_consistency(self, code: str, sector: str,
                           trade_date: str) -> float:
        """Score daily outperformance consistency over last 5 days (20%).

        What fraction of the last 5 trading days did the stock outperform
        its sector? Consistent outperformance = genuine strength; one-day
        spikes = noise.
        """
        # Get last 5 trading dates
        conn = sqlite3.connect(self.cache_db)
        try:
            dates = conn.execute(
                "SELECT trade_date FROM trading_calendar "
                "WHERE is_trading=1 AND trade_date <= ? "
                "ORDER BY trade_date DESC LIMIT 5",
                (trade_date,),
            ).fetchall()
        finally:
            conn.close()
        if not dates:
            return 50.0
        dates = [d[0] for d in dates]

        stock_rets = []
        for d in dates:
            prev = self._shift_trading_days(d, -1)
            if prev:
                sr = self._get_stock_return(code, prev, d)
                if sr is not None:
                    stock_rets.append((d, sr))

        if not stock_rets:
            return 50.0

        outperform_count = 0
        checked = 0
        conn = sqlite3.connect(self.cache_db)
        try:
            for d, sr in stock_rets:
                sec_row = conn.execute(
                    "SELECT return FROM industry_daily_returns "
                    "WHERE industry=? AND trade_date=?",
                    (sector, d),
                ).fetchone()
                if sec_row and sec_row[0] is not None:
                    checked += 1
                    if sr > sec_row[0]:
                        outperform_count += 1
        finally:
            conn.close()

        if checked == 0:
            return 50.0
        rate = outperform_count / checked
        # Map rate (0-1) to score (0-100) with 50% = neutral
        return 50.0 + (rate - 0.5) * 100.0

    # ------------------------------------------------------------------
    # Data access helpers
    # ------------------------------------------------------------------

    def _get_sector(self, code: str) -> str | None:
        conn = sqlite3.connect(self.cache_db)
        try:
            row = conn.execute(
                "SELECT industry FROM security_master WHERE code = ?",
                (code,),
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()

    def _get_stock_return(self, code: str, start: str,
                          end: str) -> float | None:
        try:
            kline = self.local.get_daily_kline(code, start, end)
            if kline is not None and not kline.empty and len(kline) >= 2:
                close = kline["close"].values
                return float((close[-1] - close[0]) / close[0])
        except Exception:
            pass
        return None

    def _get_sector_cumulative(self, industry: str, start: str,
                               end: str) -> float | None:
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT return FROM industry_daily_returns "
                "WHERE industry=? AND trade_date > ? AND trade_date <= ? "
                "ORDER BY trade_date",
                (industry, start, end),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        cum = 1.0
        for r in rows:
            if r[0] is not None:
                cum *= (1.0 + r[0])
        return cum - 1.0

    def _get_market_return(self, start: str, end: str) -> float | None:
        p0 = self._index_close("000300", start)
        p1 = self._index_close("000300", end)
        if p0 is None or p1 is None or p0 == 0:
            return None
        return (p1 - p0) / p0

    def _index_close(self, code: str, trade_date: str) -> float | None:
        conn = sqlite3.connect(self.cache_db)
        try:
            row = conn.execute(
                "SELECT adj_close FROM market_index_daily "
                "WHERE index_code=? AND trade_date=?",
                (code, trade_date),
            ).fetchone()
            if not row or row[0] is None:
                row = conn.execute(
                    "SELECT adj_close FROM market_index_daily "
                    "WHERE index_code=? AND trade_date<=? "
                    "ORDER BY trade_date DESC LIMIT 1",
                    (code, trade_date),
                ).fetchone()
        finally:
            conn.close()
        if not row or row[0] is None:
            return None
        return float(row[0])

    def _shift_trading_days(self, trade_date: str,
                            offset: int) -> str | None:
        """Shift a date by `offset` trading days (negative = backward)."""
        conn = sqlite3.connect(self.cache_db)
        try:
            if offset >= 0:
                row = conn.execute(
                    "SELECT trade_date FROM trading_calendar "
                    "WHERE is_trading=1 AND trade_date >= ? "
                    "ORDER BY trade_date LIMIT 1 OFFSET ?",
                    (trade_date, offset),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT trade_date FROM trading_calendar "
                    "WHERE is_trading=1 AND trade_date <= ? "
                    "ORDER BY trade_date DESC LIMIT 1 OFFSET ?",
                    (trade_date, -offset - 1),
                ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
