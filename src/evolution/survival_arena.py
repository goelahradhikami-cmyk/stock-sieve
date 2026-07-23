"""
Doctrine Survival Arena - Commit 6-L.7 Phase 3.2.

The core engine that ties together: regime resolution -> doctrine backtest ->
return attribution -> residual alpha -> fitness -> survival selection.

This is where "who made money" becomes "who has true skill in this market
ecology". Evolution pressure is applied on residual_alpha (not total_return),
with ecological niche protection to prevent winner-takes-all.

Usage:
    from src.evolution.survival_arena import DoctrineSurvivalArena
    arena = DoctrineSurvivalArena()
    arena.run_cycle(dates=['2026-04-15','2026-05-27'])
    arena.select_survivors()  # ecological niche protection
"""

from __future__ import annotations

import sqlite3

import numpy as np

from src.agents.doctrine_engine import DoctrineEngine, DoctrineGenome
from src.data.index_provider import IndexDataProvider
from src.data.local_provider import LocalDataProvider
from src.evolution.attribution import ReturnAttribution
from src.evolution.fitness import FitnessCalculator
from src.factors.snapshot_builder import FactorSnapshotBuilder
from src.market.regime_bootstrap import RegimeBootstrap
from src.utils.logger import get_logger

logger = get_logger(__name__)


class DoctrineSurvivalArena:
    """Runs multi-doctrine backtests with attribution + ecological selection.

    For each historical date:
      1. Resolve market regime (from market_regime_snapshots)
      2. Each doctrine scores the universe, picks Top N
      3. Compute forward returns (T+HORIZON K-line)
      4. Attribute: Total = Market Beta + Sector + Residual Alpha
      5. Record to doctrine_fitness_history
    Then:
      6. Compute per-doctrine fitness (FitnessCalculator)
      7. Select survivors with ecological niche protection
    """

    HORIZON = 20  # T+20 trading days
    TOP_N = 20  # each doctrine picks 20 stocks

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db
        self.engine = DoctrineEngine()
        self.builder = FactorSnapshotBuilder()
        self.local = LocalDataProvider()
        self.idx = IndexDataProvider()
        self.regime = RegimeBootstrap(eval_db=eval_db, cache_db=cache_db)
        self.attribution = ReturnAttribution(cache_db=cache_db)
        self.fitness_calc = FitnessCalculator(eval_db=eval_db)

    def run_cycle(self, dates: list[str], doctrines: list[DoctrineGenome] | None = None) -> dict:
        """Run one survival arena cycle over a set of historical dates.

        Args:
            dates: historical trade dates to backtest
            doctrines: doctrines to compete. If None, uses all 8 base identities.

        Returns: summary dict with per-doctrine stats.
        """
        if doctrines is None:
            doctrines = [self.engine.classify(iv) for iv in self._base_identities()]

        print("=== Doctrine Survival Arena ===")
        print(f"Dates: {len(dates)}, Doctrines: {len(doctrines)}, Horizon: T+{self.HORIZON}")
        print()

        for trade_date in dates:
            self._run_single_date(trade_date, doctrines)

        # After all dates: update regime statistics + compute fitness
        print("\n=== Computing fitness ===")
        for d in doctrines:
            n = self.fitness_calc.update_regime_statistics(d.doctrine_id)
            result = self.fitness_calc.calculate_doctrine_fitness(d.doctrine_id)
            if result:
                print(
                    f"  {d.doctrine_id:35s}: fitness={result.fitness:.3f} "
                    f"residual={result.residual_alpha_normalized:.2f} "
                    f"regime={result.regime_adaptation:.2f} "
                    f"(regime_stats={n})"
                )

        return self._summary(doctrines)

    def _run_single_date(self, trade_date: str, doctrines: list[DoctrineGenome]) -> None:
        """Run all doctrines for one date, record to fitness_history."""
        # 1. Resolve regime
        regime = self.regime.get_regime(trade_date) or "unknown"

        # 2. Find eval date (trade_date + HORIZON trading days)
        eval_date = self._eval_date(trade_date, self.HORIZON)
        if not eval_date:
            return

        # 3. Benchmark return
        bench_ret = self.idx.get_return("000300", trade_date, eval_date)

        print(f"--- {trade_date} -> {eval_date} (regime={regime}, 沪深300={bench_ret:+.2%}) ---")

        for doctrine in doctrines:
            self._backtest_doctrine(doctrine, trade_date, eval_date, regime, bench_ret)

    def _backtest_doctrine(
        self,
        doctrine: DoctrineGenome,
        trade_date: str,
        eval_date: str,
        regime: str,
        bench_ret: float,
    ) -> None:
        """Backtest one doctrine on one date, attribute, record.

        Commit 6-L.8: also records per-pick returns (for breadth) and
        computes alpha_quality = residual × stability × breadth × capacity.
        """
        # Score universe + pick Top N
        picks = self.builder.score_universe(trade_date, doctrine.factor_bias, top_n=self.TOP_N)
        if not picks:
            return

        # Forward returns for each pick (keep per-stock for breadth)
        pick_returns = []  # list of (security_id, return)
        for pick in picks:
            code = pick["security_id"]
            bare = code.split(".")[0] if "." in code else code
            try:
                kline = self.local.get_daily_kline(bare, trade_date, eval_date)
                if kline is not None and not kline.empty and len(kline) >= 2:
                    close = kline["close"].values
                    ret = (close[-1] - close[0]) / close[0]
                    pick_returns.append((code, float(ret)))
            except Exception as exc:
                logger.warning("operation failed (was silently ignored): %s", exc)

        if not pick_returns:
            return

        returns = [r for _, r in pick_returns]
        portfolio_return = float(np.mean(returns))
        # Drawdown: min cumulative return during holding
        cum = np.cumprod(1 + np.array(returns)) - 1
        drawdown = float(np.min(cum)) if len(cum) > 0 else 0.0

        # Industry weights for sector attribution
        industry_weights = self.attribution.compute_industry_weights(picks)

        # Attribute
        attr = self.attribution.attribute(
            portfolio_return=portfolio_return,
            benchmark_return=bench_ret,
            portfolio_industry_weights=industry_weights,
            trade_date=trade_date,
            holding_days=self.HORIZON,
        )

        # Commit 6-L.8: Alpha Quality = residual × stability × breadth × capacity
        from src.evolution.alpha_quality import AlphaQualityCalculator

        aq_calc = AlphaQualityCalculator()
        alpha_quality = aq_calc.calculate(
            residual_alpha=attr.residual_alpha,
            pick_returns=returns,
            n_picks=len(returns),
            doctrine_id=doctrine.doctrine_id,
        )

        # Commit 6-M: Alpha Origin Attribution
        # Decompose residual into factor_alpha + selection_alpha + timing + luck
        from src.evolution.alpha_origin import AlphaOriginAttribution

        ao = AlphaOriginAttribution(eval_db=self.eval_db, cache_db=self.cache_db)
        origin = ao.attribute(
            doctrine=doctrine,
            picks=picks,
            pick_returns=returns,
            trade_date=trade_date,
            residual_alpha=attr.residual_alpha,
            total_return=portfolio_return,
        )

        # Record to doctrine_fitness_history (now with 6-M origin fields)
        import json

        conn = sqlite3.connect(self.eval_db)
        try:
            conn.execute(
                """
                INSERT INTO doctrine_fitness_history
                (doctrine_id, trade_date, market_regime, total_return,
                 market_beta, sector_return, residual_alpha, drawdown,
                 pick_returns_json, alpha_quality,
                 factor_alpha, selection_alpha, factor_independence,
                 timing_quality, luck_penalty, origin_quality)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    doctrine.doctrine_id,
                    trade_date,
                    regime,
                    portfolio_return,
                    attr.market_beta,
                    attr.sector_return,
                    attr.residual_alpha,
                    drawdown,
                    json.dumps([round(r, 6) for r in returns]),
                    alpha_quality,
                    origin.factor_alpha,
                    origin.selection_alpha,
                    origin.factor_independence,
                    origin.timing_quality,
                    origin.luck_penalty,
                    origin.origin_quality,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        print(
            f"  {doctrine.doctrine_id:35s}: total={portfolio_return:+.2%} "
            f"beta={attr.market_beta:+.2%} sector={attr.sector_return:+.2%} "
            f"residual={attr.residual_alpha:+.2%} quality={alpha_quality:+.4f}"
        )

    def _eval_date(self, trade_date: str, horizon: int) -> str | None:
        """trade_date + HORIZON trading days, from trading_calendar."""
        conn = sqlite3.connect(self.cache_db)
        try:
            row = conn.execute(
                "SELECT trade_date FROM trading_calendar "
                "WHERE is_trading=1 AND trade_date > ? "
                "ORDER BY trade_date LIMIT 1 OFFSET ?",
                (trade_date, horizon - 1),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _summary(self, doctrines: list[DoctrineGenome]) -> dict:
        """Per-doctrine summary across all dates in this cycle."""
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            summary = {}
            for d in doctrines:
                rows = conn.execute(
                    "SELECT AVG(total_return) as avg_total, AVG(residual_alpha) as avg_resid, "
                    "AVG(market_beta) as avg_beta, AVG(sector_return) as avg_sector, "
                    "COUNT(*) as n FROM doctrine_fitness_history WHERE doctrine_id=?",
                    (d.doctrine_id,),
                ).fetchone()
                if rows and rows["n"] > 0:
                    summary[d.doctrine_id] = {
                        "avg_total": rows["avg_total"],
                        "avg_residual": rows["avg_resid"],
                        "avg_beta": rows["avg_beta"],
                        "avg_sector": rows["avg_sector"],
                        "n": rows["n"],
                    }
            return summary
        finally:
            conn.close()

    def _base_identities(self) -> list[dict]:
        """The 8 base personality identity vectors."""
        return [
            {
                "valuation": 90,
                "quality": 85,
                "growth": 40,
                "momentum": 15,
                "macro": 30,
                "contrarian": 80,
                "patience": 95,
                "concentration": 70,
            },
            {
                "valuation": 50,
                "quality": 70,
                "growth": 90,
                "momentum": 40,
                "macro": 45,
                "contrarian": 20,
                "patience": 50,
                "concentration": 60,
            },
            {
                "valuation": 10,
                "quality": 40,
                "growth": 40,
                "momentum": 95,
                "macro": 50,
                "contrarian": 5,
                "patience": 20,
                "concentration": 55,
            },
            {
                "valuation": 85,
                "quality": 50,
                "growth": 15,
                "momentum": 5,
                "macro": 60,
                "contrarian": 95,
                "patience": 85,
                "concentration": 65,
            },
            {
                "valuation": 75,
                "quality": 70,
                "growth": 25,
                "momentum": 10,
                "macro": 35,
                "contrarian": 55,
                "patience": 85,
                "concentration": 50,
            },
            {
                "valuation": 50,
                "quality": 95,
                "growth": 55,
                "momentum": 15,
                "macro": 25,
                "contrarian": 35,
                "patience": 90,
                "concentration": 80,
            },
            {
                "valuation": 40,
                "quality": 50,
                "growth": 50,
                "momentum": 55,
                "macro": 65,
                "contrarian": 30,
                "patience": 30,
                "concentration": 40,
            },
            {
                "valuation": 55,
                "quality": 55,
                "growth": 50,
                "momentum": 35,
                "macro": 40,
                "contrarian": 45,
                "patience": 60,
                "concentration": 65,
            },
        ]
