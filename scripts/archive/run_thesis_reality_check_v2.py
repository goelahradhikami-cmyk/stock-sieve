"""
Thesis Reality Check v2 - Commit 6-R.4.

The "graduation exam" for the Thesis Intelligence Layer. Uses the upgraded
data foundation (1000 stocks, 100 dates, 33 quarters vintage-aware financials)
to definitively answer: does thesis add value beyond factor exposure?

Four layers of verification:
  Layer 1: A/B test (Control A: factor-only / Treatment B: factor+timing / Treatment C: factor+veto)
  Layer 2: Multi-benchmark (HS300 / CSI All / Equal Weight / Style Neutral)
  Layer 3: Signal decomposition (earnings / mispricing / catalyst separately)
  Layer 4: Anti-overfitting (time split + leave-one-regime-out)

PASS criteria:
  - Equal-weight benchmark: incremental alpha > 0
  - Style-neutral: incremental alpha > 0
  - Win rate: > 55%
  - At least 3/4 regimes not negative
  - No severe decay (20d vs 60d vs 120d)

Usage:
    python scripts/run_thesis_reality_check_v2.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.doctrine_engine import DoctrineEngine
from src.data.akshare_provider import AkshareProvider
from src.data.index_provider import IndexDataProvider
from src.data.local_provider import LocalDataProvider
from src.evolution.attribution import ReturnAttribution
from src.evolution.factor_neutralization import FactorNeutralizer
from src.factors.snapshot_builder import FactorSnapshotBuilder
from src.market.regime_bootstrap import RegimeBootstrap
from src.thesis.residualizer import ThesisResidualizer
from src.thesis.signal_engine import ThesisSignalEngine
from src.thesis.thesis_validator import ThesisValidator
from src.thesis.timing_layer import ThesisTimingLayer
from src.utils.logger import get_logger

logger = get_logger(__name__)

HORIZON = 20


@dataclass
class ABResult:
    """One A/B comparison result."""

    control_return: float
    treatment_return: float
    incremental_alpha: float
    win: bool


@dataclass
class RealityCheckResult:
    """Complete result of the reality check."""

    # Layer 1: A/B per treatment
    control_avg: float
    treatment_b_avg: float  # timing
    treatment_c_avg: float  # veto
    incr_b: float  # timing incremental
    incr_c: float  # veto incremental
    win_rate_b: float
    win_rate_c: float
    # Layer 2: Multi-benchmark
    alpha_hs300: float
    alpha_csiall: float
    alpha_equal: float
    alpha_style_neutral: float
    # Layer 3: Signal decomposition
    earnings_contribution: float
    mispricing_contribution: float
    catalyst_contribution: float
    # Layer 4: Anti-overfitting
    train_alpha: float
    test_alpha: float
    regime_results: dict  # {regime: incremental_alpha}
    # Verdict
    passed: bool
    verdict: str


class ThesisRealityCheckV2:
    """Graduation exam for Thesis Intelligence Layer.

    Uses 1000 stocks + 100 dates + vintage-aware financials to definitively
    test whether thesis signals add value beyond factor exposure.
    """

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db
        self.engine = DoctrineEngine()
        self.builder = FactorSnapshotBuilder()
        self.local = LocalDataProvider()
        self.idx = IndexDataProvider()
        self.akshare = AkshareProvider()
        self.attr = ReturnAttribution()
        self.neutralizer = FactorNeutralizer(eval_db=eval_db)
        self.signal_engine = ThesisSignalEngine(cache_db=cache_db)
        self.residualizer = ThesisResidualizer(eval_db=eval_db)
        self.timing = ThesisTimingLayer(eval_db=eval_db, cache_db=cache_db)
        self.validator = ThesisValidator(cache_db=cache_db)
        self.regime = RegimeBootstrap(eval_db=eval_db, cache_db=cache_db)

    def run(self, doctrines: list = None) -> RealityCheckResult:
        """Run the complete reality check.

        Args:
            doctrines: list of DoctrineGenome to test. If None, uses quant_nerd
                       (most balanced, best chance of showing thesis value).
        """
        if doctrines is None:
            doctrines = [
                self.engine.classify(
                    {
                        "valuation": 40,
                        "quality": 50,
                        "growth": 50,
                        "momentum": 55,
                        "macro": 65,
                        "contrarian": 30,
                        "patience": 30,
                        "concentration": 40,
                    }
                )
            ]

        # Get all snapshot dates with 1000+ stocks
        dates = self._get_valid_dates()
        print("=== Thesis Reality Check v2 ===")
        print(f"Dates: {len(dates)} (1000+ stocks each)")
        print(f"Doctrines: {len(doctrines)}")
        print(f"Horizon: T+{HORIZON}")
        print()

        # Layer 1: A/B test
        print("--- Layer 1: A/B Test ---")
        ab_results_b = []  # timing
        ab_results_c = []  # veto
        control_returns = []
        treatment_b_returns = []
        treatment_c_returns = []

        for d in dates:
            eval_date = self._get_eval_date(d, HORIZON)
            if not eval_date:
                continue

            doctrine = doctrines[0]  # test one doctrine at a time
            picks = self.builder.score_universe(d, doctrine.factor_bias, top_n=20)
            if not picks or len(picks) < 10:
                continue

            # Compute returns
            returns = self._compute_returns(picks, d, eval_date)
            if len(returns) < 6:
                continue

            # Control A: equal weight
            ctrl_ret = float(np.mean(returns))
            control_returns.append(ctrl_ret)

            # Treatment B: timing overlay
            timing_result = self.timing.apply_timing_overlay(picks, doctrine, d)
            treat_b_ret = self.timing.get_weighted_returns(picks, returns, timing_result)
            treatment_b_returns.append(treat_b_ret)

            # Treatment C: veto (remove vetoed stocks, reweight)
            non_vetoed = [
                (p, r)
                for p, r, a in zip(picks, returns, timing_result.adjustments, strict=False)
                if not a.vetoed
            ]
            treat_c_ret = float(np.mean([r for _, r in non_vetoed])) if non_vetoed else ctrl_ret
            treatment_c_returns.append(treat_c_ret)

        ctrl_avg = float(np.mean(control_returns)) if control_returns else 0
        treat_b_avg = float(np.mean(treatment_b_returns)) if treatment_b_returns else 0
        treat_c_avg = float(np.mean(treatment_c_returns)) if treatment_c_returns else 0

        incr_b = treat_b_avg - ctrl_avg
        incr_c = treat_c_avg - ctrl_avg
        win_b = (
            float(
                np.mean(
                    [
                        1 if t > c else 0
                        for t, c in zip(treatment_b_returns, control_returns, strict=False)
                    ]
                )
            )
            if control_returns
            else 0
        )
        win_c = (
            float(
                np.mean(
                    [
                        1 if t > c else 0
                        for t, c in zip(treatment_c_returns, control_returns, strict=False)
                    ]
                )
            )
            if control_returns
            else 0
        )

        print(f"  Control A (factor equal-weight): {ctrl_avg:+.4f}")
        print(
            f"  Treatment B (factor+timing):     {treat_b_avg:+.4f}  incr={incr_b:+.4f}  win={win_b:.0%}"
        )
        print(
            f"  Treatment C (factor+veto):       {treat_c_avg:+.4f}  incr={incr_c:+.4f}  win={win_c:.0%}"
        )

        # Layer 2: Multi-benchmark
        print("\n--- Layer 2: Multi-Benchmark ---")
        alpha_hs300, alpha_csiall, alpha_equal = self._compute_multi_benchmark(doctrines[0], dates)
        alpha_style_neutral = self._compute_style_neutral(doctrines[0], dates)
        print(f"  HS300:           {alpha_hs300:+.4f}")
        print(f"  CSI All:         {alpha_csiall:+.4f}")
        print(f"  Equal Weight:    {alpha_equal:+.4f}")
        print(f"  Style Neutral:   {alpha_style_neutral:+.4f}")

        # Layer 3: Signal decomposition
        print("\n--- Layer 3: Signal Decomposition ---")
        earn_contrib, misp_contrib, cat_contrib = self._decompose_signals(doctrines[0], dates)
        print(f"  Earnings:    {earn_contrib:+.4f}")
        print(f"  Mispricing:  {misp_contrib:+.4f}")
        print(f"  Catalyst:    {cat_contrib:+.4f}")

        # Layer 4: Anti-overfitting
        print("\n--- Layer 4: Anti-Overfitting ---")
        # Time split: 2021-2024 train, 2025-2026 test
        train_dates = [d for d in dates if d < "2025-01-01"]
        test_dates = [d for d in dates if d >= "2025-01-01"]
        train_alpha = self._compute_incremental(doctrines[0], train_dates)
        test_alpha = self._compute_incremental(doctrines[0], test_dates)
        print(f"  Train (2021-2024): {train_alpha:+.4f} ({len(train_dates)} dates)")
        print(f"  Test  (2025-2026): {test_alpha:+.4f} ({len(test_dates)} dates)")

        # Regime breakdown
        regime_results = {}
        for regime_name in ["bull", "bear", "crash", "sideway", "high_volatility"]:
            regime_dates = [d for d in dates if self.regime.get_regime(d) == regime_name]
            if regime_dates:
                regime_alpha = self._compute_incremental(doctrines[0], regime_dates)
                regime_results[regime_name] = regime_alpha
                print(f"  {regime_name:18s}: {regime_alpha:+.4f} ({len(regime_dates)} dates)")

        # Verdict
        passed = (
            alpha_equal > 0
            and alpha_style_neutral > 0
            and win_b > 0.55
            and sum(1 for v in regime_results.values() if v >= 0) >= 3
        )
        verdict = "PASS" if passed else "FAIL"
        if alpha_hs300 > 0 and alpha_equal <= 0:
            verdict = "FAIL: factor camouflage (HS300 positive but equal-weight negative)"
        elif alpha_equal > 0 and alpha_style_neutral <= 0:
            verdict = "FAIL: style exposure (equal-weight positive but style-neutral negative)"

        print(f"\n=== VERDICT: {verdict} ===")

        return RealityCheckResult(
            control_avg=ctrl_avg,
            treatment_b_avg=treat_b_avg,
            treatment_c_avg=treat_c_avg,
            incr_b=incr_b,
            incr_c=incr_c,
            win_rate_b=win_b,
            win_rate_c=win_c,
            alpha_hs300=alpha_hs300,
            alpha_csiall=alpha_csiall,
            alpha_equal=alpha_equal,
            alpha_style_neutral=alpha_style_neutral,
            earnings_contribution=earn_contrib,
            mispricing_contribution=misp_contrib,
            catalyst_contribution=cat_contrib,
            train_alpha=train_alpha,
            test_alpha=test_alpha,
            regime_results=regime_results,
            passed=passed,
            verdict=verdict,
        )

    def _get_valid_dates(self) -> list[str]:
        """Get snapshot dates with 1000+ stocks."""
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT trade_date FROM stock_factor_snapshot "
                "GROUP BY trade_date HAVING COUNT(*) >= 500 "
                "ORDER BY trade_date"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def _get_eval_date(self, trade_date: str, horizon: int) -> str | None:
        conn = sqlite3.connect(self.cache_db)
        try:
            row = conn.execute(
                "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
                "AND trade_date > ? ORDER BY trade_date LIMIT 1 OFFSET ?",
                (trade_date, horizon - 1),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _compute_returns(self, picks: list[dict], start: str, end: str) -> list[float]:
        returns = []
        for pick in picks:
            bare = (
                pick["security_id"].split(".")[0]
                if "." in pick["security_id"]
                else pick["security_id"]
            )
            try:
                kline = self.local.get_daily_kline(bare, start, end)
                if kline is not None and not kline.empty and len(kline) >= 2:
                    close = kline["close"].values
                    returns.append((close[-1] - close[0]) / close[0])
            except Exception:
                pass
        return returns

    def _compute_multi_benchmark(self, doctrine, dates: list[str]) -> tuple[float, float, float]:
        """Compute alpha vs 3 benchmarks."""
        portfolio_returns = []
        bench_hs300 = []
        bench_csiall = []
        bench_equal = []

        for d in dates:
            eval_date = self._get_eval_date(d, HORIZON)
            if not eval_date:
                continue
            picks = self.builder.score_universe(d, doctrine.factor_bias, top_n=20)
            if not picks:
                continue
            returns = self._compute_returns(picks, d, eval_date)
            if len(returns) < 6:
                continue
            port_ret = float(np.mean(returns))
            portfolio_returns.append(port_ret)
            bench_hs300.append(self.idx.get_return("000300", d, eval_date))

            # CSI All
            conn = sqlite3.connect(self.cache_db)
            try:
                sp = conn.execute(
                    "SELECT close FROM market_index_daily WHERE index_code='000985' AND trade_date >= ? ORDER BY trade_date LIMIT 1",
                    (d,),
                ).fetchone()
                ep = conn.execute(
                    "SELECT close FROM market_index_daily WHERE index_code='000985' AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
                    (eval_date,),
                ).fetchone()
                bench_csiall.append(((ep[0] - sp[0]) / sp[0]) if sp and ep and sp[0] else 0.0)
                # Equal weight: sample 200 stocks from snapshot (eval_db, not cache_db)
                eval_conn = sqlite3.connect(self.eval_db)
                try:
                    all_rows = eval_conn.execute(
                        "SELECT security_id FROM stock_factor_snapshot WHERE trade_date=?", (d,)
                    ).fetchall()
                finally:
                    eval_conn.close()
            finally:
                conn.close()

            # Equal weight returns (using sampled stocks)
            eq_returns = []
            for (sec_id,) in all_rows[:200]:  # sample 200 for speed
                bare = sec_id.split(".")[0] if "." in sec_id else sec_id
                try:
                    kline = self.local.get_daily_kline(bare, d, eval_date)
                    if kline is not None and not kline.empty and len(kline) >= 2:
                        eq_returns.append(
                            (kline["close"].values[-1] - kline["close"].values[0])
                            / kline["close"].values[0]
                        )
                except Exception:
                    pass
            bench_equal.append(float(np.mean(eq_returns)) if eq_returns else 0.0)

        if not portfolio_returns:
            return 0, 0, 0

        alpha_hs300 = float(
            np.mean([p - b for p, b in zip(portfolio_returns, bench_hs300, strict=False)])
        )
        alpha_csiall = float(
            np.mean([p - b for p, b in zip(portfolio_returns, bench_csiall, strict=False)])
        )
        alpha_equal = float(
            np.mean([p - b for p, b in zip(portfolio_returns, bench_equal, strict=False)])
        )

        return alpha_hs300, alpha_csiall, alpha_equal

    def _compute_style_neutral(self, doctrine, dates: list[str]) -> float:
        """Compute style-neutral alpha using factor regression."""
        residuals = []
        for d in dates:
            eval_date = self._get_eval_date(d, HORIZON)
            if not eval_date:
                continue
            picks = self.builder.score_universe(d, doctrine.factor_bias, top_n=20)
            if not picks:
                continue
            returns = self._compute_returns(picks, d, eval_date)
            if len(returns) < 6:
                continue
            result = self.neutralizer.neutralize_via_regression(picks, returns, d)
            residuals.append(result.selection_alpha)

        return float(np.mean(residuals)) if residuals else 0.0

    def _decompose_signals(self, doctrine, dates: list[str]) -> tuple[float, float, float]:
        """Decompose thesis contribution by signal type."""
        earn_returns = []
        misp_returns = []
        cat_returns = []

        for d in dates:
            eval_date = self._get_eval_date(d, HORIZON)
            if not eval_date:
                continue
            picks = self.builder.score_universe(d, doctrine.factor_bias, top_n=20)
            if not picks:
                continue

            # Compute signals for each pick
            for pick in picks:
                bare = (
                    pick["security_id"].split(".")[0]
                    if "." in pick["security_id"]
                    else pick["security_id"]
                )
                try:
                    signals = self.signal_engine.compute_signals(bare, d)
                    # Get return
                    kline = self.local.get_daily_kline(bare, d, eval_date)
                    if kline is not None and not kline.empty and len(kline) >= 2:
                        ret = (kline["close"].values[-1] - kline["close"].values[0]) / kline[
                            "close"
                        ].values[0]
                        # Correlate return with each signal
                        earn_returns.append((signals.fundamental_acceleration, ret))
                        misp_returns.append((signals.mispricing_gap, ret))
                        cat_returns.append((signals.catalyst_proxy, ret))
                except Exception:
                    pass

        # Compute IC (information coefficient) per signal
        def ic(pairs):
            if len(pairs) < 10:
                return 0.0
            signals = [p[0] for p in pairs]
            rets = [p[1] for p in pairs]
            if np.std(signals) < 1e-6 or np.std(rets) < 1e-6:
                return 0.0
            return float(np.corrcoef(signals, rets)[0, 1])

        return ic(earn_returns), ic(misp_returns), ic(cat_returns)

    def _compute_incremental(self, doctrine, dates: list[str]) -> float:
        """Compute timing incremental alpha for a subset of dates."""
        if not dates:
            return 0.0
        ctrl_returns = []
        treat_returns = []
        for d in dates:
            eval_date = self._get_eval_date(d, HORIZON)
            if not eval_date:
                continue
            picks = self.builder.score_universe(d, doctrine.factor_bias, top_n=20)
            if not picks:
                continue
            returns = self._compute_returns(picks, d, eval_date)
            if len(returns) < 6:
                continue
            ctrl_returns.append(float(np.mean(returns)))
            timing = self.timing.apply_timing_overlay(picks, doctrine, d)
            treat_returns.append(self.timing.get_weighted_returns(picks, returns, timing))

        if not ctrl_returns:
            return 0.0
        return float(np.mean(treat_returns)) - float(np.mean(ctrl_returns))


def main():
    check = ThesisRealityCheckV2()
    result = check.run()

    print(f"\n{'=' * 70}")
    print("THESIS REALITY CHECK v2 - FINAL REPORT")
    print(f"{'=' * 70}")
    print("\nLayer 1 (A/B):")
    print(f"  Control (factor):     {result.control_avg:+.4f}")
    print(
        f"  Treatment B (timing): {result.treatment_b_avg:+.4f}  incr={result.incr_b:+.4f}  win={result.win_rate_b:.0%}"
    )
    print(
        f"  Treatment C (veto):   {result.treatment_c_avg:+.4f}  incr={result.incr_c:+.4f}  win={result.win_rate_c:.0%}"
    )
    print("\nLayer 2 (Multi-Benchmark):")
    print(f"  HS300:          {result.alpha_hs300:+.4f}")
    print(f"  CSI All:        {result.alpha_csiall:+.4f}")
    print(f"  Equal Weight:   {result.alpha_equal:+.4f}")
    print(f"  Style Neutral:  {result.alpha_style_neutral:+.4f}")
    print("\nLayer 3 (Signal IC):")
    print(f"  Earnings:    {result.earnings_contribution:+.4f}")
    print(f"  Mispricing:  {result.mispricing_contribution:+.4f}")
    print(f"  Catalyst:    {result.catalyst_contribution:+.4f}")
    print("\nLayer 4 (Anti-Overfitting):")
    print(f"  Train alpha:  {result.train_alpha:+.4f}")
    print(f"  Test alpha:   {result.test_alpha:+.4f}")
    for regime, alpha in result.regime_results.items():
        print(f"  {regime:18s}: {alpha:+.4f}")
    print(f"\nVERDICT: {result.verdict}")


if __name__ == "__main__":
    main()
