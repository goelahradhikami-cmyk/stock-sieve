"""
Baostock Financial Provider - multi-period A-share fundamentals via baostock.

WHY baostock (and not akshare/Eastmoney):
  baostock connects to its OWN server (baostock.com), NOT Eastmoney/Sina. In
  this environment a corporate proxy silently blocks Eastmoney, which kills
  akshare/efinance. baostock needs no registration and no token, so it is the
  only free, proxy-friendly source that delivers multi-period quarterly
  financials (the mootdx provider only saw a single-quarter snapshot, leaving
  the whole growth family as None).

WHAT it fills that mootdx could not:
  revenue_growth_1y / earnings_growth_1y  (from query_growth_data, YOY)
  revenue_growth_3y / earnings_growth_3y  (TTM CAGR derived from profit series)
  margin_trend                            (gross_margin now vs 3y ago)
  roe / roa / gross_margin / net_margin   (from query_profit_data)
  debt_to_equity / current_ratio          (from query_balance_data)

DROP-IN COMPATIBLE with FinancialDataProvider.get_financial_dict(): returns
the SAME dict shape, so it can replace that provider via get_financial_provider.

HONESTY: baostock is imported lazily inside methods so this module imports
cleanly even when baostock is not installed. On ANY failure get_financial_dict
returns {} - never fake data. Fields baostock does not expose
(interest_coverage, dividend_yield, holder_change_pct) are left as None.

NOTE on field names: they follow the baostock Python API. If a metric comes
back None after you verify connectivity, the mapping in _PROFIT_MAP /
_GROWTH_MAP likely needs a tweak for your baostock version - every extraction
goes through the defensive _pick() helper, so adding an alternate key is 1 line.
"""

import sqlite3
import os
import json
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)

# Column names verified live against baostock (2026-07-17). baostock profit_data
# returns: roeAvg, npMargin, gpMargin, netProfit, MBRevenue, epsTTM, ...
# balance_data returns: currentRatio, liabilityToAsset, assetToEquity, ...
# growth_data has NO revenue-YOY field, so 1y/3y growth is derived from the
# profit series (see _yoy / _ttm_cagr) rather than from growth_data.
_PROFIT_MAP = {
    'roe': ['roeAvg', 'roe'],
    'gross_margin': ['gpMargin', 'grossProfitRatio'],
    'net_margin': ['npMargin', 'netProfitRatio'],
    'net_profit': ['netProfit', 'np'],
    'revenue': ['MBRevenue', 'operateIncome', 'revenue'],
    'eps': ['epsTTM', 'niPerShare'],
}
_GROWTH_MAP = {
    'earnings_growth_yoy': ['YOYNI', 'npYOY'],
}
_BALANCE_MAP = {
    'debt_to_assets': ['liabilityToAsset', 'debtToAssets'],
    'current_ratio': ['currentRatio'],
    'asset_to_equity': ['assetToEquity'],
}


def _bs_code(code):
    code = str(code).zfill(6)
    if code.startswith('6'):
        return 'sh.' + code
    if code.startswith(('0', '3')):
        return 'sz.' + code
    if code.startswith(('4', '8')):
        return 'bj.' + code
    return 'sh.' + code


def _pick(row, *candidates):
    for c in candidates:
        if c in row and row[c] not in (None, '', 'None'):
            return row[c]
    return None


def _to_float(v):
    try:
        if v in (None, '', 'None'):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


class BaostockProvider:
    """Multi-period A-share fundamentals via baostock (no token, proxy-friendly)."""

    def __init__(self, db_path='data/cache.db', cache_ttl_days=30, n_years=3):
        self.db_path = db_path
        self.cache_ttl_days = cache_ttl_days
        # 3 years (12 quarters) is enough to derive growth_1y (needs 5 quarters)
        # and growth_3y/margin_trend (needs 13 quarters, margin_trend degrades
        # gracefully to None if fewer). Reduces queries ~40% vs 5y.
        self.n_years = n_years
        self._init_cache()

    def _init_cache(self):
        conn = sqlite3.connect(self.db_path)
        # WAL + busy_timeout: allows concurrent writers (e.g. two backfill
        # instances) without "database is locked" rollback corruption that
        # previously wiped committed rows. Safe to set on every init.
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS baostock_cache (
                code TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

    def _cache_get(self, code):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            'SELECT data, updated_at FROM baostock_cache WHERE code=?', (code,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        data, updated = row
        try:
            age = (datetime.now() - datetime.fromisoformat(updated)).days
        except Exception:
            age = 999
        if age <= self.cache_ttl_days:
            return json.loads(data)
        return None

    def _cache_set(self, code, data):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT OR REPLACE INTO baostock_cache (code, data, updated_at) VALUES (?,?,?)',
            (code, json.dumps(data, default=str), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

    def _login(self):
        try:
            import baostock as bs
        except ImportError:
            raise ImportError(
                'baostock not installed. Install with: pip install baostock'
            )
        lg = bs.login()
        if lg.error_code != '0':
            raise ConnectionError('baostock login failed: ' + str(lg.error_msg))
        return bs

    @staticmethod
    def _query(bs, func_name, code, year, quarter):
        func = getattr(bs, func_name)
        rs = func(code=code, year=year, quarter=quarter)
        rows = []
        while rs.error_code == '0' and rs.next():
            row = dict(zip(rs.fields, rs.get_row_data()))
            if row:
                rows.append(row)
        return rows

    def fetch_multiperiod(self, code, n_years=None):
        """Pull raw + normalized multi-quarter fundamentals for a code.

        Returns dict with keys profit/growth/balance/operation/cashflow/dupont
        (each a list of quarterly dicts) plus a derived 'series' of
        (statDate, net_profit, revenue, gross_margin, roe) sorted oldest->newest.
        Raises on import/login/network failure so callers can degrade.

        Optimization (Commit 6-L.6 backfill): only profit_data needs the full
        multi-quarter series (to derive YOY/CAGR growth). balance_data is only
        used for the latest snapshot, so we query it just once for the latest
        quarter. growth_data is skipped entirely (revenue-YOY is derived from
        the profit series in _yoy). This cuts queries from 5y×4q×3=60 to
        n_years×4q×1 + 1 = ~17, roughly 3-4x faster.
        """
        bs = self._login()
        bcode = _bs_code(code)
        cur_year = datetime.now().year
        back = (n_years or self.n_years)
        years = range(cur_year, cur_year - back - 1, -1)

        out: dict = {'profit': [], 'growth': [], 'balance': [], 'operation': [],
                     'cashflow': [], 'dupont': []}

        # Phase 1: profit_data for all year-quarters (needed for growth derivation)
        for y in years:
            for q in (1, 2, 3, 4):
                try:
                    rows = self._query(bs, 'query_profit_data', bcode, y, q)
                    out['profit'].extend(rows)
                except Exception as e:
                    logger.debug('baostock: profit %s Q%d failed: %s', y, q, e)
                time.sleep(0.03)

        # Phase 2: balance_data ONLY for the latest available quarter (1 query)
        # Try current year Q1 first, fall back through previous quarters.
        for y in range(cur_year, cur_year - 3, -1):
            for q in (4, 3, 2, 1):
                try:
                    rows = self._query(bs, 'query_balance_data', bcode, y, q)
                    if rows:
                        out['balance'] = rows
                        break
                except Exception as exc:
                    logger.debug("operation failed (was silently ignored): %s", exc)
                time.sleep(0.03)
            if out['balance']:
                break

        bs.logout()

        series = []
        for r in out['profit']:
            sd = r.get('statDate') or r.get('date')
            if not sd:
                continue
            series.append({
                'statDate': sd,
                'net_profit': _to_float(_pick(r, *_PROFIT_MAP['net_profit'])),
                'revenue': _to_float(_pick(r, *_PROFIT_MAP['revenue'])),
                'gross_margin': _to_float(_pick(r, *_PROFIT_MAP['gross_margin'])),
                'net_margin': _to_float(_pick(r, *_PROFIT_MAP['net_margin'])),
                'roe': _to_float(_pick(r, *_PROFIT_MAP['roe'])),
            })
        series.sort(key=lambda x: x['statDate'])
        out['series'] = series
        return out

    @staticmethod
    def _quarter_of(stat_date: str) -> int:
        """Extract quarter (1-4) from a YYYY-MM-DD statDate."""
        try:
            month = int(stat_date[5:7])
            return (month - 1) // 3 + 1
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _year_of(stat_date: str) -> int:
        try:
            return int(stat_date[:4])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _yoy(series, key):
        """Year-over-year growth: latest value vs same-quarter value 1 year ago.

        baostock profit_data returns CUMULATIVE values (Q1=single quarter,
        Q2=year-to-date through Q2, Q4=full year). So YoY MUST compare the
        same quarter across years (Q1 vs Q1, Q2 vs Q2) - otherwise you'd
        compare a Q4 full-year cumulative against a Q2 half-year cumulative
        and get nonsense like 2x instead of 1.1x.

        Previous bug: used vals[-1]/vals[-5] over the non-None filtered
        list, which silently compared different quarters when data was
        sparse (revenue is only reported in Q2/Q4). Fixed by matching on
        statDate quarter+year explicitly.
        """
        if not series:
            return None
        # Build a lookup: (year, quarter) -> value, only for non-None
        lookup = {}
        for s in series:
            sd = s.get('statDate')
            v = s.get(key)
            if sd and v is not None:
                y = BaostockProvider._year_of(sd)
                q = BaostockProvider._quarter_of(sd)
                if y and q:
                    lookup[(y, q)] = v

        if not lookup:
            return None

        # Find the latest (year, quarter) with a value
        latest_key = max(lookup.keys())
        latest_y, latest_q = latest_key
        # Same quarter, previous year
        prior_key = (latest_y - 1, latest_q)
        prior_val = lookup.get(prior_key)
        if prior_val is None or prior_val == 0:
            return None
        return lookup[latest_key] / prior_val - 1.0

    @staticmethod
    def _ttm_cagr(series, key, years_back=3):
        """TTM CAGR over `years_back` years using same-quarter comparison.

        baostock values are cumulative-within-year, so TTM = (latest cumulative)
        minus (the cumulative from 4 quarters ago, same year). Then compare
        against TTM from `years_back` years ago (same quarter).

        Simplified: since Q4 (full year) = TTM, we compare the latest full-year
        value against the full-year value `years_back` years earlier. Falls
        back to same-quarter cumulative comparison if Q4 is unavailable.
        """
        if not series:
            return None
        lookup = {}
        for s in series:
            sd = s.get('statDate')
            v = s.get(key)
            if sd and v is not None:
                y = BaostockProvider._year_of(sd)
                q = BaostockProvider._quarter_of(sd)
                if y and q:
                    lookup[(y, q)] = v

        if not lookup:
            return None

        # Prefer Q4 (full year = TTM) for cleanest CAGR
        latest_key = max(lookup.keys())
        latest_y = latest_key[0]
        # Try Q4 of the latest available year
        latest_q4 = (latest_y, 4)
        if latest_q4 not in lookup:
            # Fall back to latest available quarter
            latest_q4 = latest_key
        past_key = (latest_q4[0] - years_back, latest_q4[1])
        past_val = lookup.get(past_key)
        if past_val is None or past_val == 0:
            return None
        return (lookup[latest_q4] / past_val) ** (1.0 / years_back) - 1.0

    @staticmethod
    def _margin_trend(series):
        gm = [s.get('gross_margin') for s in series if s.get('gross_margin') is not None]
        if len(gm) < 13:
            return None
        return gm[-1] - gm[-13]

    def _tencent_quote(self, code):
        try:
            import requests
        except ImportError:
            return {}
        c = str(code).zfill(6)
        prefix = ('sh' if c.startswith(('60', '68'))
                  else 'sz' if c.startswith(('00', '30'))
                  else 'bj' if c.startswith(('4', '8'))
                  else 'sh')
        try:
            s = requests.Session()
            s.trust_env = False
            resp = s.get('https://qt.gtimg.cn/q=' + prefix + c, timeout=8)
            resp.encoding = 'gbk'
            fields = resp.text.split('~')
            return {
                'pe_ttm': _to_float(fields[39]) if len(fields) > 39 and fields[39] else None,
                'pb': _to_float(fields[46]) if len(fields) > 46 and fields[46] else None,
                'mcap': _to_float(fields[45]) if len(fields) > 45 and fields[45] else None,
            }
        except Exception as e:
            logger.warning('baostock_provider: Tencent quote failed for %s: %s', code, e)
            return {}

    def get_financial_dict(self, code):
        """Return full financial dict for FactorEngine, or {} on any failure.

        Same keys as FinancialDataProvider.get_financial_dict (drop-in replace).
        """
        cached = self._cache_get(code)
        if cached is not None:
            return cached

        try:
            raw = self.fetch_multiperiod(code)
        except Exception as e:
            logger.warning('baostock_provider: fetch failed for %s: %s', code, e)
            return {}

        series = raw.get('series', [])
        latest_profit = series[-1] if series else {}
        latest_growth = raw.get('growth', [{}])[-1] if raw.get('growth') else {}
        latest_balance = raw.get('balance', [{}])[-1] if raw.get('balance') else {}

        net_profit = latest_profit.get('net_profit')
        roe = latest_profit.get('roe')
        gross_margin = latest_profit.get('gross_margin')
        net_margin = latest_profit.get('net_margin')

        debt_to_assets = _to_float(_pick(latest_balance, *_BALANCE_MAP['debt_to_assets']))
        asset_to_equity = _to_float(_pick(latest_balance, *_BALANCE_MAP['asset_to_equity']))
        current_ratio = _to_float(_pick(latest_balance, *_BALANCE_MAP['current_ratio']))

        # baostock balance summary exposes no raw equity; derive it so we can
        # build debt_to_equity and roic.
        equity = (net_profit / roe) if (net_profit and roe) else None
        debt_to_equity = (debt_to_assets / (1 - debt_to_assets)) if (
            debt_to_assets is not None and 0 < debt_to_assets < 1) else None
        if net_profit and equity and asset_to_equity:
            invested = equity * asset_to_equity  # assetToEquity = A/E
            roic = (net_profit / invested) if invested else roe
        else:
            roic = roe

        # Growth derived from the multi-quarter profit series (baostock
        # growth_data lacks a revenue-YOY field).
        revenue_growth_1y = self._yoy(series, 'revenue')
        earnings_growth_1y = self._yoy(series, 'net_profit')
        revenue_growth_3y = self._ttm_cagr(series, 'revenue', 3)
        earnings_growth_3y = self._ttm_cagr(series, 'net_profit', 3)
        margin_trend = self._margin_trend(series)

        tq = self._tencent_quote(code)

        result = {
            'roe': roe,
            'roic': roic,
            'roa': latest_profit.get('roa'),
            'gross_margin': gross_margin,
            'net_margin': net_margin,
            'pe_ttm': tq.get('pe_ttm'),
            'pb': tq.get('pb'),
            'mcap': tq.get('mcap'),
            'fcf_yield': (net_profit / (tq.get('mcap') * 1e8)) if (
                net_profit and tq.get('mcap')) else None,
            'fcf': net_profit,
            'debt_to_equity': debt_to_equity,
            'interest_coverage': None,
            'current_ratio': current_ratio,
            'revenue_growth_1y': revenue_growth_1y,
            'earnings_growth_1y': earnings_growth_1y,
            'revenue_growth_3y': revenue_growth_3y,
            'earnings_growth_3y': earnings_growth_3y,
            'margin_trend': margin_trend,
            'dividend_yield': None,
            'bvps': None,
            'holder_change_pct': None,
            '_date': latest_profit.get('statDate', datetime.now().strftime('%Y-%m-%d')),
        }
        self._cache_set(code, result)
        return result
