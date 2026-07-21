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
        """Fetch index K-line via mootdx, fallback to Eastmoney for full history.

        mootdx does not support indices (returns empty for 000300 etc.) and
        the old fallback only stored a single Tencent snapshot for today -
        leaving ``get_return`` with no historical bars, so every
        ``market_return`` came back as 0.0 and ``alpha_vs_market`` was
        overstated by exactly ``stock_return``. The Eastmoney path backfills
        the full daily series so benchmark returns are real.
        """
        name = self.INDEX_CODES.get(index_code, index_code)
        df = self.provider.get_daily_kline(index_code, start_date, end_date)

        if df.empty:
            # mootdx doesn't support indices - backfill full history from
            # Tencent ifzq (primary, stable) then Eastmoney (fallback).
            n = self._sync_tencent_history(index_code, start_date, end_date)
            if n == 0:
                n = self._sync_eastmoney_history(index_code, start_date, end_date)
            if n == 0:
                # Last resort: today's Tencent snapshot only
                print(f"  {name}({index_code}): all history sources failed, using Tencent snapshot")
                self._sync_tencent_snapshot(index_code)
            return n

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

    def _sync_tencent_history(self, index_code: str, start_date: str,
                               end_date: str) -> int:
        """Backfill index daily K-line from Tencent ifzq API (primary source).

        Endpoint: web.ifzq.gtimg.cn/appstock/app/fqkline/get
        Returns real index points (not ETF prices) for 000300/000905/000852,
        is rate-limit friendly, and returns 600+ daily bars in one call.
        Uses ``trust_env=False`` to bypass a host HTTPS proxy.
        """
        import requests
        name = self.INDEX_CODES.get(index_code, index_code)
        # Tencent symbol prefix: sh=Shanghai, sz=Shenzhen. All three preset
        # indices are Shanghai-listed (000xxx).
        sym = f"sh{index_code}"
        # datalen: ~500 trading days covers ~2 years; cap at requested range
        # by date filtering below.
        sess = requests.Session()
        sess.trust_env = False
        sess.headers.update({"User-Agent": "Mozilla/5.0"})
        try:
            r = sess.get(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                params={"param": f"{sym},day,{start_date},{end_date},640,"},
                timeout=15,
            )
            data = r.json()
        except Exception as e:
            print(f"  {name}({index_code}): Tencent history request failed: {e}")
            return 0

        stock_data = data.get("data", {}).get(sym, {})
        # 'day' = unadjusted; 'qfq'/'hfq' would be adjusted. Indices need no
        # adjustment, so prefer 'day'.
        klines = stock_data.get("day") or stock_data.get("qfqday") or []
        if not klines:
            print(f"  {name}({index_code}): Tencent returned no bars")
            return 0

        # Each kline: [date, open, close, high, low, volume, ...]
        records = []
        for k in klines:
            if len(k) < 6:
                continue
            try:
                d, o, c, h, low, vol = k[0], k[1], k[2], k[3], k[4], k[5]
                close_val = float(c)
                records.append((
                    index_code, d,
                    float(o), float(h), float(low),
                    close_val, close_val,   # adj_close == close (indices)
                    float(vol), 0.0,
                ))
            except (ValueError, TypeError):
                continue

        self.db.executemany("""
            INSERT OR REPLACE INTO market_index_daily
            (index_code, trade_date, open, high, low, close, adj_close, volume, amount)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, records)
        self.db.commit()
        print(f"  {name}({index_code}): {len(records)} bars synced (Tencent)")
        return len(records)

    def _sync_eastmoney_history(self, index_code: str, start_date: str,
                                 end_date: str) -> int:
        """Backfill index daily K-line from Eastmoney push2his API.

        Eastmoney serves real index points (not ETF prices like mootdx) for
        000300/000905/000852 and is the most reliable free source. Uses a
        session with ``trust_env=False`` so a host HTTPS proxy does not block
        the request (the default ``requests`` call honours ``HTTP_PROXY``).
        """
        import requests
        name = self.INDEX_CODES.get(index_code, index_code)
        # Eastmoney secid prefix: 1 = Shanghai, 0 = Shenzhen. All three preset
        # indices (000300/000905/000852) are Shanghai-listed.
        prefix = "1"
        beg = start_date.replace("-", "")
        end = end_date.replace("-", "")
        params = {
            "secid": f"{prefix}.{index_code}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "101",   # daily K
            "fqt": "0",     # no adjustment - indices are already points,
                            # forward-adjust would store a relative factor
                            # (e.g. 3.65) in the adj field, which corrupts
                            # adj_close and makes get_return return nonsense.
            "beg": beg,
            "end": end,
        }
        sess = requests.Session()
        sess.trust_env = False   # bypass host proxy (HTTP_PROXY/HTTPS_PROXY)
        sess.headers.update({"User-Agent": "Mozilla/5.0"})
        try:
            r = sess.get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params=params, timeout=15,
            )
            data = r.json()
            klines = data.get("data", {}).get("klines", []) if data.get("data") else []
        except Exception as e:
            print(f"  {name}({index_code}): Eastmoney request failed: {e}")
            return 0

        if not klines:
            print(f"  {name}({index_code}): Eastmoney returned no bars")
            return 0

        # Each kline: "date,open,close,high,low,vol,amount" (fqt=0 -> no adj field
        # of meaning; we treat adj_close == close since indices need no adjustment)
        records = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            d, o, c, h, low, vol, amt = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
            try:
                close_val = float(c)
                records.append((
                    index_code, d,
                    float(o), float(h), float(low),
                    close_val, close_val,   # adj_close == close for indices
                    float(vol), float(amt),
                ))
            except ValueError:
                continue

        self.db.executemany("""
            INSERT OR REPLACE INTO market_index_daily
            (index_code, trade_date, open, high, low, close, adj_close, volume, amount)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, records)
        self.db.commit()
        print(f"  {name}({index_code}): {len(records)} bars synced (Eastmoney)")
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
