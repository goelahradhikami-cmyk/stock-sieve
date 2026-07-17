"""
Data Provider — Unified interface to A-share market data.

Wraps mootdx (通达信), Tencent Finance, Sina, and Eastmoney sources.
Provides MarketSnapshot, StockSnapshot, and FactorSnapshot data structures.

Data source priority:
  1. mootdx — TCP, no IP blocking, K-line + finance
  2. Tencent — HTTP, no IP blocking, PE/PB/market cap/indices
  3. Sina — HTTP, low risk, financial statements
  4. Eastmoney — HTTP, rate-limited, exclusive data only
"""

import sqlite3
import time
import json
import hashlib
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Optional
import warnings

import pandas as pd
import numpy as np
import requests

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ── Try importing mootdx (may not be installed) ──
try:
    from mootdx.quotes import Quotes
    _HAS_MOOTDX = True
except ImportError:
    _HAS_MOOTDX = False


# ═══════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════

@dataclass
class MarketSnapshot:
    """Market-wide state at a point in time."""
    date: str
    regime_type: str = "unknown"           # bull / bear / crisis / rotation
    risk_score: float = 50.0               # 0-100
    growth_env_score: float = 50.0         # 0-100
    value_env_score: float = 50.0          # 0-100
    momentum_env_score: float = 50.0       # 0-100
    defensive_env_score: float = 50.0      # 0-100
    liquidity_score: float = 50.0          # 0-100
    market_pe_percentile: Optional[float] = None
    market_pb_percentile: Optional[float] = None
    sh_index: Optional[float] = None
    sz_index: Optional[float] = None
    hs300_index: Optional[float] = None
    indicators: dict = field(default_factory=dict)


@dataclass
class StockSnapshot:
    """Single stock basic info + current price."""
    code: str
    name: str = ""
    price: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    last_close: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[float] = None
    amount: Optional[float] = None
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    mcap: Optional[float] = None           # 总市值（亿）
    float_mcap: Optional[float] = None     # 流通市值（亿）
    turnover_pct: Optional[float] = None   # 换手率
    industry: str = ""
    list_date: str = ""


@dataclass
class FactorSnapshot:
    """Computed factor values for a single stock."""
    code: str
    date: str
    # Quality factors
    roe: Optional[float] = None
    roe_5y_avg: Optional[float] = None
    roic: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    fcf_yield: Optional[float] = None
    accruals_ratio: Optional[float] = None
    # Value factors
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    ps: Optional[float] = None
    ev_ebitda: Optional[float] = None
    dividend_yield: Optional[float] = None
    # Growth factors
    revenue_growth_1y: Optional[float] = None
    revenue_growth_3y: Optional[float] = None
    earnings_growth_1y: Optional[float] = None
    earnings_growth_3y: Optional[float] = None
    margin_trend: Optional[float] = None  # gross margin change over 3yr
    # Momentum factors
    momentum_1m: Optional[float] = None
    momentum_3m: Optional[float] = None
    momentum_6m: Optional[float] = None
    momentum_12m: Optional[float] = None
    rsi_14: Optional[float] = None
    volume_ratio: Optional[float] = None
    # Risk factors
    beta: Optional[float] = None
    volatility_1m: Optional[float] = None
    max_drawdown_1y: Optional[float] = None
    debt_to_equity: Optional[float] = None
    interest_coverage: Optional[float] = None
    # Sentiment / Alternative
    holder_change_pct: Optional[float] = None
    north_flow_ratio: Optional[float] = None
    # Raw values for further computation
    _raw: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════
# Data Provider
# ═══════════════════════════════════════════════════════════

class DataProvider:
    """Unified interface to A-share market data sources."""

    def __init__(self, cache_db_path: str = "data/cache.db"):
        self._cache_db_path = cache_db_path
        self._init_cache()
        self._session = requests.Session()
        # Bypass the system proxy. Behind a corporate proxy the default session
        # inherits HTTP(S)_PROXY from the environment, which silently blocks
        # qt.gtimg.cn — every quote then comes back empty (snap.name == "") and
        # the daily_run loop skipped *every* stock via `if not snap.name`. With
        # trust_env=False requests talks to Tencent directly, which is reachable.
        self._session.trust_env = False
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; StockSieve/0.1)",
        })
        self._last_em_call = 0.0  # eastmoney rate limiting

    # ── Cache ──────────────────────────────────────────

    def _init_cache(self):
        import os
        os.makedirs(os.path.dirname(self._cache_db_path), exist_ok=True)
        conn = sqlite3.connect(self._cache_db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                cache_key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ttl_seconds INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created_at)")
        conn.commit()
        conn.close()

    def _cache_get(self, key: str) -> Optional[dict]:
        conn = sqlite3.connect(self._cache_db_path)
        row = conn.execute(
            "SELECT data, created_at, ttl_seconds FROM cache WHERE cache_key = ?", (key,)
        ).fetchone()
        conn.close()
        if row:
            data, created, ttl = row
            created_time = datetime.fromisoformat(created)
            if (datetime.now() - created_time).total_seconds() < ttl:
                return json.loads(data)
        return None

    def _cache_set(self, key: str, data: dict, ttl: int):
        conn = sqlite3.connect(self._cache_db_path)
        conn.execute(
            "INSERT OR REPLACE INTO cache (cache_key, data, ttl_seconds) VALUES (?, ?, ?)",
            (key, json.dumps(data, default=str), ttl)
        )
        conn.commit()
        conn.close()

    def _cache_key(self, *parts) -> str:
        return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()

    # ── Eastmoney rate limiting ─────────────────────────

    def _em_throttle(self):
        """Ensure at least 1s between Eastmoney calls."""
        elapsed = time.time() - self._last_em_call
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed + np.random.uniform(0, 0.5))
        self._last_em_call = time.time()

    # ── Tencent Finance ─────────────────────────────────

    def _tencent_quote_batch(self, codes: list[str]) -> dict:
        """Fetch real-time quotes from Tencent Finance. No IP blocking."""
        cache_key = self._cache_key("tq", ",".join(sorted(codes)))
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        # Build market-prefixed codes
        prefixed = []
        for c in codes:
            c = str(c).zfill(6)
            if c.startswith(("00", "30")):
                prefixed.append(f"sz{c}")
            elif c.startswith(("60", "68")):
                prefixed.append(f"sh{c}")
            elif c.startswith(("4", "8")):
                prefixed.append(f"bj{c}")
            else:
                prefixed.append(f"sh{c}")

        url = f"https://qt.gtimg.cn/q={','.join(prefixed)}"
        try:
            resp = self._session.get(url, timeout=10)
            resp.encoding = "gbk"
            result = self._parse_tencent_response(resp.text)
            self._cache_set(cache_key, result, ttl=300)  # 5 min TTL
            return result
        except Exception as e:
            warnings.warn(f"Tencent quote failed: {e}")
            return {}

    def _parse_tencent_response(self, text: str) -> dict:
        """Parse Tencent's semicolon-delimited response."""
        result = {}
        for line in text.strip().split("\n"):
            if not line.strip() or "=" not in line:
                continue
            # v_sh600519="1~贵州茅台~600519~1680.00~..."
            parts = line.split("=", 1)
            if len(parts) != 2:
                continue
            var_name = parts[0].strip()
            raw = parts[1].strip().strip('";')
            fields = raw.split("~")

            # Extract the numeric code
            code = var_name.replace("v_sh", "").replace("v_sz", "").replace("v_bj", "")

            if len(fields) < 30:
                result[code] = {"name": fields[1] if len(fields) > 1 else "", "price": None}
                continue

            try:
                result[code] = {
                    "name": fields[1],
                    "price": float(fields[3]) if fields[3] else None,
                    "last_close": float(fields[4]) if fields[4] else None,
                    "open": float(fields[5]) if fields[5] else None,
                    "volume": float(fields[6]) if fields[6] else None,
                    "high": float(fields[33]) if len(fields) > 33 and fields[33] else None,
                    "low": float(fields[34]) if len(fields) > 34 and fields[34] else None,
                    "pe_ttm": float(fields[39]) if len(fields) > 39 and fields[39] else None,
                    "pb": float(fields[46]) if len(fields) > 46 and fields[46] else None,
                    "mcap": float(fields[45]) if len(fields) > 45 and fields[45] else None,
                    "float_mcap": float(fields[44]) if len(fields) > 44 and fields[44] else None,
                    "turnover_pct": float(fields[38]) if len(fields) > 38 and fields[38] else None,
                    "change_pct": float(fields[32]) if len(fields) > 32 and fields[32] else None,
                    "amount": float(fields[37]) if len(fields) > 37 and fields[37] else None,
                }
            except (ValueError, IndexError):
                result[code] = {"name": fields[1] if len(fields) > 1 else "", "price": None}

        return result

    # ── Eastmoney Stock Info ────────────────────────────

    def _eastmoney_stock_info(self, code: str) -> dict:
        """Fetch stock basic info from Eastmoney."""
        cache_key = self._cache_key("emi", code)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        self._em_throttle()
        prefix = "SH" if code.startswith("6") else "SZ"
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": f"{1 if prefix == 'SH' else 0}.{code}",
            "fields": "f57,f58,f100,f116,f117,f162,f167,f170",
        }
        try:
            resp = self._session.get(url, params=params, timeout=10)
            data = resp.json().get("data", {})
            result = {
                "code": data.get("f57", code),
                "name": data.get("f58", ""),
                "industry": data.get("f100", ""),
                "total_shares": data.get("f116"),
                "float_shares": data.get("f117"),
            }
            self._cache_set(cache_key, result, ttl=86400)  # 1 day TTL
            return result
        except Exception:
            return {}

    # ── Public API ──────────────────────────────────────

    def get_stock_snapshot(self, code: str) -> StockSnapshot:
        """Get current snapshot for a single stock."""
        data = self._tencent_quote_batch([code])
        info = data.get(code, {})
        return StockSnapshot(
            code=code,
            name=info.get("name", ""),
            price=info.get("price"),
            open=info.get("open"),
            high=info.get("high"),
            low=info.get("low"),
            last_close=info.get("last_close"),
            change_pct=info.get("change_pct"),
            volume=info.get("volume"),
            amount=info.get("amount"),
            pe_ttm=info.get("pe_ttm"),
            pb=info.get("pb"),
            mcap=info.get("mcap"),
            float_mcap=info.get("float_mcap"),
            turnover_pct=info.get("turnover_pct"),
        )

    def get_batch_snapshots(self, codes: list[str]) -> list[StockSnapshot]:
        """Get snapshots for multiple stocks."""
        data = self._tencent_quote_batch(codes)
        results = []
        for code in codes:
            info = data.get(code, {})
            results.append(StockSnapshot(
                code=code,
                name=info.get("name", ""),
                price=info.get("price"),
                open=info.get("open"),
                high=info.get("high"),
                low=info.get("low"),
                last_close=info.get("last_close"),
                change_pct=info.get("change_pct"),
                volume=info.get("volume"),
                amount=info.get("amount"),
                pe_ttm=info.get("pe_ttm"),
                pb=info.get("pb"),
                mcap=info.get("mcap"),
                float_mcap=info.get("float_mcap"),
                turnover_pct=info.get("turnover_pct"),
            ))
        return results

    def get_market_snapshot(self) -> MarketSnapshot:
        """Get current market-wide state."""
        indices = self._tencent_quote_batch(["000001", "399001", "000300"])
        sh = indices.get("000001", {})
        sz = indices.get("399001", {})
        hs300 = indices.get("000300", {})

        return MarketSnapshot(
            date=date.today().isoformat(),
            sh_index=sh.get("price"),
            sz_index=sz.get("price"),
            hs300_index=hs300.get("price"),
        )

    def get_index_history(self, code: str, days: int = 250) -> pd.DataFrame:
        """Get historical index data (not yet implemented).

        Requires mootdx bars() for index K-lines. Currently returns an empty
        frame; callers must handle emptiness rather than assuming data.
        """
        warnings.warn(
            "get_index_history is not implemented — returning empty DataFrame",
            stacklevel=2,
        )
        return pd.DataFrame()

    def get_stock_list(self) -> list[dict]:
        """Get full A-share stock list (not yet implemented).

        Use ``src.data.security_master.SecurityMaster`` /
        ``sync_security_master`` for the stock universe instead.
        """
        warnings.warn(
            "get_stock_list is not implemented — use SecurityMaster instead",
            stacklevel=2,
        )
        return []

    def get_financial_data(self, code: str) -> dict:
        """Get financial statement data for a stock.

        .. warning::
            Sina Finance API has changed and this method currently returns
            empty structures for every report type. Prefer mootdx financial
            data. The warning is emitted once per call to make the emptiness
            visible rather than silent.
        """
        warnings.warn(
            "get_financial_data: Sina API no longer works, returning empty data; "
            "use mootdx finance instead",
            stacklevel=2,
        )
        cache_key = self._cache_key("fin", code)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        result = {}
        prefix = "sh" if code.startswith("6") else ("sz" if code.startswith(("0", "3")) else "bj")

        for report_type, field in [("lrb", "income"), ("fzb", "balance"), ("llb", "cashflow")]:
            try:
                url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vFinanceSummary/{field}.phtml"
                # Note: Sina changed their API; simplified stub
                result[report_type] = []
            except Exception:
                result[report_type] = []

        self._cache_set(cache_key, result, ttl=86400)
        return result


# ═══════════════════════════════════════════════════════════
# Market Data Provider — mootdx-based K-line & snapshot
# ═══════════════════════════════════════════════════════════

class MarketDataProvider:
    """Market data provider using mootdx for daily K-line and snapshots.

    Separated from DataProvider (which uses Tencent/Eastmoney) for clear
    responsibility: this handles price history, DataProvider handles
    real-time quotes and financial data.
    """

    def __init__(self):
        self._quotes = None
        self._kline_cache = {}

    @property
    def quotes(self):
        if self._quotes is None:
            try:
                from mootdx.quotes import Quotes
                self._quotes = Quotes.factory(market='std')
            except ImportError:
                raise ImportError("mootdx required. Install: pip install mootdx")
        return self._quotes

    def get_daily_kline(self, code: str, start_date: str = None,
                         end_date: str = None, adj: str = 'qfq') -> "pd.DataFrame":
        """Return standardized daily K-line via mootdx bars with date filtering.

        Columns: date, open, high, low, close, adj_close, volume, amount, turnover
        """
        import pandas as pd
        from datetime import date

        # Determine offset from date range
        if start_date and end_date:
            try:
                days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
                offset = min(500, max(30, int(days * 1.5) + 20))
            except Exception:
                offset = 250
        else:
            offset = 250

        cache_key = f"{code}_{start_date}_{end_date}"
        if cache_key in self._kline_cache:
            return self._kline_cache[cache_key]

        try:
            # mootdx 0.11.x: bars() with offset (kline API not available)
            df = self.quotes.bars(symbol=code, frequency=9, offset=offset)

            if df is None or df.empty:
                return pd.DataFrame()

            # Standardize: mootdx returns vol, datetime (may also have volume as duplicate)
            if 'vol' in df.columns and 'volume' not in df.columns:
                df = df.rename(columns={'vol': 'volume'})
            elif 'vol' in df.columns:
                df = df.drop(columns=['vol'])  # Already have volume, drop duplicate
            if 'datetime' in df.columns and 'date' not in df.columns:
                df = df.rename(columns={'datetime': 'date'})
            elif 'datetime' in df.columns:
                df = df.drop(columns=['datetime'])

            # Ensure date column is datetime
            if 'date' not in df.columns:
                if 'datetime' in df.columns:
                    df['date'] = pd.to_datetime(df['datetime'])
                    df = df.drop(columns=['datetime'])
                else:
                    df['date'] = pd.to_datetime(df.index)
            else:
                df['date'] = pd.to_datetime(df['date'])

            # Adjusted close
            if 'close' in df.columns:
                df['adj_close'] = df['close']

            # Fill missing
            for col in ['turnover', 'amount']:
                if col not in df.columns or df[col].isna().all():
                    df[col] = float('nan')

            # Client-side date filtering (offset is approximate)
            if start_date:
                df = df[df['date'] >= pd.Timestamp(start_date)]
            if end_date:
                df = df[df['date'] <= pd.Timestamp(end_date)]

            if df.empty:
                return pd.DataFrame()

            self._kline_cache[cache_key] = df
            return df

        except Exception as e:
            logger.warning("provider: get_daily_kline failed for %s: %s", code, e)
            return pd.DataFrame()

    def get_stock_snapshot(self, code: str) -> dict:
        """Get real-time snapshot via mootdx."""
        market = 1 if code.startswith('6') else 0
        try:
            quote = self.quotes.quotes(symbol=[code])
            if quote is not None and not quote.empty:
                row = quote.iloc[0]
                return {
                    'code': code,
                    'name': row.get('name', ''),
                    'price': float(row.get('price', 0) or 0),
                    'open': float(row.get('open', 0) or 0),
                    'high': float(row.get('high', 0) or 0),
                    'low': float(row.get('low', 0) or 0),
                    'volume': int(row.get('vol', 0) or 0),
                    'amount': float(row.get('amount', 0) or 0),
                }
        except Exception as e:
            logger.warning("provider: get_stock_snapshot failed for %s: %s", code, e)
        return {}
