"""
v4.0 Phase 0 Guardian Autopsy - Commit 6-S.19.2.

THE INVESTMENT COMMITTEE DIAGNOSTIC TOOL. Not an optimization script.
Decomposes Guardian's 1204 episodes to answer:
  1. WHEN does Guardian work/fail? (conditional reliability, not causal)
  2. Is confidence calibrated to actual outcomes?
  3. What are the failure modes? (when NOT to trust Guardian)
  4. Is Guardian alpha real or just beta rebound? (attribution sanity check)

Priority order (frozen, per design review):
  P0: Exp0C Failure Taxonomy (know when NOT to bet first)
  P1: Exp0B Confidence Calibration
  P2: Exp0A Regime Map (unsupervised discovery, not predefined bins)
  P3: Exp0D Beta Attribution (most dangerous alternative explanation)

Anti-overfitting discipline (6-S.19.1):
  - No causal claims ('works because X'). Only conditional ('works WHEN X').
  - No story before statistics. Statistics first.
  - Unsupervised regime discovery (no arbitrary bins).
  - Calibration curve (not just 'confidence band has positive return').

Data sources:
  - shadow_episode (1204 episodes, market features at signal time)
  - shadow_outcome (returns, alpha_vs_hs300)
  - shadow_candidates_v3 (194 events with residual_alpha, for cross-check)

Usage:
    python scripts/run_guardian_autopsy.py
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

from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
REPORT_DIR = "data/reports"


def load_episodes():
    """Load all episodes with features and outcomes."""
    con = sqlite3.connect(SHADOW_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT e.episode_id, e.trade_date, e.market_state, e.confidence,
                  e.confidence_band, e.decision, e.position_target,
                  e.vol_20d, e.vol_change, e.trend_ma60, e.breadth,
                  e.recovery_prob, e.reason_codes,
                  o.portfolio_return_t20, o.market_return_t20,
                  o.alpha_vs_hs300, o.alpha_vs_csiall, o.alpha_vs_equal
           FROM shadow_episode e
           LEFT JOIN shadow_outcome o ON o.episode_id = e.episode_id
           ORDER BY e.episode_id"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _stats(values, label=""):
    valid = [v for v in values if v is not None]
    if not valid:
        return {"label": label, "n": 0}
    arr = np.array(valid)
    return {
        "label": label,
        "n": len(valid),
        "mean_pct": float(np.mean(arr) * 100),
        "median_pct": float(np.median(arr) * 100),
        "std_pct": float(np.std(arr) * 100),
        "win_rate_pct": float(100.0 * np.sum(arr > 0) / len(arr)),
        "min_pct": float(np.min(arr) * 100),
        "max_pct": float(np.max(arr) * 100),
    }


def exp0c_failure_taxonomy(episodes):
    """P0: Failure Taxonomy - when does Guardian FAIL? (priority: know when NOT to bet)"""
    print("\n" + "=" * 70, flush=True)
    print("Exp0C (P0): Failure Taxonomy - When does Guardian FAIL?", flush=True)
    print("=" * 70, flush=True)

    # BUY episodes with non-zero returns
    buy = [
        e
        for e in episodes
        if e["decision"] == "BUY"
        and e["portfolio_return_t20"] is not None
        and e["portfolio_return_t20"] != 0
    ]
    print(f"\n  BUY episodes with non-zero return: {len(buy)}", flush=True)

    if len(buy) < 5:
        print("  INSUFFICIENT N for failure taxonomy", flush=True)
        # Use ALL episodes (including BLOCK) to understand failure environments
        print("  Analyzing ALL episodes by market_state instead...", flush=True)
        by_state = defaultdict(list)
        for e in episodes:
            if e["portfolio_return_t20"] is not None and e["portfolio_return_t20"] != 0:
                by_state[e["market_state"]].append(e)
        print(f"\n  {'state':25s} {'n':>4} {'mean_ret':>9} {'win_rate':>9}", flush=True)
        for state in sorted(by_state.keys()):
            sub = by_state[state]
            rets = [e["portfolio_return_t20"] for e in sub]
            s = _stats(rets, state)
            print(
                f"  {state:25s} {s['n']:4d} {s['mean_pct']:+7.2f}% {s['win_rate_pct']:7.1f}%",
                flush=True,
            )
        return {"n_buy_nonzero": len(buy), "fallback": "all_episodes_by_state"}

    # Classify failures: BUY but negative return or negative alpha
    failures = [
        e
        for e in buy
        if e["portfolio_return_t20"] < 0
        or (e["alpha_vs_hs300"] is not None and e["alpha_vs_hs300"] < -0.02)
    ]
    successes = [e for e in buy if e not in failures]
    print(f"  Successes (BUY, positive): {len(successes)}", flush=True)
    print(f"  Failures (BUY, negative):  {len(failures)}", flush=True)

    if failures:
        print("\n  Failure feature profiles:", flush=True)
        print(f"  {'feature':20s} {'failures_mean':>14} {'successes_mean':>14}", flush=True)
        for feat in ["vol_20d", "breadth", "trend_ma60", "recovery_prob", "confidence"]:
            f_vals = [e[feat] for e in failures if e[feat] is not None]
            s_vals = [e[feat] for e in successes if e[feat] is not None]
            if f_vals and s_vals:
                print(f"  {feat:20s} {np.mean(f_vals):14.4f} {np.mean(s_vals):14.4f}", flush=True)

        # Failure by market_state
        print("\n  Failures by market_state:", flush=True)
        fail_states = defaultdict(list)
        for e in failures:
            fail_states[e["market_state"]].append(e["portfolio_return_t20"])
        for state, rets in sorted(fail_states.items()):
            print(f"    {state:25s}: N={len(rets)}, mean={np.mean(rets) * 100:+.2f}%", flush=True)

    return {
        "n_buy": len(buy),
        "n_success": len(successes),
        "n_failure": len(failures),
        "failure_rate": float(len(failures) / len(buy)) if buy else 0,
    }


def exp0b_confidence_calibration(episodes):
    """P1: Confidence Calibration - is confidence predictive of outcome?"""
    print("\n" + "=" * 70, flush=True)
    print("Exp0B (P1): Confidence Calibration", flush=True)
    print("=" * 70, flush=True)

    buy = [
        e
        for e in episodes
        if e["decision"] == "BUY"
        and e["portfolio_return_t20"] is not None
        and e["portfolio_return_t20"] != 0
    ]

    if len(buy) < 5:
        print(f"  INSUFFICIENT N ({len(buy)} non-zero BUY episodes)", flush=True)
        # Use ALL episodes (BUY + BLOCK) for confidence calibration
        print("  Using ALL episodes with non-zero outcomes...", flush=True)
        buy = [
            e
            for e in episodes
            if e["portfolio_return_t20"] is not None and e["portfolio_return_t20"] != 0
        ]

    if len(buy) < 5:
        print("  Still insufficient. Skipping calibration.", flush=True)
        return {"skipped": True, "n": len(buy)}

    # Calibration by confidence_band
    print(f"\n  Calibration by confidence_band (N={len(buy)}):", flush=True)
    print(f"  {'band':10s} {'n':>4} {'mean_ret':>9} {'win_rate':>9} {'mean_alpha':>10}", flush=True)
    bands = defaultdict(list)
    for e in buy:
        bands[e["confidence_band"]].append(e)
    for band in ["blocked", "small", "normal", "full"]:
        sub = bands.get(band, [])
        if sub:
            rets = [e["portfolio_return_t20"] for e in sub]
            alphas = [e["alpha_vs_hs300"] for e in sub if e["alpha_vs_hs300"] is not None]
            print(
                f"  {band:10s} {len(sub):4d} {np.mean(rets) * 100:+7.2f}% "
                f"{100 * sum(1 for r in rets if r > 0) / len(rets):7.1f}% "
                f"{np.mean(alphas) * 100:+8.2f}%",
                flush=True,
            )

    # Calibration curve: confidence quintile vs actual win rate
    print("\n  Calibration curve (confidence quintile vs actual):", flush=True)
    conf_vals = [
        (e["confidence"], e["portfolio_return_t20"]) for e in buy if e["confidence"] is not None
    ]
    conf_vals.sort(key=lambda x: x[0])
    n = len(conf_vals)
    q = max(n // 5, 1)
    print(
        f"  {'quintile':10s} {'conf_range':>16} {'n':>4} {'mean_ret':>9} {'win_rate':>9}",
        flush=True,
    )
    calibration = []
    for qi in range(5):
        start = qi * q
        end = (qi + 1) * q if qi < 4 else n
        sub = conf_vals[start:end]
        if not sub:
            continue
        confs = [x[0] for x in sub]
        rets = [x[1] for x in sub]
        wr = 100.0 * sum(1 for r in rets if r > 0) / len(rets)
        print(
            f"  Q{qi + 1:1d}         [{min(confs):5.1f},{max(confs):5.1f}] "
            f"{len(sub):4d} {np.mean(rets) * 100:+7.2f}% {wr:7.1f}%",
            flush=True,
        )
        calibration.append(
            {
                "quintile": qi + 1,
                "conf_range": [float(min(confs)), float(max(confs))],
                "n": len(sub),
                "mean_ret_pct": float(np.mean(rets) * 100),
                "win_rate_pct": float(wr),
            }
        )

    # Calibration verdict
    if len(calibration) >= 2:
        wr_low = calibration[0]["win_rate_pct"]
        wr_high = calibration[-1]["win_rate_pct"]
        delta = wr_high - wr_low
        print(f"\n  Calibration delta (Q5 - Q1 win rate): {delta:+.1f}pp", flush=True)
        if delta > 10:
            print("  -> CALIBRATED: confidence predicts outcome (delta > 10pp)", flush=True)
        elif delta > 0:
            print("  -> WEAKLY CALIBRATED: positive but small delta", flush=True)
        else:
            print("  -> NOT CALIBRATED: confidence does NOT predict outcome", flush=True)

    return {"calibration": calibration, "n": len(buy)}


def exp0a_regime_map(episodes):
    """P2: Regime Map - unsupervised regime discovery (no arbitrary bins)"""
    print("\n" + "=" * 70, flush=True)
    print("Exp0A (P2): Regime Map (unsupervised discovery)", flush=True)
    print("=" * 70, flush=True)

    # Features for clustering
    features = ["vol_20d", "breadth", "trend_ma60", "recovery_prob"]
    valid = [
        e
        for e in episodes
        if all(e.get(f) is not None for f in features)
        and e["portfolio_return_t20"] is not None
        and e["portfolio_return_t20"] != 0
    ]

    print(f"  Episodes with all features + non-zero return: {len(valid)}", flush=True)

    if len(valid) < 10:
        print("  INSUFFICIENT N for unsupervised clustering", flush=True)
        # Fallback: use market_state as regime proxy
        print("  Fallback: market_state as regime proxy", flush=True)
        by_state = defaultdict(list)
        for e in valid:
            by_state[e["market_state"]].append(e)
        print(f"\n  {'state':25s} {'n':>4} {'mean_ret':>9} {'win_rate':>9}", flush=True)
        for state in sorted(by_state.keys()):
            sub = by_state[state]
            rets = [e["portfolio_return_t20"] for e in sub]
            print(
                f"  {state:25s} {len(sub):4d} {np.mean(rets) * 100:+7.2f}% "
                f"{100 * sum(1 for r in rets if r > 0) / len(rets):7.1f}%",
                flush=True,
            )
        return {"n": len(valid), "fallback": "market_state"}

    # Simple k-means (k=3) on normalized features
    X = np.array([[e[f] for f in features] for e in valid], dtype=float)
    # Normalize
    X_norm = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-9)

    # K-means (k=3)
    k = 3
    rng = np.random.default_rng(42)
    centroids = X_norm[rng.choice(len(X_norm), k, replace=False)]
    for _ in range(50):
        dists = np.linalg.norm(X_norm[:, None] - centroids[None, :], axis=2)
        labels = np.argmin(dists, axis=1)
        new_centroids = np.array(
            [
                X_norm[labels == i].mean(axis=0) if np.any(labels == i) else centroids[i]
                for i in range(k)
            ]
        )
        if np.allclose(centroids, new_centroids):
            break
        centroids = new_centroids

    print(f"\n  Unsupervised regimes (k={k}):", flush=True)
    print(
        f"  {'regime':8s} {'n':>4} {'vol':>7} {'breadth':>8} {'trend':>7} {'recov':>7} "
        f"{'mean_ret':>9} {'win_rate':>9}",
        flush=True,
    )
    regimes = {}
    for ri in range(k):
        mask = labels == ri
        sub = [valid[i] for i in range(len(valid)) if mask[i]]
        if not sub:
            continue
        rets = [e["portfolio_return_t20"] for e in sub]
        wr = 100.0 * sum(1 for r in rets if r > 0) / len(rets)
        vol_mean = np.mean([e["vol_20d"] for e in sub])
        br_mean = np.mean([e["breadth"] for e in sub])
        tr_mean = np.mean([e["trend_ma60"] for e in sub])
        rc_mean = np.mean([e["recovery_prob"] for e in sub])
        print(
            f"  R{ri + 1}       {len(sub):4d} {vol_mean:7.3f} {br_mean:8.3f} {tr_mean:7.3f} "
            f"{rc_mean:7.3f} {np.mean(rets) * 100:+7.2f}% {wr:7.1f}%",
            flush=True,
        )
        regimes[f"R{ri + 1}"] = {
            "n": len(sub),
            "features": {
                "vol": float(vol_mean),
                "breadth": float(br_mean),
                "trend": float(tr_mean),
                "recovery_prob": float(rc_mean),
            },
            "mean_ret_pct": float(np.mean(rets) * 100),
            "win_rate_pct": float(wr),
        }

    return {"n": len(valid), "regimes": regimes, "k": k}


def exp0d_beta_attribution(episodes):
    """P3: Beta Attribution - is Guardian alpha real or just beta rebound?"""
    print("\n" + "=" * 70, flush=True)
    print("Exp0D (P3): Beta Attribution - real alpha or beta rebound?", flush=True)
    print("=" * 70, flush=True)

    buy = [
        e
        for e in episodes
        if e["decision"] == "BUY"
        and e["portfolio_return_t20"] is not None
        and e["portfolio_return_t20"] != 0
        and e["market_return_t20"] is not None
    ]

    print(f"  BUY episodes with return + market data: {len(buy)}", flush=True)

    if len(buy) < 5:
        print("  INSUFFICIENT N for beta attribution", flush=True)
        return {"skipped": True, "n": len(buy)}

    rets = np.array([e["portfolio_return_t20"] for e in buy])
    mkts = np.array([e["market_return_t20"] for e in buy])
    alphas = np.array([e["alpha_vs_hs300"] for e in buy if e["alpha_vs_hs300"] is not None])

    print("\n  Attribution decomposition:", flush=True)
    print(f"    Portfolio return (mean):  {np.mean(rets) * 100:+.2f}%", flush=True)
    print(f"    Market return (mean):     {np.mean(mkts) * 100:+.2f}%", flush=True)
    print(f"    Alpha vs HS300 (mean):    {np.mean(alphas) * 100:+.2f}%", flush=True)
    print(
        f"    Beta contribution:        {np.mean(mkts) * 100:+.2f}% (= market return)", flush=True
    )
    print(
        f"    True Guardian alpha:      {np.mean(alphas) * 100:+.2f}% (= portfolio - market)",
        flush=True,
    )

    # Ratio: how much of total return is alpha vs beta?
    total_ret = np.mean(rets)
    beta_part = np.mean(mkts)
    alpha_part = np.mean(alphas) if len(alphas) == len(rets) else total_ret - beta_part

    if abs(total_ret) > 1e-6:
        alpha_ratio = alpha_part / total_ret
        beta_ratio = beta_part / total_ret
        print("\n  Attribution ratio:", flush=True)
        print(f"    Alpha share: {alpha_ratio * 100:.1f}%", flush=True)
        print(f"    Beta share:  {beta_ratio * 100:.1f}%", flush=True)

        if alpha_ratio > 0.3:
            verdict = "REAL ALPHA: Guardian adds > 30% beyond market beta"
        elif alpha_ratio > 0:
            verdict = "MARGINAL ALPHA: Guardian adds small amount beyond beta"
        else:
            verdict = "NO ALPHA: Guardian is pure beta rebound exposure"
        print(f"    Verdict: {verdict}", flush=True)
    else:
        verdict = "INCONCLUSIVE: total return too small to attribute"
        print(f"    {verdict}", flush=True)

    return {
        "n": len(buy),
        "portfolio_return_pct": float(np.mean(rets) * 100),
        "market_return_pct": float(np.mean(mkts) * 100),
        "alpha_pct": float(np.mean(alphas) * 100) if len(alphas) else None,
        "verdict": verdict,
    }


def run_autopsy():
    episodes = load_episodes()
    print("=" * 70, flush=True)
    print("v4.0 Phase 0: Guardian Autopsy (6-S.19.2)", flush=True)
    print("Investment Committee Diagnostic Tool", flush=True)
    print("=" * 70, flush=True)
    print(f"\nTotal episodes: {len(episodes)}", flush=True)

    buy_all = [e for e in episodes if e["decision"] == "BUY"]
    buy_nonzero = [
        e
        for e in buy_all
        if e["portfolio_return_t20"] is not None and e["portfolio_return_t20"] != 0
    ]
    print(f"  BUY episodes: {len(buy_all)}", flush=True)
    print(f"  BUY with non-zero return: {len(buy_nonzero)}", flush=True)
    print("  (Note: many early episodes have 0.0 return = not evaluated)", flush=True)

    # Run in priority order: C (failure) -> B (calibration) -> A (regime) -> D (attribution)
    r_c = exp0c_failure_taxonomy(episodes)
    r_b = exp0b_confidence_calibration(episodes)
    r_a = exp0a_regime_map(episodes)
    r_d = exp0d_beta_attribution(episodes)

    # Summary
    print("\n" + "=" * 70, flush=True)
    print("AUTOPSY SUMMARY", flush=True)
    print("=" * 70, flush=True)
    print(
        f"\n  Exp0C (Failure Taxonomy): {r_c.get('n_failure', 'N/A')} failures of "
        f"{r_c.get('n_buy', 'N/A')} BUY episodes",
        flush=True,
    )
    print(
        f"  Exp0B (Calibration): {'see calibration curve' if not r_b.get('skipped') else 'SKIPPED (insufficient N)'}",
        flush=True,
    )
    print(
        f"  Exp0A (Regime Map): {len(r_a.get('regimes', {}))} regimes discovered"
        if r_a.get("regimes")
        else "  Exp0A: fallback",
        flush=True,
    )
    if r_d.get("verdict"):
        print(f"  Exp0D (Beta Attribution): {r_d['verdict']}", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.19.2",
        "experiment": "guardian_autopsy",
        "date": str(date.today()),
        "n_total_episodes": len(episodes),
        "n_buy": len(buy_all),
        "n_buy_nonzero": len(buy_nonzero),
        "exp0c_failure_taxonomy": r_c,
        "exp0b_confidence_calibration": r_b,
        "exp0a_regime_map": r_a,
        "exp0d_beta_attribution": r_d,
    }
    report_path = os.path.join(REPORT_DIR, f"guardian_autopsy_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_autopsy()
