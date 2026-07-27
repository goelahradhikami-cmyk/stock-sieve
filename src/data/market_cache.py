"""
Market Data Cache Layer - Commit Evolution Engine v2 prerequisite.

Caches daily K-line into SQLite so 50-generation × 32-doctrine backtests don't
re-read .day files 160000 times. The bottleneck in long evolution runs is data
IO (reading TDX .day files per stock per date range), not computation.

Flow:
  1. preload(symbols, start_date, end_date) - bulk read .day files, write to
     price_snapshot_cache table (one-time, ~minutes for 200 stocks × 2.5y)
  2. get_kline(code, start, end) - read from cache (SQL query, 10-50x faster)

Usage:
    from src.data.market_cache import MarketDataCache
    cache = MarketDataCache()
    cache.preload(['600519','000001',...], '2024-01-01', '2026-07-17')
    kline = cache.get_kline('600519', '2026-04-15', '2026-05-13')
"""

from __future__ import annotations

import sqlite3
import time

import pandas as pd

from src.data.local_provider import LocalDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


DDL_PRICE_CACHE = """
CREATE TABLE IF NOT EXISTS price_snapshot_cache (
    security_id  TEXT NOT NULL,
    trade_date   DATE NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    PRIMARY KEY (security_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_psc_sid ON price_snapshot_cache(security_id);
CREATE INDEX IF NOT EXISTS idx_psc_date ON price_snapshot_cache(trade_date);
"""


class MarketDataCache:
    """SQLite-backed daily K-line cache for fast backtesting.

    Reads from LocalDataProvider (TDX .day files) on preload, then serves
    from SQLite on subsequent queries. ~10-50x faster than re-reading .day
    files for the 160000 queries in a 50-gen evolution run.
    """

    def __init__(self, db_path: str = "data/cache.db"):
        self.db_path = db_path
        self._local = LocalDataProvider()
        self._ensure_table()

    def _ensure_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(DDL_PRICE_CACHE)
        conn.commit()
        conn.close()

    def preload(self, symbols: list[str], start_date: str, end_date: str) -> int:
        """Bulk-load K-line for all symbols into the cache.

        Args:
            symbols: list of bare stock codes (e.g. ['600519', '000001'])
            start_date: ISO start date
            end_date: ISO end date

        Returns: number of rows written.
        """
        conn = sqlite3.connect(self.db_path)
        total_written = 0
        t0 = time.time()

        try:
            for i, code in enumerate(symbols):
                try:
                    kline = self._local.get_daily_kline(code, start_date, end_date)
                    if kline is None or kline.empty:
                        continue

                    # Normalize columns
                    rows = []
                    for _, row in kline.iterrows():
                        date_val = row.get("date", row.name)
                        date_str = (
                            str(date_val)[:10]
                            if hasattr(date_val, "strftime")
                            else str(date_val)[:10]
                        )
                        rows.append(
                            (
                                code,
                                date_str,
                                float(row.get("open", 0)),
                                float(row.get("high", 0)),
                                float(row.get("low", 0)),
                                float(row.get("close", 0)),
                                float(row.get("volume", 0)),
                            )
                        )

                    conn.executemany(
                        "INSERT OR REPLACE INTO price_snapshot_cache "
                        "(security_id, trade_date, open, high, low, close, volume) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                    total_written += len(rows)
                except Exception as e:
                    logger.debug("market_cache: preload %s failed: %s", code, e)

                if (i + 1) % 50 == 0:
                    conn.commit()
                    elapsed = time.time() - t0
                    logger.info(
                        "market_cache: %d/%d symbols, %d rows (%.1fs)",
                        i + 1,
                        len(symbols),
                        total_written,
                        elapsed,
                    )

            conn.commit()
        finally:
            conn.close()

        elapsed = time.time() - t0
        logger.info(
            "market_cache: preloaded %d rows for %d symbols in %.1fs",
            total_written,
            len(symbols),
            elapsed,
        )
        return total_written

    def get_kline(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Read cached K-line for a code between two dates.

        Returns DataFrame with columns: date, open, high, low, close, volume.
        Empty DataFrame if not in cache (caller should handle).
        """
        conn = sqlite3.connect(self.db_path)
        try:
            df = pd.read_sql_query(
                "SELECT trade_date AS date, open, high, low, close, volume "
                "FROM price_snapshot_cache "
                "WHERE security_id=? AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date",
                conn,
                params=(code, start_date, end_date),
            )
        finally:
            conn.close()
        return df

    def get_close(self, code: str, date: str) -> float | None:
        """Get a single close price (for fast forward-return calc)."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT close FROM price_snapshot_cache "
                "WHERE security_id=? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
                (code, date),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def get_forward_return(self, code: str, start_date: str, end_date: str) -> float | None:
        """Compute forward return from cache (avoid loading full DataFrame).

        return = (close[end] - close[start]) / close[start]
        """
        conn = sqlite3.connect(self.db_path)
        try:
            # Start price: first available on or after start_date
            start_row = conn.execute(
                "SELECT close FROM price_snapshot_cache "
                "WHERE security_id=? AND trade_date >= ? ORDER BY trade_date LIMIT 1",
                (code, start_date),
            ).fetchone()
            # End price: last available on or before end_date
            end_row = conn.execute(
                "SELECT close FROM price_snapshot_cache "
                "WHERE security_id=? AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
                (code, end_date),
            ).fetchone()
        finally:
            conn.close()

        if not start_row or not end_row:
            return None
        start_price = start_row[0]
        end_price = end_row[0]
        if not start_price or start_price <= 0:
            return None
        return (end_price - start_price) / start_price

    def is_preloaded(self, code: str) -> bool:
        """Check if a code has any cached data."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM price_snapshot_cache WHERE security_id=? LIMIT 1",
                (code,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
