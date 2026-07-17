"""
Index Data Provider — Market index daily data via mootdx.

Commit 6-A: Caches CSI 300 / CSI 500 / CSI 1000 in SQLite
for fast benchmark return lookups.
"""

import sqlite3
from src.data.db import managed_connect
import pandas as pd
from datetime import date, timedelta
from typing import Optional

from src.data.provider import MarketDataProvider


class IndexDataProvider:
    """Provides market index OHLCV data, cached in SQLite."""

    INDEX_CODES = {
        '000300': '沪深300',
        '000905': '中证500',
        '000852': '中证1000',
    }

    # Default benchmark for evaluation
    BENCHMARK_CODE = '000300'

    def __init__(self, db_path: str = "data/cache.db"):
        self.db = managed_connect(self, db_path)
        self.provider = MarketDataProvider()
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS market_index_daily (
                index_code  TEXT NOT NULL,
                trade_date  DATE NOT NULL,
                open        REAL, high REAL, low REAL, close REAL,
                adj_close   REAL, volume REAL, amount REAL,
                PRIMARY KEY (index_code, trade_date)
            )
        """)
        self.db.commit()

    def sync_index(self, index_code: str, start_date: str, end_date: str):
        """Fetch index K-line via mootdx, fallback to Tencent for current snapshot."""
        name = self.INDEX_CODES.get(index_code, index_code)
        df = self.provider.get_daily_kline(index_code, start_date, end_date)

        if df.empty:
            # mootdx doesn't support indices — use Tencent live quote
            print(f"  {name}({index_code}): using Tencent snapshot (mootdx unsupported)")
            self._sync_tencent_snapshot(index_code)
            return 1

        records = []
        for _, row in df.iterrows():
            date_val = row.get('date')
            date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)[:10]
            records.append((
                index_code, date_str,
                row.get('open'), row.get('high'), row.get('low'),
                row.get('close'), row.get('adj_close', row.get('close')),
                row.get('volume'), row.get('amount'),
            ))

        self.db.executemany("""
            INSERT OR REPLACE INTO market_index_daily
            (index_code, trade_date, open, high, low, close, adj_close, volume, amount)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, records)
        self.db.commit()
        print(f"  {name}({index_code}): {len(records)} bars synced")
        return len(records)

    def _sync_tencent_snapshot(self, index_code: str):
        """Record today's index close from Tencent Finance."""
        import requests
        prefix = "sh" if index_code.startswith(("0", "6")) else "sz"
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={prefix}{index_code}", timeout=5)
            r.encoding = "gbk"
            fields = r.text.split("~")
            if len(fields) > 4:
                price = float(fields[3]) if fields[3] else 0
                today = date.today().isoformat()
                self.db.execute("""
                    INSERT OR REPLACE INTO market_index_daily
                    (index_code, trade_date, close, adj_close)
                    VALUES (?, ?, ?, ?)
                """, (index_code, today, price, price))
                self.db.commit()
                print(f"    Snapshot: {price:.2f} @ {today}")
        except Exception as e:
            print(f"    ⚠️ Tencent snapshot failed: {e}")

    def sync_all(self, start_date: str = '2020-01-01', end_date: str = None):
        """Sync all preset indices."""
        if end_date is None:
            end_date = date.today().isoformat()
        total = 0
        for code, name in self.INDEX_CODES.items():
            n = self.sync_index(code, start_date, end_date)
            total += n
        return total

    def _get_adj_close(self, index_code: str, trade_date: str) -> Optional[float]:
        """Return adj_close on ``trade_date`` or the most recent prior bar.

        Returns ``None`` if no usable (non-NULL) value exists — guards against
        ``float(None)`` / ``None``-arithmetic crashes when the cache row exists
        but the price column was never populated.
        """
        row = self.db.execute(
            "SELECT adj_close FROM market_index_daily WHERE index_code=? AND trade_date=?",
            (index_code, trade_date)
        ).fetchone()
        if not row or row[0] is None:
            row = self.db.execute(
                "SELECT adj_close FROM market_index_daily WHERE index_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                (index_code, trade_date)
            ).fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])

    def get_return(self, index_code: str, start_date: str, end_date: str) -> float:
        """Calculate index return between two dates."""
        start_price = self._get_adj_close(index_code, start_date)
        if start_price is None or start_price == 0:
            return 0.0
        end_price = self._get_adj_close(index_code, end_date)
        if end_price is None:
            return 0.0
        return (end_price - start_price) / start_price

    def get_latest_close(self, index_code: str = '000300') -> float:
        """Get the most recent close for an index.

        Returns ``0.0`` (never raises) when no row exists or the cached price
        is NULL — previously ``float(row[0])`` crashed on ``float(None)``.
        """
        row = self.db.execute(
            "SELECT adj_close FROM market_index_daily WHERE index_code=? ORDER BY trade_date DESC LIMIT 1",
            (index_code,)
        ).fetchone()
        if not row or row[0] is None:
            return 0.0
        return float(row[0])
