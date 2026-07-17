"""
Financial Data Provider — Real A-share financial data via mootdx.

Uses mootdx Quotes.finance() which returns 37-field quarterly snapshot.
Computes derived financial metrics for FactorEngine consumption.
"""

import sqlite3
from src.data.db import managed_connect
import os
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np

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
            q = Quotes.factory(market='std')
            data = q.finance(symbol=code)

            if data is None or data.empty:
                return {}

            row = data.iloc[0]
            result = {}
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
        pe_ttm = pb = mcap = None
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
            "current_ratio": (raw.get("current_liabilities") or 0) and (
                (raw.get("total_assets") or 0) / (raw.get("current_liabilities") or 1)
            ),
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

    def _load_cached(self, code: str) -> dict:
        """Load cached financial snapshot."""
        try:
            cur = self.db.execute(
                "SELECT * FROM finance_snapshots WHERE code=? ORDER BY date DESC LIMIT 1",
                (code,)
            )
            row = cur.fetchone()
            if not row:
                return {}
            cols = ["code", "date", "net_profit", "revenue", "operating_profit",
                    "equity", "total_assets", "current_liabilities", "long_term_liabilities",
                    "bvps", "float_shares", "total_shares", "shareholders"]
            result = {}
            for i, col in enumerate(cols):
                if i < len(row):
                    result[col] = row[i]
            return result
        except Exception:
            return {}

    def _save(self, code: str, data: dict):
        self.db.execute("""
            INSERT OR REPLACE INTO finance_snapshots
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            code, data.get("_date", ""),
            data.get("net_profit"), data.get("revenue"), data.get("operating_profit"),
            data.get("equity"), data.get("total_assets"),
            data.get("current_liabilities"), data.get("long_term_liabilities"),
            data.get("bvps"), data.get("float_shares"), data.get("total_shares"),
            data.get("shareholders"),
        ))
        self.db.commit()
