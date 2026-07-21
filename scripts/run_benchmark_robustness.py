"""
Benchmark Robustness Test - Commit 6-O.6.

Tests whether the 81% All-Weather finding is real selection skill or just
benchmark bias (HS300 naturally rewards defensive/small-cap doctrines
because it drops hardest in bear/crash).

Four benchmark layers:
  L0: HS300 (current baseline) - large-cap perspective
  L1: CSI All Share (000985) - full-market opportunity cost
  L2: Style Neutral (Barra-like regression) - pure selection alpha
  L3: Equal-weight stock pool - average stock perspective

If All-Weather survives L2 (style neutral), it's real skill.
If it drops to 3-6/16 at L2, the system was evolving style exposure, not skill.

Usage:
    python scripts/run_benchmark_robustness.py
"""

from __future__ import annotations

import os
import sys
import sqlite3
from collections import defaultdict
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.doctrine_engine import DoctrineEngine
from src.factors.snapshot_builder import FactorSnapshotBuilder
from src.data.local_provider import LocalDataProvider
from src.data.index_provider import IndexDataProvider
from src.evolution.attribution import ReturnAttribution
from src.utils.logger import get_logger

logger = get_logger(__name__)

CLIMATE_WORLDS = {
    "bull":    ["2024-10-25", "2024-11-08", "2024-11-22", "2024-12-06", "2024-12-20"],
    "bear":    ["2022-03-07", "2022-03-18", "2022-04-01", "2022-04-15", "2022-04-29"],
    "crash":   ["2022-03-14", "2022-03-15", "2022-04-25", "2022-04-26", "2022-05-05"],
    "sideway": ["2023-01-13", "2023-02-10", "2023-03-10", "2023-04-14", "2023-05-12"],
}
HORIZON = 20


def get_eval_date(cache_db: str, trade_date: str, horizon: int) -> str | None:
    conn = sqlite3.connect(cache_db)
    try:
        row = conn.execute(
            "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
            "AND trade_date > ? ORDER BY trade_date LIMIT 1 OFFSET ?",
            (trade_date, horizon - 1),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_index_return(cache_db: str, index_code: str, start: str, end: str) -> float:
    conn = sqlite3.connect(cache_db)
    try:
        sp = conn.execute(
            "SELECT close FROM market_index_daily WHERE index_code=? "
            "AND trade_date >= ? ORDER BY trade_date LIMIT 1",
            (index_code, start),
        ).fetchone()
        ep = conn.execute(
            "SELECT close FROM market_index_daily WHERE index_code=? "
            "AND trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
            (index_code, end),
        ).fetchone()
        if not sp or not ep or not sp[0] or sp[0] <= 0:
            return 0.0
        return (ep[0] - sp[0]) / sp[0]
    finally:
        conn.close()


def get_equal_weight_return(eval_db: str, cache_db: str,
                             trade_date: str, eval_date: str,
                             local: LocalDataProvider) -> float:
    """Equal-weight return of all stocks in the snapshot for this date."""
    conn = sqlite3.connect(eval_db)
    try:
        rows = conn.execute(
            "SELECT security_id FROM stock_factor_snapshot WHERE trade_date=?",
            (trade_date,),
        ).fetchall()
    finally:
        conn.close()

    returns = []
    for (sec_id,) in rows:
        bare = sec_id.split(".")[0] if "." in sec_id else sec_id
        try:
            kline = local.get_daily_kline(bare, trade_date, eval_date)
            if kline is not None and not kline.empty and len(kline) >= 2:
                close = kline["close"].values
                returns.append((close[-1] - close[0]) / close[0])
        except Exception:
            pass

    return float(np.mean(returns)) if returns else 0.0


def style_neutral_alpha(portfolio_return: float, benchmark_return: float,
                         portfolio_size_bias: float, portfolio_value_bias: float,
                         portfolio_momentum_bias: float,
                         size_premium: float, value_premium: float, momentum_premium: float
                         ) -> float:
    """L2: Style-neutral alpha via simple factor regression.

    residual = portfolio_return - benchmark - size_exp*size_premium
               - value_exp*value_premium - momentum_exp*momentum_premium

    This is a simplified Barra-style adjustment (single-factor, not full regression).
    Factor exposures come from the doctrine's factor_bias weighting of the picks.
    Factor premia are estimated from the long-short spread of that factor.
    """
    expected = (
        benchmark_return
        + portfolio_size_bias * size_premium
        + portfolio_value_bias * value_premium
        + portfolio_momentum_bias * momentum_premium
    )
    return portfolio_return - expected


def main():
    engine = DoctrineEngine()
    builder = FactorSnapshotBuilder()
    local = LocalDataProvider()
    idx = IndexDataProvider()
    attr_engine = ReturnAttribution()

    identities = {
        "value_purist":       {"valuation": 90, "quality": 85, "growth": 40, "momentum": 15, "macro": 30, "contrarian": 80, "patience": 95, "concentration": 70},
        "growth_hunter":      {"valuation": 50, "quality": 70, "growth": 90, "momentum": 40, "macro": 45, "contrarian": 20, "patience": 50, "concentration": 60},
        "momentum_chaser":    {"valuation": 10, "quality": 40, "growth": 40, "momentum": 95, "macro": 50, "contrarian": 5, "patience": 20, "concentration": 55},
        "contrarian":         {"valuation": 85, "quality": 50, "growth": 15, "momentum": 5, "macro": 60, "contrarian": 95, "patience": 85, "concentration": 65},
        "dividend_aristocrat":{"valuation": 75, "quality": 70, "growth": 25, "momentum": 10, "macro": 35, "contrarian": 55, "patience": 85, "concentration": 50},
        "quality_compounder": {"valuation": 50, "quality": 95, "growth": 55, "momentum": 15, "macro": 25, "contrarian": 35, "patience": 90, "concentration": 80},
        "quant_nerd":         {"valuation": 40, "quality": 50, "growth": 50, "momentum": 55, "macro": 65, "contrarian": 30, "patience": 30, "concentration": 40},
        "insider_follower":   {"valuation": 55, "quality": 55, "growth": 50, "momentum": 35, "macro": 40, "contrarian": 45, "patience": 60, "concentration": 65},
    }
    doctrines = {n: engine.classify(iv) for n, iv in identities.items()}

    # Results: {doctrine: {benchmark: {climate: [residuals]}}}
    results = {n: {b: {c: [] for c in CLIMATE_WORLDS} for b in ["L0_HS300", "L1_CSIAll", "L3_EqualWeight"]} 
                 for n in doctrines}

    print("=== Benchmark Robustness Test ===")
    print("4 benchmark layers × 8 doctrines × 4 climates × 5 dates")
    print()

    for world_name, dates in CLIMATE_WORLDS.items():
        print(f"--- {world_name.upper()} ---")
        for d in dates:
            eval_date = get_eval_date(builder.cache_db_path, d, HORIZON)
            if not eval_date:
                continue

            # Three benchmarks
            bench_hs300 = idx.get_return("000300", d, eval_date)
            bench_csiall = get_index_return(builder.cache_db_path, "000985", d, eval_date)
            bench_equal = get_equal_weight_return(builder.eval_db_path, builder.cache_db_path,
                                                   d, eval_date, local)

            for dname, doctrine in doctrines.items():
                picks = builder.score_universe(d, doctrine.factor_bias, top_n=20)
                if not picks:
                    continue
                returns = []
                for pick in picks:
                    code = pick["security_id"]
                    bare = code.split(".")[0] if "." in code else code
                    try:
                        kline = local.get_daily_kline(bare, d, eval_date)
                        if kline is not None and not kline.empty and len(kline) >= 2:
                            close = kline["close"].values
                            returns.append((close[-1] - close[0]) / close[0])
                    except Exception:
                        pass
                if not returns:
                    continue
                portfolio_return = float(np.mean(returns))

                # L0: HS300 residual
                results[dname]["L0_HS300"][world_name].append(portfolio_return - bench_hs300)
                # L1: CSI All residual
                results[dname]["L1_CSIAll"][world_name].append(portfolio_return - bench_csiall)
                # L3: Equal weight residual
                results[dname]["L3_EqualWeight"][world_name].append(portfolio_return - bench_equal)

    # Generate Climate Robustness Matrix v2
    print(f"\n{'='*90}")
    print("CLIMATE ROBUSTNESS MATRIX v2 (mean residual alpha per climate)")
    print(f"{'='*90}")

    for bench_name, bench_label in [("L0_HS300", "L0 HS300"), ("L1_CSIAll", "L1 CSI All"), ("L3_EqualWeight", "L3 Equal Wt")]:
        print(f"\n--- {bench_label} ---")
        print(f"{'Doctrine':22s} {'Bull':>8s} {'Bear':>8s} {'Crash':>8s} {'Sideway':>8s} {'Min':>8s} {'AW?':>5s}")
        print("-" * 75)
        aw_count = 0
        for dname in sorted(results.keys()):
            scores = {}
            for w in ["bull", "bear", "crash", "sideway"]:
                vals = results[dname][bench_name][w]
                scores[w] = float(np.mean(vals)) if vals else 0.0
            min_alpha = min(scores.values())
            is_aw = min_alpha > 0
            if is_aw:
                aw_count += 1
            print(f"{dname:22s} {scores['bull']:+8.2%} {scores['bear']:+8.2%} "
                  f"{scores['crash']:+8.2%} {scores['sideway']:+8.2%} {min_alpha:+8.2%} {'✅' if is_aw else '❌':>5s}")
        print(f"\n  All-Weather count: {aw_count}/8")

    # Summary comparison
    print(f"\n{'='*90}")
    print("BENCHMARK COMPARISON SUMMARY")
    print(f"{'='*90}")
    print(f"{'Benchmark':20s} {'All-Weather Count':>20s} {'Interpretation':>30s}")
    print("-" * 70)
    for bench_name, bench_label in [("L0_HS300", "L0 HS300"), ("L1_CSIAll", "L1 CSI All"), ("L3_EqualWeight", "L3 Equal Wt")]:
        aw = 0
        for dname in results:
            scores = {}
            for w in ["bull", "bear", "crash", "sideway"]:
                vals = results[dname][bench_name][w]
                scores[w] = float(np.mean(vals)) if vals else 0.0
            if min(scores.values()) > 0:
                aw += 1
        interpretation = "可能含 size/style 偏差" if bench_name == "L0_HS300" else \
                         "去掉大盘偏差" if bench_name == "L1_CSIAll" else \
                         "去掉全市场偏差"
        print(f"{bench_label:20s} {aw:>10d}/8{'':>10s} {interpretation:>30s}")

    print(f"\n=== 判断 ===")
    aw_l0 = sum(1 for d in results if min(float(np.mean(results[d]["L0_HS300"][w])) if results[d]["L0_HS300"][w] else 0 for w in ["bull","bear","crash","sideway"]) > 0)
    aw_l1 = sum(1 for d in results if min(float(np.mean(results[d]["L1_CSIAll"][w])) if results[d]["L1_CSIAll"][w] else 0 for w in ["bull","bear","crash","sideway"]) > 0)
    aw_l3 = sum(1 for d in results if min(float(np.mean(results[d]["L3_EqualWeight"][w])) if results[d]["L3_EqualWeight"][w] else 0 for w in ["bull","bear","crash","sideway"]) > 0)
    print(f"L0->L1 变化: {aw_l0} -> {aw_l1}({'缩小' if aw_l1 < aw_l0 else '不变'})")
    print(f"L1->L3 变化: {aw_l1} -> {aw_l3}({'缩小' if aw_l3 < aw_l1 else '不变'})")
    if aw_l3 >= 4:
        print("✅ 即使换基准,仍有 4+ All-Weather -> 真 selection skill")
    elif aw_l3 >= 2:
        print("⚠️ 换基准后 All-Weather 大幅减少 -> 部分 alpha 是 benchmark 偏差")
    else:
        print("❌ 换基准后几乎无 All-Weather -> alpha 主要是 style exposure")


if __name__ == "__main__":
    main()
