"""
Event Reaction Calculator - Commit 6-S.15.1 (v3.3 Phase 1).

Computes how the market reacted to earnings announcements. This is the
price-reaction half of the Expectation Gap Engine.

Key principle: returns are measured FROM the announcement date FORWARD.
This is event-study methodology, NOT momentum. Momentum (trailing returns)
spans the announcement and contaminates the signal with pre-announcement
expectations. Event returns capture only the market's reaction to the
new information.

Three layers of adjustment:
  1. Raw return (stock price change after announcement)
  2. Market-adjusted (subtract HS300 return over same window)
  3. Sector-adjusted (subtract industry return - PRIMARY signal)
  4. Fully neutral (market + sector, for residual_alpha comparison)

Usage:
    from src.thesis.event_reaction import EventReactionCalculator
    calc = EventReactionCalculator()
    result = calc.compute("600519", "2024-08-30")
    # result.sector_adjusted_t5 = +0.03 (market underreacted by 3%)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.data.local_provider import LocalDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Event windows (in trading days, measured from next_trading_day after announcement)
WINDOWS = [1, 5, 10, 20]
PRIMARY_WINDOW = 5  # sector_adjusted_t5 is the primary EGE signal


@dataclass
class EventReactionResult:
    """Price reaction to an earnings announcement."""

    security_id: str
    available_date: str  # announcement date (event anchor)

    # Earnings
    earnings_yoy_current: float | None = None
    earnings_yoy_previous: float | None = None
    earnings_yoy_previous2: float | None = None
    earnings_acceleration: float | None = None
    earnings_acceleration_2nd: float | None = None
    frm_direction: str | None = None

    # Raw returns (forward from announcement)
    return_t1: float | None = None
    return_t5: float | None = None
    return_t10: float | None = None
    return_t20: float | None = None

    # Market-adjusted
    market_return_t1: float | None = None
    market_return_t5: float | None = None
    market_return_t10: float | None = None
    market_return_t20: float | None = None
    market_adjusted_t1: float | None = None
    market_adjusted_t5: float | None = None
    market_adjusted_t10: float | None = None
    market_adjusted_t20: float | None = None

    # Sector-adjusted (PRIMARY signal)
    sector_code: str | None = None
    sector_return_t1: float | None = None
    sector_return_t5: float | None = None
    sector_return_t10: float | None = None
    sector_return_t20: float | None = None
    sector_adjusted_t1: float | None = None
    sector_adjusted_t5: float | None = None  # PRIMARY
    sector_adjusted_t10: float | None = None
    sector_adjusted_t20: float | None = None

    # Fully neutral
    residual_t5: float | None = None
    residual_t20: float | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class EventReactionCalculator:
    """6-S.15.1: Compute price reaction to earnings announcements.

    All returns are FORWARD from the announcement date (event study).
    Never uses trailing momentum (which spans the announcement).
    """

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db
        self.local = LocalDataProvider()
        self._sector_cache: dict[str, str | None] = {}
        self._calendar_cache: list[str] | None = None

    def compute(self, security_id: str, available_date: str) -> EventReactionResult:
        """Compute event reaction for one stock's one announcement.

        Args:
            security_id: bare stock code (e.g. "600519")
            available_date: earnings announcement date (ISO)

        Returns: EventReactionResult with all windows populated
        """
        security_id = str(security_id).zfill(6)
        result = EventReactionResult(
            security_id=security_id,
            available_date=available_date,
        )

        # 1. Load vintage-aware earnings (3 periods for 2nd derivative)
        self._load_earnings(result, security_id, available_date)

        # 2. Find the trading day AFTER announcement (event start)
        event_start = self._next_trading_day(available_date)
        if event_start is None:
            return result  # no trading after announcement (e.g. future date)

        # 3. Compute forward returns for each window
        for window in WINDOWS:
            event_end = self._offset_trading_day(event_start, window)
            if event_end is None:
                continue

            # Stock return
            stock_ret = self._get_stock_return(security_id, event_start, event_end)
            # Market return (HS300)
            market_ret = self._get_market_return(event_start, event_end)
            # Sector return
            sector_code = self._get_sector_code(security_id)
            sector_ret = (
                self._get_sector_cumulative_return(sector_code, event_start, event_end)
                if sector_code
                else None
            )

            # Store
            if window == 1:
                result.return_t1 = stock_ret
                result.market_return_t1 = market_ret
                result.sector_code = sector_code
                result.sector_return_t1 = sector_ret
                result.market_adjusted_t1 = (
                    stock_ret - market_ret
                    if stock_ret is not None and market_ret is not None
                    else None
                )
                result.sector_adjusted_t1 = (
                    stock_ret - sector_ret
                    if stock_ret is not None and sector_ret is not None
                    else None
                )
            elif window == 5:
                result.return_t5 = stock_ret
                result.market_return_t5 = market_ret
                result.sector_return_t5 = sector_ret
                result.market_adjusted_t5 = (
                    stock_ret - market_ret
                    if stock_ret is not None and market_ret is not None
                    else None
                )
                result.sector_adjusted_t5 = (
                    stock_ret - sector_ret
                    if stock_ret is not None and sector_ret is not None
                    else None
                )
            elif window == 10:
                result.return_t10 = stock_ret
                result.market_return_t10 = market_ret
                result.sector_return_t10 = sector_ret
                result.market_adjusted_t10 = (
                    stock_ret - market_ret
                    if stock_ret is not None and market_ret is not None
                    else None
                )
                result.sector_adjusted_t10 = (
                    stock_ret - sector_ret
                    if stock_ret is not None and sector_ret is not None
                    else None
                )
            elif window == 20:
                result.return_t20 = stock_ret
                result.market_return_t20 = market_ret
                result.sector_return_t20 = sector_ret
                result.market_adjusted_t20 = (
                    stock_ret - market_ret
                    if stock_ret is not None and market_ret is not None
                    else None
                )
                result.sector_adjusted_t20 = (
                    stock_ret - sector_ret
                    if stock_ret is not None and sector_ret is not None
                    else None
                )

        # 4. Fully neutral (residual = stock - market - sector)
        if (
            result.return_t5 is not None
            and result.market_return_t5 is not None
            and result.sector_return_t5 is not None
        ):
            result.residual_t5 = (
                result.return_t5 - result.market_return_t5 - result.sector_return_t5
            )
        if (
            result.return_t20 is not None
            and result.market_return_t20 is not None
            and result.sector_return_t20 is not None
        ):
            result.residual_t20 = (
                result.return_t20 - result.market_return_t20 - result.sector_return_t20
            )

        return result

    # ------------------------------------------------------------------
    # Earnings loading (vintage-aware, 3 periods)
    # ------------------------------------------------------------------

    def _load_earnings(self, result: EventReactionResult, code: str, as_of_date: str):
        """Load vintage-aware 3 periods of earnings_yoy."""
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT earnings_yoy, report_date, available_date "
                "FROM akshare_financials WHERE code = ? "
                "AND (available_date IS NOT NULL AND available_date <= ? "
                "     OR available_date IS NULL AND date(report_date, '+90 days') <= ?) "
                "ORDER BY report_date DESC LIMIT 3",
                (code, as_of_date, as_of_date),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) >= 1:
            ey = rows[0][0]
            result.earnings_yoy_current = ey / 100.0 if ey is not None else None
        if len(rows) >= 2:
            ey = rows[1][0]
            result.earnings_yoy_previous = ey / 100.0 if ey is not None else None
        if len(rows) >= 3:
            ey = rows[2][0]
            result.earnings_yoy_previous2 = ey / 100.0 if ey is not None else None

        # Acceleration (1st derivative)
        if result.earnings_yoy_current is not None and result.earnings_yoy_previous is not None:
            result.earnings_acceleration = (
                result.earnings_yoy_current - result.earnings_yoy_previous
            )

        # 2nd derivative
        if (
            result.earnings_acceleration is not None
            and result.earnings_yoy_previous is not None
            and result.earnings_yoy_previous2 is not None
        ):
            prev_accel = result.earnings_yoy_previous - result.earnings_yoy_previous2
            result.earnings_acceleration_2nd = result.earnings_acceleration - prev_accel

        # FRM direction (consistent with 6-S.12.2)
        if result.earnings_acceleration is not None:
            if result.earnings_acceleration > 0.02:
                result.frm_direction = "improving"
            elif result.earnings_acceleration < -0.02:
                result.frm_direction = "deteriorating"
            else:
                result.frm_direction = "stable"

    # ------------------------------------------------------------------
    # Trading calendar helpers
    # ------------------------------------------------------------------

    def _load_calendar(self) -> list[str]:
        if self._calendar_cache is not None:
            return self._calendar_cache
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT trade_date FROM trading_calendar WHERE is_trading=1 ORDER BY trade_date"
            ).fetchall()
        finally:
            conn.close()
        self._calendar_cache = [r[0] for r in rows]
        return self._calendar_cache

    def _next_trading_day(self, date_str: str) -> str | None:
        """First trading day STRICTLY AFTER date_str."""
        cal = self._load_calendar()
        for d in cal:
            if d > date_str:
                return d
        return None

    def _offset_trading_day(self, start: str, offset: int) -> str | None:
        """Trading day that is `offset` trading days after `start`."""
        cal = self._load_calendar()
        try:
            idx = cal.index(start)
        except ValueError:
            # start not in calendar, find next
            for i, d in enumerate(cal):
                if d >= start:
                    idx = i
                    break
            else:
                return None
        target = idx + offset
        if 0 <= target < len(cal):
            return cal[target]
        return None

    # ------------------------------------------------------------------
    # Return calculations
    # ------------------------------------------------------------------

    def _get_stock_return(self, code: str, start: str, end: str) -> float | None:
        try:
            kline = self.local.get_daily_kline(code, start, end)
            if kline is not None and not kline.empty and len(kline) >= 2:
                close = kline["close"].values
                return float((close[-1] - close[0]) / close[0])
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)
        return None

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
                "SELECT adj_close FROM market_index_daily WHERE index_code=? AND trade_date=?",
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

    def _get_sector_code(self, code: str) -> str | None:
        if code in self._sector_cache:
            return self._sector_cache[code]
        conn = sqlite3.connect(self.cache_db)
        try:
            row = conn.execute(
                "SELECT industry FROM security_master WHERE code = ?",
                (code,),
            ).fetchone()
        finally:
            conn.close()
        result = row[0] if row and row[0] else None
        self._sector_cache[code] = result
        return result

    def _get_sector_cumulative_return(self, industry: str, start: str, end: str) -> float | None:
        if not industry:
            return None
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT return FROM industry_daily_returns "
                "WHERE industry = ? AND trade_date > ? AND trade_date <= ? "
                "ORDER BY trade_date",
                (industry, start, end),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        cumulative = 1.0
        for r in rows:
            if r[0] is not None:
                cumulative *= 1.0 + r[0]
        return cumulative - 1.0
