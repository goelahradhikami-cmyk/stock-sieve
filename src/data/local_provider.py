"""
Local TDX Data Provider — Read Tongdaxin .day files directly.

Format: 32-byte records: I(date) I(open) I(high) I(low) I(close) f(amount) I(volume) I(reserved)
Prices are in cents (÷100). Volume is in lots.
"""

import struct
import os
import pandas as pd
from datetime import date, datetime
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)


class LocalDataProvider:
    """Reads Tongdaxin local .day files."""

    # TDX data directories — priority order (fallbacks only).
    # Override at runtime via environment variables so the code is not tied to a
    # hardcoded drive letter:
    #   STOCK_SIEVE_TDX_VIPDOC  -> exact path to the "vipdoc" directory
    #   STOCK_SIEVE_TDX_ROOT    -> TDX install root; its "vipdoc" subdir is used
    DEFAULT_TDX_PATHS = [
        "D:/new_tdx_mock/vipdoc",     # Updated daily (to 2026-07-15)
        "C:/new_tdx/vipdoc",          # Historical (to 2026-02-27)
        "D:/goldsun/vipdoc",
    ]

    def __init__(self):
        self._cache = {}
        self.tdx_root = self._resolve_tdx_root()
        if self.tdx_root is None:
            print("  ⚠️ No TDX vipdoc directory found. Set STOCK_SIEVE_TDX_ROOT "
                  "or STOCK_SIEVE_TDX_VIPDOC, or ensure one of the default paths exists.")

    def _resolve_tdx_root(self) -> Optional[str]:
        """Return the first existing TDX vipdoc directory.

        Environment overrides take priority over the hardcoded defaults, so the
        provider works on any machine without editing source.
        """
        candidates = []
        env_vipdoc = os.environ.get("STOCK_SIEVE_TDX_VIPDOC")
        if env_vipdoc:
            candidates.append(env_vipdoc)
        env_root = os.environ.get("STOCK_SIEVE_TDX_ROOT")
        if env_root:
            candidates.append(os.path.join(env_root, "vipdoc"))
        candidates.extend(self.DEFAULT_TDX_PATHS)
        for p in candidates:
            if p and os.path.isdir(p):
                return p
        return None

    def _find_file(self, code: str) -> Optional[str]:
        """Locate the .day file for a stock code."""
        if not self.tdx_root:
            return None

        if code.startswith("6"):
            market = "sh"
        elif code.startswith(("0", "3")):
            market = "sz"
        elif code.startswith(("4", "8")):
            market = "bj"
        else:
            return None

        path = os.path.join(self.tdx_root, market, "lday", f"{market}{code}.day")
        if os.path.exists(path):
            return path
        return None

    def get_daily_kline(self, code: str, start_date: str = None,
                         end_date: str = None) -> pd.DataFrame:
        """Read TDX .day file and return DataFrame with OHLCV.

        Returns columns: date, open, high, low, close, adj_close, volume, amount
        """
        cache_key = f"{code}_{start_date}_{end_date}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        filepath = self._find_file(code)
        if not filepath:
            return pd.DataFrame()

        try:
            records = []
            with open(filepath, "rb") as f:
                while True:
                    data = f.read(32)
                    if len(data) < 32:
                        break
                    d, op, hi, lo, cl, amt, vol, _ = struct.unpack("I I I I I f I I", data)
                    if d < 19900101:  # Invalid date
                        continue
                    date_str = f"{d // 10000}-{(d // 100) % 100:02d}-{d % 100:02d}"

                    # Date filtering
                    if start_date and date_str < start_date:
                        continue
                    if end_date and date_str > end_date:
                        continue

                    records.append({
                        "date": date_str,
                        "open": op / 100.0,
                        "high": hi / 100.0,
                        "low": lo / 100.0,
                        "close": cl / 100.0,
                        "adj_close": cl / 100.0,  # TDX .day is already adjusted
                        "volume": float(vol),
                        "amount": amt,
                    })

            if not records:
                return pd.DataFrame()

            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            self._cache[cache_key] = df
            return df

        except Exception as e:
            logger.warning("local_provider: TDX read error for %s: %s", code, e)
            return pd.DataFrame()

    def get_stock_snapshot(self, code: str) -> dict:
        """Get latest bar as snapshot."""
        df = self.get_daily_kline(code)
        if df.empty:
            return {}
        row = df.iloc[-1]
        return {
            "code": code, "date": str(row["date"])[:10],
            "price": row["close"], "open": row["open"],
            "high": row["high"], "low": row["low"],
            "volume": row["volume"], "amount": row["amount"],
        }

    def has_data(self, code: str) -> bool:
        return self._find_file(code) is not None
