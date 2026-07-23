"""
Return Attribution Engine - Commit 6-L.7 Phase 2.2.

Splits a doctrine portfolio's total return into:
  Total Return = Market Beta + Sector Exposure + Residual Alpha

This is the X-lite sector attribution (per user decision): market beta from
the 000300 benchmark, sector exposure from market-cap-weighted industry
returns (self-built in industry_bootstrap), residual alpha is what's left.

Why this matters: without attribution, evolution rewards "who made money"
not "who has true skill". A momentum doctrine loaded on AI stocks during an
AI bull run would show +15% "alpha" that's really just sector beta - and
the evolution engine would over-breed momentum into a high-beta monster.
residual_alpha is the fitness basis: it's what the doctrine adds beyond
passive market + sector exposure.

Usage:
    from src.evolution.attribution import ReturnAttribution
    attr = ReturnAttribution()
    result = attr.attribute(
        portfolio_return=0.15,
        benchmark_return=0.03,
        portfolio_industry_weights={"半导体": 0.4, "电池": 0.3, "银行": 0.3},
        trade_date="2026-05-27",
    )
    # result = {"total": 0.15, "market_beta": 0.03, "sector": 0.07, "residual_alpha": 0.05}
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AttributionResult:
    """Result of decomposing a portfolio return."""

    total_return: float
    market_beta: float  # benchmark return (simplified: beta=1.0)
    sector_return: float  # Σ(industry_weight × industry_return)
    residual_alpha: float  # total - market_beta - sector_return

    def to_dict(self) -> dict:
        return {
            "total": self.total_return,
            "market_beta": self.market_beta,
            "sector": self.sector_return,
            "residual_alpha": self.residual_alpha,
        }


class ReturnAttribution:
    """Decompose portfolio return into market beta + sector + residual alpha.

    Phase 1 (X-lite): uses 1.0 market beta (simplified - assumes the doctrine
    portfolio carries market beta). Sector exposure uses self-built
    industry_daily_returns. residual_alpha is the remainder.
    Full factor-model regression (Fama-French / Brinson) is deferred to 6-M.
    """

    def __init__(self, cache_db: str = "data/cache.db"):
        self.cache_db = cache_db

    def attribute(
        self,
        portfolio_return: float,
        benchmark_return: float,
        portfolio_industry_weights: dict[str, float],
        trade_date: str,
        holding_days: int = 20,
    ) -> AttributionResult:
        """Attribute a portfolio return over a holding period.

        Args:
            portfolio_return: total return of the doctrine's picks (e.g. 0.15 = +15%)
            benchmark_return: 000300 return over the same period (e.g. 0.03)
            portfolio_industry_weights: {industry_name: weight} from the picks,
                weights summing to ~1.0 (e.g. {"半导体": 0.4, "电池": 0.3})
            trade_date: the selection date (start of holding period)
            holding_days: number of trading days held (for cumulating industry returns)

        Returns: AttributionResult with total/beta/sector/residual_alpha
        """
        # Market beta: simplified to 1.0 × benchmark return.
        # (Full version would regress portfolio vs benchmark to get beta; deferred to 6-M.)
        market_beta = benchmark_return

        # Sector exposure: weight × industry cumulative return over holding period
        sector_return = self._sector_contribution(
            portfolio_industry_weights, trade_date, holding_days
        )

        # Residual alpha: what's left after market + sector
        residual_alpha = portfolio_return - market_beta - sector_return

        return AttributionResult(
            total_return=portfolio_return,
            market_beta=market_beta,
            sector_return=sector_return if sector_return is not None else 0.0,
            residual_alpha=residual_alpha,
        )

    def _sector_contribution(
        self, industry_weights: dict[str, float], trade_date: str, holding_days: int
    ) -> float:
        """Compute Σ(industry_weight × industry_cumulative_return).

        Reads industry_daily_returns for the holding window starting at
        trade_date, sums each industry's returns, then weights by the
        portfolio's industry exposure.
        """
        if not industry_weights:
            return 0.0

        # Find the holding window end date (trade_date + holding_days trading days)
        # industry_daily_returns has trade_date as the actual date of each daily return
        conn = sqlite3.connect(self.cache_db)
        try:
            # Get all industry returns in the window [trade_date, trade_date + holding_days]
            # We approximate by taking returns where trade_date >= start AND < start + holding_days
            # The industry_daily_returns table has one row per (date, industry) with that day's return
            rows = conn.execute(
                "SELECT industry, return FROM industry_daily_returns "
                "WHERE trade_date >= ? "
                "AND trade_date <= date(?, '+' || ? || ' days') "
                "ORDER BY trade_date",
                (trade_date, trade_date, holding_days + 10),  # +10 buffer for weekends/holidays
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return 0.0

        # Cumulate per industry
        industry_cum: dict[str, float] = {}
        for industry, daily_ret in rows:
            if industry not in industry_weights:
                continue
            if daily_ret is None:
                continue
            # Cumulative return: (1+r1)*(1+r2)*... - 1
            if industry not in industry_cum:
                industry_cum[industry] = 1.0
            industry_cum[industry] *= 1.0 + daily_ret

        # Weight by portfolio's industry exposure
        sector_contribution = 0.0
        for industry, weight in industry_weights.items():
            if industry in industry_cum:
                cum_ret = industry_cum[industry] - 1.0  # back to ratio
                sector_contribution += weight * cum_ret

        return sector_contribution

    def compute_industry_weights(self, picks: list[dict]) -> dict[str, float]:
        """Compute industry weights from a doctrine's stock picks.

        Args:
            picks: list of dicts with 'security_id' (and optionally 'weight')

        Returns: {industry: weight} summing to 1.0, read from security_master
        """
        if not picks:
            return {}

        conn = sqlite3.connect(self.cache_db)
        try:
            industry_mv: dict[str, float] = {}
            total_mv = 0.0
            for pick in picks:
                code = pick.get("security_id", "")
                bare = code.split(".")[0] if "." in code else code
                row = conn.execute(
                    "SELECT industry, total_mv FROM security_master WHERE code=?",
                    (bare,),
                ).fetchone()
                if not row or not row[0]:
                    continue
                industry, mv = row
                weight = float(mv) if mv and mv > 0 else 1.0
                industry_mv[industry] = industry_mv.get(industry, 0.0) + weight
                total_mv += weight

            if total_mv <= 0:
                return {}

            return {ind: mv / total_mv for ind, mv in industry_mv.items()}
        finally:
            conn.close()
