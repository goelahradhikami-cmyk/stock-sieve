"""
Factor Engine — Computes 50+ factors across 6 families.

Families (aligned with personality_genome_v3.2 §10):
  - value (8 sub-factors)
  - quality (6)
  - growth (5)
  - momentum (4)
  - risk (5)
  - sentiment (3)

Each factor outputs: raw_value, percentile (cross-sectional), z_score
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class FactorResult:
    """Single factor computation result."""

    name: str
    family: str
    raw_value: float | None = None
    percentile: float | None = None  # 0-1 cross-sectional rank
    z_score: float | None = None  # standardized
    weight: float = 0.0  # weight in composite score


@dataclass
class CompositeResult:
    """Composite factor scores for a single stock."""

    code: str
    date: str
    quality_score: float = 50.0
    value_score: float = 50.0
    growth_score: float = 50.0
    momentum_score: float = 50.0
    risk_score: float = 50.0
    sentiment_score: float = 50.0
    factors: list[FactorResult] = field(default_factory=list)


class FactorEngine:
    """Computes and normalizes factors across the stock universe."""

    # ── Factor definitions ───────────────────────────────

    FACTOR_FAMILIES = {
        "value": [
            {"name": "pe_ttm", "description": "PE (TTM)", "direction": "lower_better"},
            {"name": "pb", "description": "PB", "direction": "lower_better"},
            {"name": "ps", "description": "PS ratio", "direction": "lower_better"},
            {"name": "ev_ebitda", "description": "EV/EBITDA", "direction": "lower_better"},
            {"name": "fcf_yield", "description": "FCF Yield", "direction": "higher_better"},
            {
                "name": "dividend_yield",
                "description": "Dividend Yield",
                "direction": "higher_better",
            },
            {
                "name": "earnings_yield",
                "description": "Earnings Yield",
                "direction": "higher_better",
            },
            {"name": "peg_ratio", "description": "PEG Ratio", "direction": "lower_better"},
        ],
        "quality": [
            {"name": "roe", "description": "ROE", "direction": "higher_better"},
            {"name": "roe_5y_avg", "description": "ROE 5Y Average", "direction": "higher_better"},
            {"name": "roic", "description": "ROIC", "direction": "higher_better"},
            {"name": "gross_margin", "description": "Gross Margin", "direction": "higher_better"},
            {"name": "net_margin", "description": "Net Margin", "direction": "higher_better"},
            {
                "name": "accruals_ratio",
                "description": "Accruals Ratio",
                "direction": "lower_better",
            },
        ],
        "growth": [
            {
                "name": "revenue_growth_1y",
                "description": "Revenue Growth 1Y",
                "direction": "higher_better",
            },
            {
                "name": "revenue_growth_3y",
                "description": "Revenue Growth 3Y CAGR",
                "direction": "higher_better",
            },
            {
                "name": "earnings_growth_1y",
                "description": "Earnings Growth 1Y",
                "direction": "higher_better",
            },
            {
                "name": "earnings_growth_3y",
                "description": "Earnings Growth 3Y CAGR",
                "direction": "higher_better",
            },
            {
                "name": "margin_trend",
                "description": "Margin Trend 3Y",
                "direction": "higher_better",
            },
        ],
        "momentum": [
            {"name": "momentum_1m", "description": "1M Momentum", "direction": "higher_better"},
            {"name": "momentum_3m", "description": "3M Momentum", "direction": "higher_better"},
            {"name": "momentum_6m", "description": "6M Momentum", "direction": "higher_better"},
            {"name": "momentum_12m", "description": "12M Momentum", "direction": "higher_better"},
        ],
        "risk": [
            {"name": "volatility_1m", "description": "1M Volatility", "direction": "lower_better"},
            {
                "name": "max_drawdown_1y",
                "description": "1Y Max Drawdown",
                "direction": "lower_better",
            },
            {
                "name": "debt_to_equity",
                "description": "Debt-to-Equity",
                "direction": "lower_better",
            },
            {
                "name": "interest_coverage",
                "description": "Interest Coverage",
                "direction": "higher_better",
            },
            {"name": "beta", "description": "Beta", "direction": "lower_better"},
        ],
        "sentiment": [
            {
                "name": "holder_change_pct",
                "description": "Holder Count Change",
                "direction": "lower_better",
            },
            {"name": "rsi_14", "description": "RSI 14-day", "direction": "neutral"},
            {"name": "volume_ratio", "description": "Volume Ratio", "direction": "neutral"},
        ],
    }

    def __init__(self) -> None:
        self._all_factors = []
        for family, factors in self.FACTOR_FAMILIES.items():
            for f in factors:
                f["family"] = family
                self._all_factors.append(f)

    def compute_single_stock(
        self,
        code: str,
        financial_data: dict,
        price_data: pd.DataFrame,
        market_data: dict | None = None,
    ) -> CompositeResult:
        """Compute all factors for a single stock.

        Args:
            code: Stock code
            financial_data: dict with keys like 'roe', 'gross_margin', etc.
            price_data: DataFrame with columns: date, close, volume
            market_data: dict with market-wide data for beta/percentile calc

        Returns:
            CompositeResult with all factor values and composite scores.
        """
        result = CompositeResult(
            code=code,
            date=str(price_data.iloc[-1]["date"]) if not price_data.empty else "",
        )

        factors = []

        # ── Compute each factor ──────────────────────────
        for factor_def in self._all_factors:
            name = factor_def["name"]
            family = factor_def["family"]

            raw_value = self._compute_factor(name, financial_data, price_data, market_data)
            factors.append(
                FactorResult(
                    name=name,
                    family=family,
                    raw_value=raw_value,
                )
            )

        result.factors = factors

        # ── Compute composite scores per family ───────────

        def family_score(family: str, direction: str | None = None) -> float:
            """Average of factors in a family, normalized 0-100."""
            f_factors = [f for f in factors if f.family == family and f.raw_value is not None]
            if not f_factors:
                return 50.0

            scores = []
            for f in f_factors:
                fd = next((x for x in self._all_factors if x["name"] == f.name), None)
                if fd is None:
                    continue
                # Convert raw to 0-100 based on direction
                raw = f.raw_value
                if raw is None or pd.isna(raw):
                    continue
                if fd["direction"] == "higher_better":
                    scores.append(raw * 100 if abs(raw) <= 1 else min(100, max(0, raw)))
                elif fd["direction"] == "lower_better":
                    scores.append(
                        max(0, 100 - raw * 100) if abs(raw) <= 1 else min(100, max(0, 100 - raw))
                    )
                else:  # neutral
                    scores.append(50.0)

            return float(np.mean(scores)) if scores else 50.0

        result.quality_score = family_score("quality")
        result.value_score = family_score("value")
        result.growth_score = family_score("growth")
        result.momentum_score = family_score("momentum")
        result.risk_score = family_score("risk")
        result.sentiment_score = family_score("sentiment")

        return result

    def _compute_factor(
        self, name: str, financial: dict, prices: pd.DataFrame, market: dict | None = None
    ) -> float | None:
        """Compute a single factor value."""

        # ── Value factors ─────────────────────────────────
        if name == "pe_ttm":
            return financial.get("pe_ttm")
        if name == "pb":
            return financial.get("pb")
        if name == "ps":
            return financial.get("ps")
        if name == "ev_ebitda":
            return financial.get("ev_ebitda")
        if name == "fcf_yield":
            fcf = financial.get("fcf")
            mcap = financial.get("mcap")
            return fcf / mcap if fcf and mcap else None
        if name == "dividend_yield":
            return financial.get("dividend_yield")
        if name == "earnings_yield":
            pe = financial.get("pe_ttm")
            return 1.0 / pe if pe and pe > 0 else None
        if name == "peg_ratio":
            pe = financial.get("pe_ttm")
            eg = financial.get("earnings_growth_1y")
            return pe / (eg * 100) if pe and eg and eg > 0 else None

        # ── Quality factors ────────────────────────────────
        if name == "roe":
            return financial.get("roe")
        if name == "roe_5y_avg":
            return financial.get("roe_5y_avg")
        if name == "roic":
            return financial.get("roic")
        if name == "gross_margin":
            return financial.get("gross_margin")
        if name == "net_margin":
            return financial.get("net_margin")
        if name == "accruals_ratio":
            return financial.get("accruals_ratio")

        # ── Growth factors ─────────────────────────────────
        if name == "revenue_growth_1y":
            return financial.get("revenue_growth_1y")
        if name == "revenue_growth_3y":
            return financial.get("revenue_growth_3y")
        if name == "earnings_growth_1y":
            return financial.get("earnings_growth_1y")
        if name == "earnings_growth_3y":
            return financial.get("earnings_growth_3y")
        if name == "margin_trend":
            return financial.get("margin_trend")

        # ── Momentum factors ───────────────────────────────
        if prices.empty:
            return None

        def _mom(days: int) -> float | None:
            if len(prices) <= days:
                return None
            return float(prices["close"].iloc[-1] / prices["close"].iloc[-days - 1] - 1)

        if name == "momentum_1m":
            return _mom(21)
        if name == "momentum_3m":
            return _mom(63)
        if name == "momentum_6m":
            return _mom(126)
        if name == "momentum_12m":
            return _mom(252)

        # ── Risk factors ───────────────────────────────────
        if name == "volatility_1m":
            if len(prices) < 21:
                return None
            rets = prices["close"].pct_change().dropna().tail(21)
            return float(rets.std() * np.sqrt(252))

        if name == "max_drawdown_1y":
            if len(prices) < 252:
                return None
            close = prices["close"].tail(252)
            peak = close.expanding().max()
            dd = (close - peak) / peak
            return float(dd.min())

        if name == "debt_to_equity":
            return financial.get("debt_to_equity")

        if name == "interest_coverage":
            return financial.get("interest_coverage")

        if name == "beta":
            return financial.get("beta")

        # ── Sentiment factors ──────────────────────────────
        if name == "holder_change_pct":
            return financial.get("holder_change_pct")

        if name == "rsi_14":
            if len(prices) < 15:
                return None
            close = prices["close"]
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            return float(100 - 100 / (1 + rs.iloc[-1])) if pd.notna(rs.iloc[-1]) else None

        if name == "volume_ratio":
            if len(prices) < 6 or "volume" not in prices.columns:
                return None
            vol_vals = prices["volume"].values[-6:]
            vol5 = float(np.mean(vol_vals[:5]))
            vol_today = float(vol_vals[-1])
            return float(vol_today / vol5) if vol5 > 0 else None

        return None

    def compute_cross_sectional(
        self, stock_results: list[CompositeResult]
    ) -> list[CompositeResult]:
        """Normalize factors *and* composite family scores across the universe.

        For each factor this populates ``percentile`` (0-1 cross-sectional rank)
        and ``z_score``. The six family composite scores
        (``quality_score`` … ``sentiment_score``) are then **recomputed from the
        cross-sectional percentile** instead of the raw value — this is what makes
        the scores relative to the current universe (and comparable across
        families / regimes), rather than an absolute, scale-sensitive value.

        Args:
            stock_results: List of CompositeResult from compute_single_stock.

        Returns:
            Same list, with percentile, z_score, and family scores normalized.
        """
        if len(stock_results) < 2:
            # No cross-section to normalize against: leave the single-stock
            # (raw-value) fallback scores produced by compute_single_stock intact.
            return stock_results

        # Group factors by name
        factor_values: dict[str, list] = {}
        for r in stock_results:
            for f in r.factors:
                if f.raw_value is not None and not pd.isna(f.raw_value):
                    factor_values.setdefault(f.name, []).append(f.raw_value)

        # Compute percentile + z_score for each factor
        for r in stock_results:
            for f in r.factors:
                if f.name in factor_values and f.raw_value is not None:
                    values = np.array(factor_values[f.name])
                    f.percentile = float((values < f.raw_value).mean())
                    if values.std() > 0:
                        f.z_score = float((f.raw_value - values.mean()) / values.std())

        # Recompute family composite scores from cross-sectional percentile
        for r in stock_results:
            r.quality_score = self._family_score_from_percentile(r, "quality")
            r.value_score = self._family_score_from_percentile(r, "value")
            r.growth_score = self._family_score_from_percentile(r, "growth")
            r.momentum_score = self._family_score_from_percentile(r, "momentum")
            r.risk_score = self._family_score_from_percentile(r, "risk")
            r.sentiment_score = self._family_score_from_percentile(r, "sentiment")

        return stock_results

    def _family_score_from_percentile(self, result: CompositeResult, family: str) -> float:
        """Average cross-sectional score (0-100) of one family's factors.

        ``higher_better`` → percentile*100, ``lower_better`` → (1-percentile)*100,
        ``neutral`` → 50 (a neutral factor carries no directional edge, so it
        contributes a flat mid-score rather than a spurious rank).
        """
        scores = []
        for f in result.factors:
            if f.family != family or f.raw_value is None or pd.isna(f.raw_value):
                continue
            fd = next((x for x in self._all_factors if x["name"] == f.name), None)
            if fd is None:
                continue
            if f.percentile is None:
                # Not part of a cross-section (e.g. single-stock path) → flat.
                scores.append(50.0)
                continue
            p = f.percentile  # fraction of stocks with a strictly-lower raw value
            if fd["direction"] == "higher_better":
                scores.append(p * 100)
            elif fd["direction"] == "lower_better":
                scores.append((1 - p) * 100)
            else:
                scores.append(50.0)
        return float(np.mean(scores)) if scores else 50.0

    def compute_universe(self, stock_inputs: list[dict]) -> list[CompositeResult]:
        """Compute cross-sectionally-normalized factors for a whole universe.

        Convenience wrapper around ``compute_single_stock`` + ``compute_cross_sectional``
        so callers don't have to wire the two passes by hand.

        Args:
            stock_inputs: Iterable of dicts, each with keys
                ``code``, ``financial_data``, ``price_data``,
                and optional ``market_data`` — exactly the args of
                ``compute_single_stock``.

        Returns:
            List of CompositeResult with cross-sectional family scores populated.
            (A single-element input falls back to the raw-value scores, since no
            cross-section exists to normalize against.)
        """
        results = [
            self.compute_single_stock(
                code=inp["code"],
                financial_data=inp["financial_data"],
                price_data=inp["price_data"],
                market_data=inp.get("market_data"),
            )
            for inp in stock_inputs
        ]
        return self.compute_cross_sectional(results)

    def get_factor_names(self) -> list[str]:
        """Return list of all factor names."""
        return [f["name"] for f in self._all_factors]

    def get_family_names(self) -> list[str]:
        """Return list of factor family names."""
        return list(self.FACTOR_FAMILIES.keys())
