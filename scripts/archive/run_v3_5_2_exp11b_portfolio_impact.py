"""
v3.5.2 Exp11B: Extreme Filter Portfolio Impact Test - Commit 6-S.17.9.

Exp11A proved: Mid×Mid is falsified (small-sample artifact), but the
EXTREME PENALTY survives at N=194 (H×H = -6.87%, Gap Q5 = -6.26%).

The question is NO LONGER 'do extreme stocks lose money' (answered: yes).
The question is NOW: 'does filtering them out IMPROVE the portfolio?'

This is a fundamentally different test. Layer 2 is a RISK FILTER, not
an alpha selector. Success means:
  - Portfolio B (FRM + MEF) has BETTER risk-adjusted quality than A
  - Even if alpha is similar, lower drawdown/volatility = success
  - Must beat random filtering (Portfolio C) to prove MEF adds value

Three portfolios (per design review):
  Portfolio A: FRM direction only (baseline, all improving+stable)
  Portfolio B: FRM direction + Market Extremity Filter (MEF)
    MEF rejects: High FRM × High Gap (H×H), Gap Q5 (highest disagreement)
  Portfolio C: FRM direction + random filter (same N reduction as B)
    Bootstrap 1000x to establish random-filter distribution

Metrics (per episode, then aggregated):
  residual_alpha: mean alpha (B >= A is nice, but not required)
  worst_stock: min(stock_return_t20) - drawdown proxy (B < A = success)
  volatility: std(stock_return_t20) - B < A = success
  win_rate: positive_rate (B >= A = success)
  n_stocks: portfolio size (B < A due to filtering)

Success criteria (frozen):
  B worst_stock < A worst_stock (MEF reduces drawdown) AND
  B volatility <= A volatility (MEF does not increase vol) AND
  B alpha >= A alpha - 1pp (MEF does not destroy much alpha) AND
  B beats C (random filter) on at least 2 of 3 metrics

If PASS: v3.6 = Market State Intelligence Layer (MSIL) with MEF
If FAIL: MEF does not improve portfolio quality. FRM stays alone.

Usage:
    python scripts/run_v3_5_2_exp11b_portfolio_impact.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.expectation_gap import ExpectationGapEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"
REPORT_DIR = "data/reports"

N_BOOTSTRAP_RANDOM = 1000


def load_candidates():
    """Load all candidates with FRM score, gap, grouped by episode."""
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    rows = shadow.execute(
        """SELECT v.episode_id, v.trade_date, v.security_id, v.residual_alpha,
                  v.frm_score, v.frm_direction, v.rs_data_available,
                  v.stock_return_t20
           FROM shadow_candidates_v3 v
           WHERE v.residual_alpha IS NOT NULL
           ORDER BY v.episode_id, v.id"""
    ).fetchall()
    shadow.close()

    ege = ExpectationGapEngine(cache_db=CACHE_DB)
    enriched = []
    for r in rows:
        gap = None
        try:
            score = ege.compute(r["security_id"], r["trade_date"])
            gap = score.gap_score
        except Exception:
            pass
        enriched.append(
            {
                "episode_id": r["episode_id"],
                "trade_date": r["trade_date"],
                "sid": r["security_id"],
                "alpha": r["residual_alpha"],
                "frm": r["frm_score"],
                "frm_dir": r["frm_direction"],
                "rs_avail": r["rs_data_available"],
                "return_t20": r["stock_return_t20"],
                "gap": gap,
            }
        )

    # Compute quintiles for FRM and Gap (cross-sectional, all candidates)
    frm_vals = sorted([e["frm"] for e in enriched if e["frm"] is not None])
    gap_vals = sorted([e["gap"] for e in enriched if e["gap"] is not None])
    n = len(frm_vals)
    frm_q4_threshold = frm_vals[int(0.8 * n)]  # Q5 starts at 80th percentile
    gap_q4_threshold = gap_vals[int(0.8 * n)]

    for e in enriched:
        e["is_h_frm"] = e["frm"] is not None and e["frm"] >= frm_q4_threshold
        e["is_h_gap"] = e["gap"] is not None and e["gap"] >= gap_q4_threshold
        e["is_hh"] = e["is_h_frm"] and e["is_h_gap"]
        e["is_gap_q5"] = e["is_h_gap"]  # same as H gap

    return enriched, (frm_q4_threshold, gap_q4_threshold)


def _portfolio_metrics(stocks, label=""):
    """Compute portfolio metrics for a list of stocks."""
    if not stocks:
        return {"label": label, "n": 0}
    alphas = [s["alpha"] for s in stocks if s["alpha"] is not None]
    returns = [s["return_t20"] for s in stocks if s["return_t20"] is not None]
    return {
        "label": label,
        "n": len(stocks),
        "alpha_pct": float(np.mean(alphas) * 100) if alphas else None,
        "positive_rate_pct": float(100.0 * sum(1 for a in alphas if a > 0) / len(alphas))
        if alphas
        else None,
        "worst_stock_pct": float(min(returns) * 100) if returns else None,
        "volatility_pct": float(np.std(returns) * 100) if len(returns) > 1 else None,
        "median_alpha_pct": float(np.median(alphas) * 100) if alphas else None,
    }


def _print_portfolio(s, indent=4):
    pad = " " * indent
    if s["n"] == 0:
        print(f"{pad}{s['label']}: EMPTY", flush=True)
        return
    print(f"{pad}{s['label']}: N={s['n']}", flush=True)
    print(
        f"{pad}  alpha={s['alpha_pct']:+.2f}%  positive={s['positive_rate_pct']:.1f}%  "
        f"median={s['median_alpha_pct']:+.2f}%",
        flush=True,
    )
    print(
        f"{pad}  worst_stock={s['worst_stock_pct']:+.2f}%  vol={s['volatility_pct']:.2f}%",
        flush=True,
    )


def build_portfolios(enriched):
    """Build Portfolio A (FRM only), B (FRM+MEF), C (random filter)."""
    # Group by episode
    by_episode = defaultdict(list)
    for e in enriched:
        by_episode[e["episode_id"]].append(e)

    port_a_all = []  # FRM direction only (all stocks)
    port_b_all = []  # FRM + MEF (reject H×H and Gap Q5)
    port_c_meta = []  # per-episode: how many removed, for random filter

    for ep_id, stocks in by_episode.items():
        # Portfolio A: all stocks (FRM direction already applied - all are improving/stable)
        port_a_all.extend(stocks)

        # Portfolio B: reject H×H and Gap Q5
        port_b = [s for s in stocks if not s["is_hh"] and not s["is_gap_q5"]]
        port_b_all.extend(port_b)

        # Record how many were removed for random filter comparison
        n_removed = len(stocks) - len(port_b)
        port_c_meta.append({"episode_id": ep_id, "n_total": len(stocks), "n_removed": n_removed})

    return port_a_all, port_b_all, port_c_meta, by_episode


def run_random_filter_bootstrap(by_episode, port_c_meta, n_bootstrap=N_BOOTSTRAP_RANDOM):
    """Portfolio C: random filter, same N reduction as B, bootstrap."""
    rng = np.random.default_rng(42)
    bootstrap_results = []

    for _ in range(n_bootstrap):
        port_c_all = []
        for meta in port_c_meta:
            ep_id = meta["episode_id"]
            stocks = by_episode[ep_id]
            n_remove = meta["n_removed"]
            if n_remove >= len(stocks):
                port_c_all.extend(stocks)
            else:
                # Randomly remove same number as MEF
                indices = list(range(len(stocks)))
                removed = rng.choice(indices, size=n_remove, replace=False)
                kept = [s for i, s in enumerate(stocks) if i not in removed]
                port_c_all.extend(kept)
        s = _portfolio_metrics(port_c_all, "random")
        bootstrap_results.append(s)

    return bootstrap_results


def run_exp11b():
    enriched, (frm_q4, gap_q4) = load_candidates()
    print("=" * 70, flush=True)
    print("v3.5.2 Exp11B: Extreme Filter Portfolio Impact Test (6-S.17.9)", flush=True)
    print("Does Market Extremity Filter (MEF) improve portfolio quality?", flush=True)
    print("=" * 70, flush=True)
    print(f"\nTotal candidates: N={len(enriched)}", flush=True)
    print(f"FRM Q5 threshold (top 20%): {frm_q4:.1f}", flush=True)
    print(f"Gap Q5 threshold (top 20%): {gap_q4:.4f}", flush=True)

    port_a, port_b, port_c_meta, by_episode = build_portfolios(enriched)

    # Portfolio metrics
    print(f"\n{'=' * 70}", flush=True)
    print("PORTFOLIO COMPARISON", flush=True)
    print(f"{'=' * 70}", flush=True)

    s_a = _portfolio_metrics(port_a, "Portfolio A: FRM direction only (baseline)")
    s_b = _portfolio_metrics(port_b, "Portfolio B: FRM + MEF (reject H×H, Gap Q5)")

    print("\n  --- Portfolio A: FRM direction only ---", flush=True)
    _print_portfolio(s_a)
    print("\n  --- Portfolio B: FRM + Market Extremity Filter ---", flush=True)
    _print_portfolio(s_b)

    # How many removed
    n_removed = len(port_a) - len(port_b)
    print(
        f"\n  MEF removed: {n_removed} stocks ({100.0 * n_removed / len(port_a):.1f}% of pool)",
        flush=True,
    )

    # Random filter bootstrap
    print(f"\n  --- Portfolio C: Random filter (bootstrap {N_BOOTSTRAP_RANDOM}x) ---", flush=True)
    boot_results = run_random_filter_bootstrap(by_episode, port_c_meta)
    boot_alphas = [r["alpha_pct"] for r in boot_results if r["alpha_pct"] is not None]
    boot_worsts = [r["worst_stock_pct"] for r in boot_results if r["worst_stock_pct"] is not None]
    boot_vols = [r["volatility_pct"] for r in boot_results if r["volatility_pct"] is not None]

    print("  Random filter distribution:", flush=True)
    print(
        f"    alpha:      mean={np.mean(boot_alphas):+.2f}%  std={np.std(boot_alphas):.2f}%  "
        f"[{np.percentile(boot_alphas, 5):+.2f}%, {np.percentile(boot_alphas, 95):+.2f}%]",
        flush=True,
    )
    print(
        f"    worst_stock: mean={np.mean(boot_worsts):+.2f}%  std={np.std(boot_worsts):.2f}%  "
        f"[{np.percentile(boot_worsts, 5):+.2f}%, {np.percentile(boot_worsts, 95):+.2f}%]",
        flush=True,
    )
    print(
        f"    volatility:  mean={np.mean(boot_vols):.2f}%  std={np.std(boot_vols):.2f}%  "
        f"[{np.percentile(boot_vols, 5):.2f}%, {np.percentile(boot_vols, 95):.2f}%]",
        flush=True,
    )

    # Per-episode comparison
    print(f"\n{'=' * 70}", flush=True)
    print("PER-EPISODE COMPARISON (A vs B)", flush=True)
    print(f"{'=' * 70}", flush=True)
    print(
        f"  {'episode':15s} {'A_n':>4} {'B_n':>4} {'A_alpha':>8} {'B_alpha':>8} "
        f"{'A_worst':>8} {'B_worst':>8}",
        flush=True,
    )
    for ep_id in sorted(by_episode.keys()):
        stocks = by_episode[ep_id]
        s_a_ep = _portfolio_metrics(stocks)
        s_b_ep = _portfolio_metrics([s for s in stocks if not s["is_hh"] and not s["is_gap_q5"]])
        print(
            f"  {ep_id:15s} {s_a_ep['n']:4d} {s_b_ep['n']:4d} "
            f"{s_a_ep['alpha_pct']:+7.2f}% {s_b_ep['alpha_pct']:+7.2f}% "
            f"{s_a_ep['worst_stock_pct']:+7.2f}% {s_b_ep['worst_stock_pct']:+7.2f}%",
            flush=True,
        )

    # ─── Verdict ───
    print(f"\n{'=' * 70}", flush=True)
    print("VERDICT SYNTHESIS", flush=True)
    print(f"{'=' * 70}", flush=True)

    checks = {}
    # Check 1: B worst_stock < A worst_stock (MEF reduces drawdown)
    checks["mef_reduces_worst"] = s_b["worst_stock_pct"] > s_a["worst_stock_pct"]
    # Note: worst_stock is negative, so "greater" = less negative = better

    # Check 2: B volatility <= A volatility
    checks["mef_reduces_vol"] = s_b["volatility_pct"] <= s_a["volatility_pct"]

    # Check 3: B alpha >= A alpha - 1pp (MEF does not destroy much alpha)
    checks["mef_preserves_alpha"] = s_b["alpha_pct"] >= s_a["alpha_pct"] - 1.0

    # Check 4: B beats random on at least 2 of 3 metrics
    b_beats_random_alpha = s_b["alpha_pct"] > np.percentile(boot_alphas, 50)
    b_beats_random_worst = s_b["worst_stock_pct"] > np.percentile(boot_worsts, 50)
    b_beats_random_vol = s_b["volatility_pct"] < np.percentile(boot_vols, 50)
    n_beats = sum([b_beats_random_alpha, b_beats_random_worst, b_beats_random_vol])
    checks["mef_beats_random_2of3"] = n_beats >= 2

    print("\n  Validation checks:", flush=True)
    print(
        f"    MEF reduces worst stock:    {'PASS' if checks['mef_reduces_worst'] else 'FAIL'} "
        f"(A={s_a['worst_stock_pct']:+.2f}%, B={s_b['worst_stock_pct']:+.2f}%)",
        flush=True,
    )
    print(
        f"    MEF reduces volatility:     {'PASS' if checks['mef_reduces_vol'] else 'FAIL'} "
        f"(A={s_a['volatility_pct']:.2f}%, B={s_b['volatility_pct']:.2f}%)",
        flush=True,
    )
    print(
        f"    MEF preserves alpha (<1pp): {'PASS' if checks['mef_preserves_alpha'] else 'FAIL'} "
        f"(A={s_a['alpha_pct']:+.2f}%, B={s_b['alpha_pct']:+.2f}%)",
        flush=True,
    )
    print(
        f"    MEF beats random 2/3:       {'PASS' if checks['mef_beats_random_2of3'] else 'FAIL'} "
        f"(beats {n_beats}/3 random median)",
        flush=True,
    )

    all_pass = all(checks.values())
    n_pass = sum(checks.values())

    if all_pass:
        verdict = "ALL CHECKS PASS - MEF improves portfolio quality"
        recommendation = (
            "v3.6 = Market State Intelligence Layer (MSIL) with MEF. "
            "FRM direction gate + Market Extremity Filter. Risk filter validated."
        )
    elif n_pass >= 3:
        verdict = f"PARTIAL ({n_pass}/4 pass) - MEF shows promise"
        recommendation = "Analyze which checks failed. May need MEF threshold tuning."
    else:
        verdict = f"FAIL ({n_pass}/4 pass) - MEF does NOT improve portfolio"
        recommendation = "MEF does not add value beyond random filtering. FRM stays alone."

    print(f"\n  >>> VERDICT: {verdict}", flush=True)
    print(f"  >>> RECOMMENDATION: {recommendation}", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.17.9",
        "experiment": "exp11b_portfolio_impact",
        "date": str(date.today()),
        "thresholds": {"frm_q5": frm_q4, "gap_q5": gap_q4},
        "portfolio_a": s_a,
        "portfolio_b": s_b,
        "n_removed": n_removed,
        "random_bootstrap": {
            "alpha_mean": float(np.mean(boot_alphas)),
            "alpha_p5": float(np.percentile(boot_alphas, 5)),
            "alpha_p95": float(np.percentile(boot_alphas, 95)),
            "worst_mean": float(np.mean(boot_worsts)),
            "vol_mean": float(np.mean(boot_vols)),
        },
        "checks": checks,
        "n_pass": n_pass,
        "verdict": verdict,
        "recommendation": recommendation,
    }
    report_path = os.path.join(REPORT_DIR, f"v3_5_2_exp11b_portfolio_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_exp11b()
