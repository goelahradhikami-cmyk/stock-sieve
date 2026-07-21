"""
Industry Attribution Bootstrap - Commit 6-L.7 Phase 1.2.

Backfills security_master.industry (currently empty string for all 5540
stocks) and builds industry_daily_returns (market-cap-weighted industry
indices) so that Return Attribution can separate Sector Exposure from
Residual Alpha.

Data source: baostock query_stock_industry (证监会行业分类, ~4000 stocks,
82 industries, ~2s total - no per-stock loop needed).

Usage:
    from src.market.industry_bootstrap import IndustryBootstrap
    IndustryBootstrap().backfill_industry()
    IndustryBootstrap().build_industry_daily_returns('2024-01-01')
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)


DDL_INDUSTRY_DAILY_RETURNS = """
CREATE TABLE IF NOT EXISTS industry_daily_returns (
    trade_date  DATE NOT NULL,
    industry    TEXT NOT NULL,
    return      REAL,
    PRIMARY KEY (trade_date, industry)
);
CREATE INDEX IF NOT EXISTS idx_idr_date ON industry_daily_returns(trade_date);
CREATE INDEX IF NOT EXISTS idx_idr_industry ON industry_daily_returns(industry);
"""


def _bare_code(code: str) -> str:
    """sh.600519 -> 600519, 600519.SZ -> 600519, bj.430047 -> 430047."""
    c = str(code).strip()
    if "." in c:
        prefix, suffix = c.split(".", 1)
        # baostock format: "sh.600519" -> take suffix; "600519.SZ" -> take prefix
        c = suffix if prefix.lower() in ("sh", "sz", "bj") else prefix
    return c.zfill(6)


def _industry_short_name(full: str) -> str:
    """J66货币金融服务 -> 货币金融服务 (strip the leading code prefix)."""
    if not full:
        return ""
    #证监会格式: "J66货币金融服务" -> 取中文名
    import re
    m = re.match(r'^[A-Z]\d+(.*)', full)
    return m.group(1) if m else full


class IndustryBootstrap:
    """Backfill industry classification + build industry daily returns."""

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db

    def _ensure_table(self):
        conn = sqlite3.connect(self.cache_db)
        conn.executescript(DDL_INDUSTRY_DAILY_RETURNS)
        conn.commit()
        conn.close()

    def backfill_industry(self) -> int:
        """Fetch industry classification via baostock, write to security_master.

        Returns: number of stocks updated.
        """
        import baostock as bs
        lg = bs.login()
        if lg.error_code != '0':
            raise ConnectionError(f"baostock login failed: {lg.error_msg}")

        try:
            rs = bs.query_stock_industry()
            rows = []
            while rs.error_code == '0' and rs.next():
                row = dict(zip(rs.fields, rs.get_row_data()))
                rows.append(row)
        finally:
            bs.logout()

        if not rows:
            logger.warning("industry_bootstrap: no data from baostock")
            return 0

        conn = sqlite3.connect(self.cache_db)
        updated = 0
        try:
            for row in rows:
                code = _bare_code(row.get("code", ""))
                industry_full = row.get("industry", "")
                if not code or not industry_full:
                    continue
                industry = _industry_short_name(industry_full)
                n = conn.execute(
                    "UPDATE security_master SET industry=? WHERE code=?",
                    (industry, code),
                ).rowcount
                if n > 0:
                    updated += 1
            conn.commit()
        finally:
            conn.close()

        logger.info("industry_bootstrap: updated %d stocks with industry", updated)
        return updated

    def build_industry_daily_returns(self, start_date: str = "2024-01-01",
                                      end_date: str | None = None) -> int:
        """Build market-cap-weighted industry daily returns from stock K-lines.

        For each trade date, group stocks by industry, compute the
        market-cap-weighted average return of stocks in that industry.

        Returns: number of (date, industry) rows written.

        NOTE: this reads stock daily returns from local TDX .day files via
        LocalDataProvider. For ~5000 stocks × 500 days this is CPU-bound but
        offline. Cap at a sample if too slow.
        """
        self._ensure_table()
        from src.data.local_provider import LocalDataProvider
        local = LocalDataProvider()

        conn = sqlite3.connect(self.cache_db)
        # Get stocks with industry + market cap
        stock_rows = conn.execute(
            "SELECT code, industry, total_mv FROM security_master "
            "WHERE status='active' AND is_st=0 AND industry != '' AND industry IS NOT NULL "
            "ORDER BY code"
        ).fetchall()
        conn.close()

        if not stock_rows:
            logger.warning("industry_bootstrap: no stocks with industry - run backfill_industry first")
            return 0

        logger.info("industry_bootstrap: %d stocks with industry, building returns", len(stock_rows))

        # End date default = today
        if end_date is None:
            end_date = date.today().isoformat()

        # Phase 1: compute per-stock daily returns, group by industry+date
        # To keep it fast, sample at most 1000 stocks (enough for industry weights)
        if len(stock_rows) > 1000:
            import random
            stock_rows = random.sample(stock_rows, 1000)

        industry_date_returns: dict[tuple[str, str], list[tuple[float, float]]] = {}
        # {(industry, date): [(return, market_cap), ...]}

        for i, (code, industry, mv) in enumerate(stock_rows):
            try:
                kline = local.get_daily_kline(code, start_date, end_date)
                if kline is None or kline.empty or len(kline) < 2:
                    continue
                closes = kline["close"].values
                dates = kline["date"].values if "date" in kline.columns else kline.index
                market_cap = float(mv) if mv else 1.0
                # daily returns
                rets = np.diff(closes) / closes[:-1]
                for j, ret in enumerate(rets):
                    if np.isnan(ret) or ret == 0:
                        continue
                    d = str(dates[j + 1])[:10]
                    key = (industry, d)
                    if key not in industry_date_returns:
                        industry_date_returns[key] = []
                    industry_date_returns[key].append((float(ret), market_cap))
            except Exception:
                pass
            if (i + 1) % 200 == 0:
                logger.info("industry_bootstrap: %d/%d stocks processed", i + 1, len(stock_rows))

        # Phase 2: compute market-cap-weighted average per (industry, date)
        conn = sqlite3.connect(self.cache_db)
        written = 0
        try:
            for (industry, d), rets_mvs in industry_date_returns.items():
                total_mv = sum(mv for _, mv in rets_mvs)
                if total_mv <= 0:
                    continue
                weighted_ret = sum(r * mv for r, mv in rets_mvs) / total_mv
                conn.execute(
                    "INSERT OR REPLACE INTO industry_daily_returns (trade_date, industry, return) VALUES (?, ?, ?)",
                    (d, industry, float(weighted_ret)),
                )
                written += 1
            conn.commit()
        finally:
            conn.close()

        logger.info("industry_bootstrap: wrote %d (date, industry) return rows", written)
        return written

    def get_industry_return(self, trade_date: str, industry: str) -> float | None:
        """Read a single industry's return for a date."""
        conn = sqlite3.connect(self.cache_db)
        try:
            row = conn.execute(
                "SELECT return FROM industry_daily_returns WHERE trade_date=? AND industry=?",
                (trade_date, industry),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def get_industry_distribution(self) -> dict[str, int]:
        """Count of stocks per industry."""
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT industry, COUNT(*) FROM security_master "
                "WHERE status='active' AND industry != '' GROUP BY industry "
                "ORDER BY COUNT(*) DESC LIMIT 20"
            ).fetchall()
            return {r[0]: r[1] for r in rows}
        finally:
            conn.close()
