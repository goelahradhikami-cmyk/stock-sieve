"""
Financial Data Provider — Real A-share financial data via mootdx.

Uses mootdx Quotes.finance() which returns 37-field quarterly snapshot.
Computes derived financial metrics for FactorEngine consumption.
"""

import contextlib
import os
from datetime import datetime
from typing import Any

import pandas as pd

from src.data.db import managed_connect
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Column mapping: mootdx pinyin → English
FINANCE_MAP = {
    "jinglirun": "net_profit",
    "zhuyingshouru": "revenue",
    "zhuyinglirun": "operating_profit",
    "jingzichan": "equity",
    "zongzichan": "total_assets",
    "liudongfuzhai": "current_liabilities",
    "changqifuzhai": "long_term_liabilities",
    "meigujingzichan": "bvps",
    "liutongguben": "float_shares",
    "zongguben": "total_shares",
    "gudongrenshu": "shareholders",
    "yingyelirun": "operating_income",
}


class FinancialDataProvider:
    """Fetch A-share financial snapshots via mootdx."""

    def __init__(self, db_path: str = "data/cache.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = managed_connect(self, db_path)
        self._ensure_tables()

    def _ensure_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS finance_snapshots (
                code TEXT, date TEXT,
                net_profit REAL, revenue REAL, operating_profit REAL,
                equity REAL, total_assets REAL,
                current_liabilities REAL, long_term_liabilities REAL,
                bvps REAL, float_shares REAL, total_shares REAL,
                shareholders INTEGER,
                PRIMARY KEY (code, date)
            );
        """)
        # Commit 6-L.6: persist PE/PB/mcap so we don't re-fetch from Tencent
        # on every call (was the #1 bottleneck for 5000-stock backfill).
        for col, col_type in [
            ("pe_ttm", "REAL"),
            ("pb", "REAL"),
            ("mcap", "REAL"),  # 亿元
            ("float_mcap", "REAL"),  # 亿元
            ("turnover_pct", "REAL"),
        ]:
            with contextlib.suppress(Exception):  # idempotent - column may already exist
                self.db.execute(f"ALTER TABLE finance_snapshots ADD COLUMN {col} {col_type}")
        self.db.commit()

    def fetch_latest(self, code: str) -> dict:
        """Fetch latest financial snapshot via mootdx.

        Returns dict with: net_profit, revenue, equity, total_assets,
        current_liabilities, long_term_liabilities, bvps, float_shares, total_shares
        """
        # Check cache
        cached = self._load_cached(code)
        if cached:
            return cached

        try:
            from mootdx.quotes import Quotes

            q = Quotes.factory(market="std")
            data = q.finance(symbol=code)

            if data is None or data.empty:
                return {}

            row = data.iloc[0]
            result: dict[str, Any] = {}
            for cn_key, en_key in FINANCE_MAP.items():
                if cn_key in row.index:
                    val = row[cn_key]
                    result[en_key] = float(val) if pd.notna(val) and val != 0 else None

            result["_date"] = str(row.get("updated_date", datetime.now().strftime("%Y-%m-%d")))[:10]
            result["_code"] = code

            # Cache it
            self._save(code, result)
            return result

        except ImportError:
            logger.warning("financials: mootdx not installed. Install: pip install mootdx")
            return {}
        except Exception as e:
            logger.warning("financials: fetch failed for %s: %s", code, e)
            return {}

    def get_financial_dict(self, code: str) -> dict:
        """Get financial data formatted for FactorEngine.

        Returns dict with keys matching FactorEngine expectations:
          roe, roe_5y_avg, roic, gross_margin, net_margin, pe_ttm, pb,
          fcf_yield, debt_to_equity, interest_coverage,
          revenue_growth_1y, earnings_growth_1y, margin_trend,
          mcap
        """
        raw = self.fetch_latest(code)

        # Get market data from Tencent for PE/PB/mcap.
        # trust_env=False bypasses the system proxy — qt.gtimg.cn is otherwise
        # blocked behind a corporate proxy, which used to leave PE/PB/mcap None
        # and collapse the whole value family to a neutral 50.
        pe_ttm = pb = mcap = float_mcap = turnover_pct = None
        cached_market = self._load_cached_market(code)
        if cached_market:
            pe_ttm = cached_market.get("pe_ttm")
            pb = cached_market.get("pb")
            mcap = cached_market.get("mcap")
            float_mcap = cached_market.get("float_mcap")
            turnover_pct = cached_market.get("turnover_pct")

        if pe_ttm is None:
            # Not cached - fetch from Tencent (trust_env=False bypasses proxy)
            try:
                import requests

                c = str(code).zfill(6)
                if c.startswith(("60", "68")):
                    prefix = "sh"
                elif c.startswith(("00", "30")):
                    prefix = "sz"
                elif c.startswith(("4", "8", "9")):
                    prefix = "bj"
                else:
                    prefix = "sh"
                s = requests.Session()
                s.trust_env = False
                url = f"https://qt.gtimg.cn/q={prefix}{c}"
                resp = s.get(url, timeout=8)
                resp.encoding = "gbk"
                fields = resp.text.split("~")
                if len(fields) > 46:
                    pe_ttm = float(fields[39]) if fields[39] else None
                    pb = float(fields[46]) if fields[46] else None
                    mcap = float(fields[45]) if fields[45] else None  # 亿元
                    if len(fields) > 44:
                        float_mcap = float(fields[44]) if fields[44] else None  # 流通市值亿元
                    if len(fields) > 38:
                        turnover_pct = float(fields[38]) if fields[38] else None
                # Persist to cache so next call doesn't re-fetch
                if pe_ttm is not None or mcap is not None:
                    self._save_market_data(
                        code, raw.get("_date", ""), pe_ttm, pb, mcap, float_mcap, turnover_pct
                    )
            except Exception as e:
                logger.warning("financials: Tencent quote fetch failed for %s: %s", code, e)

        net_profit = raw.get("net_profit") or 0
        revenue = raw.get("revenue") or 0
        equity = raw.get("equity") or 1
        total_assets = raw.get("total_assets") or 1
        cl = raw.get("current_liabilities") or 0
        ltl = raw.get("long_term_liabilities") or 0
        total_liabilities = cl + ltl

        # ── Compute derived metrics ───────────────────────
        roe = (net_profit / equity) if equity and net_profit else None
        roa = (net_profit / total_assets) if total_assets and net_profit else None

        # ROIC = NOPAT / (equity + debt)
        invested_capital = equity + total_liabilities
        roic = (net_profit / invested_capital) if invested_capital and net_profit else (roe or None)

        # Net margin
        net_margin = (net_profit / revenue) if revenue and net_profit else None

        # Debt to equity
        debt_to_equity = (total_liabilities / equity) if equity else None

        # FCF yield (approximate: net_profit/mcap since no detailed CF data)
        fcf_yield = (net_profit / (mcap * 1e8)) if mcap and net_profit else None

        return {
            "roe": roe,
            "roic": roic,
            "roa": roa,
            "gross_margin": None,  # Not available from this data source
            "net_margin": net_margin,
            "pe_ttm": pe_ttm,
            "pb": pb,
            "mcap": mcap,
            "fcf_yield": fcf_yield,
            "fcf": net_profit,  # Approximate
            "debt_to_equity": debt_to_equity,
            "interest_coverage": None,
            "current_ratio": (raw.get("current_liabilities") or 0)
            and ((raw.get("total_assets") or 0) / (raw.get("current_liabilities") or 1)),
            "revenue_growth_1y": None,  # Need historical data
            "earnings_growth_1y": None,
            "earnings_growth_3y": None,
            "revenue_growth_3y": None,
            "margin_trend": None,
            "dividend_yield": None,
            "bvps": raw.get("bvps"),
            "holder_change_pct": None,
            "_date": raw.get("_date", ""),
        }

    # ── Internal helpers ──────────────────────────────────

    def _load_cached_market(self, code: str) -> dict:
        """Load cached PE/PB/mcap for a code (Commit 6-L.6 persistence)."""
        try:
            cur = self.db.execute(
                "SELECT pe_ttm, pb, mcap, float_mcap, turnover_pct "
                "FROM finance_snapshots WHERE code=? "
                "AND pe_ttm IS NOT NULL ORDER BY date DESC LIMIT 1",
                (code,),
            )
            row = cur.fetchone()
            if not row:
                return {}
            return {
                "pe_ttm": row[0],
                "pb": row[1],
                "mcap": row[2],
                "float_mcap": row[3],
                "turnover_pct": row[4],
            }
        except Exception:
            return {}

    def _save_market_data(
        self, code: str, date_str: str, pe_ttm, pb, mcap, float_mcap, turnover_pct
    ) -> None:
        """Persist PE/PB/mcap into the latest finance_snapshots row."""
        try:
            self.db.execute(
                """
                UPDATE finance_snapshots
                SET pe_ttm=?, pb=?, mcap=?, float_mcap=?, turnover_pct=?
                WHERE code=? AND date=(
                    SELECT date FROM finance_snapshots WHERE code=?
                    ORDER BY date DESC LIMIT 1)
            """,
                (pe_ttm, pb, mcap, float_mcap, turnover_pct, code, code),
            )
            self.db.commit()
        except Exception as e:
            logger.debug("financials: save_market_data failed for %s: %s", code, e)

    def _load_cached(self, code: str) -> dict:
        """Load cached financial snapshot."""
        try:
            cur = self.db.execute(
                "SELECT * FROM finance_snapshots WHERE code=? ORDER BY date DESC LIMIT 1", (code,)
            )
            row = cur.fetchone()
            if not row:
                return {}
            cols = [
                "code",
                "date",
                "net_profit",
                "revenue",
                "operating_profit",
                "equity",
                "total_assets",
                "current_liabilities",
                "long_term_liabilities",
                "bvps",
                "float_shares",
                "total_shares",
                "shareholders",
            ]
            result = {}
            for i, col in enumerate(cols):
                if i < len(row):
                    result[col] = row[i]
            return result
        except Exception:
            return {}

    def _save(self, code: str, data: dict):
        """Insert/replace a financial snapshot row.

        Uses explicit column names (not positional VALUES) so that the
        6-L.6 market-data columns (pe_ttm/pb/mcap/float_mcap/turnover_pct)
        are left NULL here and filled separately by _save_market_data.
        """
        self.db.execute(
            """
            INSERT OR REPLACE INTO finance_snapshots
            (code, date, net_profit, revenue, operating_profit,
             equity, total_assets, current_liabilities, long_term_liabilities,
             bvps, float_shares, total_shares, shareholders)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                code,
                data.get("_date", ""),
                data.get("net_profit"),
                data.get("revenue"),
                data.get("operating_profit"),
                data.get("equity"),
                data.get("total_assets"),
                data.get("current_liabilities"),
                data.get("long_term_liabilities"),
                data.get("bvps"),
                data.get("float_shares"),
                data.get("total_shares"),
                data.get("shareholders"),
            ),
        )
        self.db.commit()
