"""
v3.5 Phase 1 Exp1: FRM Direction vs Score Attribution - Commit 6-S.17.1.

THE HIGHEST-PRIORITY experiment in the Security Analyst evolution chain.
Answers: did we freeze a real signal, or a layer of packaging?

    FRM score (0-100, weighted 50/30/20 earnings/margin/revenue)
        |
        ?  (what is the alpha source?)
        |
    +2.52% alpha (v3.2.1 Group B, N=60)

Three competing hypotheses:
  H2 (direction-only): FRM score is a noisy proxy for direction. The
      0-100 score adds NO incremental information. Security Analyst
      should simplify to a Recovery Direction Detector.
  H1 (score incremental): Score adds value WITHIN direction. Direction
      is layer 1 (is it recovering?), score is layer 2 (how strong?).
      Keep FRM as-is.
  H3 (uncertainty): Score itself is an inverted-U (like gap and
      sustainability). Alpha is in the middle, not the extremes. Score
      is an uncertainty proxy, not a quality signal.

Three-layer test design (per design review):
  Test 1: Direction baseline - alpha of improving-only / stable-only /
           combined. Establishes whether direction alone explains alpha.
  Test 2: Conditional score - WITHIN each direction, quintile FRM score
           vs alpha. Tests whether score adds incremental signal beyond
           direction. (THE KEY TEST)
  Test 3: Incremental regression - future_alpha = b1*direction +
           b2*frm_score + controls. Tests b2 significance.
  Test 4: Temporal stability - split by year/market_state to verify
           the signal is not a single-regime artifact.

Verdict rules (frozen):
  H2 PASS: direction alpha ≈ FRM alpha
           AND within-direction score monotonicity absent (Q5 !> Q1)
           AND b2 ≈ 0
           -> FRM score = unnecessary complexity. Simplify to direction.
  H1 PASS: within-direction: high score > low score (Q5 > Q1 by >= 1pp)
           AND b2 positive
           -> keep FRM score
  H3 SIGNAL: within-direction score shows inverted-U (Q-mid > Q-extremes)
           -> score is uncertainty proxy, route to H3 investigation

Usage:
    python scripts/run_v3_5_exp1_direction_vs_score.py
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

# Verdict thresholds (frozen)
H1_Q5_MINUS_Q1_PP = 1.0  # Q5 > Q1 by >= 1.0pp within direction
H2_BETA2_TOLERANCE = 0.5  # |b2| normalized < 0.5pp = approximately zero
MIN_N_PER_QUINTILE = 5  # need >= 5 per quintile for reliable stats


def load_group_a():
    """Load Group A (FRM-only, v3.2.1 Group B reproduced)."""
    con = sqlite3.connect(SHADOW_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT v.security_id, v.trade_date, v.episode_id,
                  v.residual_alpha, v.market_beta, v.sector_beta,
                  v.stock_return_t20, v.frm_direction, v.frm_score,
                  v.earnings_acceleration, v.frm_score,
                  e.market_state
           FROM shadow_candidates_v3 v
           JOIN shadow_episode e ON e.episode_id = v.episode_id
           WHERE v.residual_alpha IS NOT NULL
             AND v.rs_data_available = 0
           ORDER BY v.id"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _stats(rows, label=""):
    """Compute residual_alpha stats for a row list."""
    if not rows:
        return {
            "label": label,
            "n": 0,
            "alpha": None,
            "positive_rate": None,
            "median": None,
            "std": None,
        }
    ra = np.array([r["residual_alpha"] for r in rows])
    ret = np.array([r["stock_return_t20"] for r in rows if r["stock_return_t20"] is not None])
    return {
        "label": label,
        "n": len(rows),
        "alpha_pct": float(np.mean(ra) * 100),
        "median_pct": float(np.median(ra) * 100),
        "positive_rate_pct": float(100.0 * np.sum(ra > 0) / len(ra)),
        "std_pct": float(np.std(ra) * 100),
        "return_mean_pct": float(np.mean(ret) * 100) if len(ret) else None,
    }


def _print_stats(s, indent=4):
    pad = " " * indent
    if s["n"] == 0:
        print(f"{pad}{s['label']}: EMPTY", flush=True)
        return
    print(
        f"{pad}{s['label']}: N={s['n']}  alpha={s['alpha_pct']:+.2f}%  "
        f"positive={s['positive_rate_pct']:.1f}%  "
        f"median={s['median_pct']:+.2f}%",
        flush=True,
    )


def test1_direction_baseline(rows):
    """Test 1: Direction baseline.

    Establish whether direction alone (improving/stable/combined) explains
    the FRM alpha. If improving+stable ≈ full FRM (+2.52%), direction may
    be sufficient (supports H2).
    """
    print("\n" + "=" * 70, flush=True)
    print("TEST 1: Direction Baseline", flush=True)
    print("=" * 70, flush=True)

    groups = {
        "improving_only": [r for r in rows if r["frm_direction"] == "improving"],
        "stable_only": [r for r in rows if r["frm_direction"] == "stable"],
        "combined_imp_stable": [r for r in rows if r["frm_direction"] in ("improving", "stable")],
        "all_group_a": rows,
    }
    results = {}
    for label, grp in groups.items():
        s = _stats(grp, label)
        results[label] = s
        _print_stats(s)

    # H2 early signal: does combined (direction-only, no score) match full FRM?
    combined_alpha = results["combined_imp_stable"]["alpha_pct"]
    full_alpha = results["all_group_a"]["alpha_pct"]
    delta = combined_alpha - full_alpha
    print("\n  Direction-only (improving+stable) vs full FRM:", flush=True)
    print(
        f"    combined: {combined_alpha:+.2f}%   full FRM: {full_alpha:+.2f}%   "
        f"delta: {delta:+.2f}pp",
        flush=True,
    )
    print("    (delta < 0.5pp supports H2: direction alone is sufficient)", flush=True)

    return results, delta


def test2_conditional_score(rows):
    """Test 2: Conditional score (THE KEY TEST).

    WITHIN each direction, quintile FRM score vs alpha. If score adds
    incremental signal, Q5 (highest score) should have higher alpha than
    Q1 (lowest score) within the same direction.

    Verdict:
      Q5 > Q1 by >= 1pp  -> H1 (score adds value)
      Q5 ≈ Q1 or Q5 < Q1 -> H2 (score is noise within direction)
      Q-mid > Q-extremes  -> H3 (score is uncertainty proxy, inverted-U)
    """
    print("\n" + "=" * 70, flush=True)
    print("TEST 2: Conditional Score (WITHIN direction) - THE KEY TEST", flush=True)
    print("=" * 70, flush=True)

    results = {}
    for direction in ["improving", "stable"]:
        sub = [r for r in rows if r["frm_direction"] == direction and r["frm_score"] is not None]
        print(f"\n  --- {direction} (N={len(sub)}) ---", flush=True)
        if len(sub) < MIN_N_PER_QUINTILE * 5:
            print(f"    SKIP: N={len(sub)} < {MIN_N_PER_QUINTILE * 5} for 5 quintiles", flush=True)
            results[direction] = {"n": len(sub), "skipped": True}
            continue

        # Sort by frm_score, split into 5 quintiles
        sub_sorted = sorted(sub, key=lambda r: r["frm_score"])
        n = len(sub_sorted)
        q_size = n // 5
        quintiles = {}
        print(
            f"    {'quintile':10s} {'n':>4} {'frm_range':>16} {'alpha':>8} {'positive':>9}",
            flush=True,
        )
        for qi in range(5):
            start = qi * q_size
            end = (qi + 1) * q_size if qi < 4 else n
            q_rows = sub_sorted[start:end]
            if not q_rows:
                continue
            s = _stats(q_rows, f"Q{qi + 1}")
            scores = [r["frm_score"] for r in q_rows]
            quintiles[f"Q{qi + 1}"] = {
                "n": s["n"],
                "frm_score_range": [float(min(scores)), float(max(scores))],
                "alpha_pct": s["alpha_pct"],
                "positive_rate_pct": s["positive_rate_pct"],
            }
            print(
                f"    Q{qi + 1:1d}         {s['n']:4d}  "
                f"[{min(scores):5.1f},{max(scores):5.1f}]   "
                f"{s['alpha_pct']:+6.2f}%  {s['positive_rate_pct']:7.1f}%",
                flush=True,
            )

        # Monotonicity test: Q5 vs Q1
        if "Q1" in quintiles and "Q5" in quintiles:
            q5_minus_q1 = quintiles["Q5"]["alpha_pct"] - quintiles["Q1"]["alpha_pct"]
            print(f"\n    Q5 - Q1 (alpha delta): {q5_minus_q1:+.2f}pp", flush=True)
            if q5_minus_q1 >= H1_Q5_MINUS_Q1_PP:
                verdict = "H1 SIGNAL (score adds value: Q5 > Q1)"
            elif q5_minus_q1 <= -H1_Q5_MINUS_Q1_PP:
                verdict = "H2/H3 SIGNAL (Q5 < Q1 - score is noise OR inverted-U)"
            else:
                verdict = "H2 SIGNAL (Q5 ≈ Q1 - score adds no value)"
            print(f"    Verdict: {verdict}", flush=True)
            quintiles["q5_minus_q1_pp"] = float(q5_minus_q1)
            quintiles["verdict"] = verdict

        # Inverted-U check (H3): is Q3 > Q1 AND Q3 > Q5?
        if all(k in quintiles for k in ("Q1", "Q3", "Q5")):
            q1, q3, q5 = (
                quintiles["Q1"]["alpha_pct"],
                quintiles["Q3"]["alpha_pct"],
                quintiles["Q5"]["alpha_pct"],
            )
            if q3 > q1 + 1 and q3 > q5 + 1:
                print(
                    f"    H3 SIGNAL: Q3 ({q3:+.2f}%) > Q1 ({q1:+.2f}%) AND > Q5 ({q5:+.2f}%) "
                    f"- inverted-U, score is uncertainty proxy",
                    flush=True,
                )
                quintiles["h3_signal"] = True

        results[direction] = quintiles

    return results


def test3_incremental_regression(rows):
    """Test 3: Incremental regression.

    future_alpha = b0 + b1 * direction_dummy + b2 * frm_score_normalized
                   + b3 * market_state_dummy (control)

    H2: b2 ≈ 0 (score adds no incremental info beyond direction)
    H1: b2 > 0 (score adds positive incremental info)
    """
    print("\n" + "=" * 70, flush=True)
    print("TEST 3: Incremental Regression (direction + score)", flush=True)
    print("=" * 70, flush=True)

    # Prepare regression data
    # direction_dummy: improving=1, stable=0.5, deteriorating=0 (but deteriorating hard-rejected)
    # Use binary: improving=1, stable=0
    reg_rows = [
        r
        for r in rows
        if r["frm_direction"] in ("improving", "stable")
        and r["frm_score"] is not None
        and r["residual_alpha"] is not None
    ]

    if len(reg_rows) < 20:
        print(f"  SKIP: N={len(reg_rows)} < 20 for regression", flush=True)
        return {"n": len(reg_rows), "skipped": True}

    y = np.array([r["residual_alpha"] * 100 for r in reg_rows])  # in pp
    # direction: improving=1, stable=0
    x_dir = np.array([1.0 if r["frm_direction"] == "improving" else 0.0 for r in reg_rows])
    # frm_score normalized to 0-1
    scores = np.array([r["frm_score"] for r in reg_rows])
    x_score = (scores - scores.min()) / (scores.max() - scores.min() + 1e-9)

    # Simple OLS: y = b0 + b1*x_dir + b2*x_score
    X = np.column_stack([np.ones(len(y)), x_dir, x_score])
    try:
        beta, residuals, rank, sv = np.linalg.lstsq(X, y, rcond=None)
        y_pred = X @ beta
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        # Standard errors (simplified, no robust SE)
        n, k = X.shape
        mse = ss_res / (n - k) if n > k else 0
        try:
            var_beta = mse * np.linalg.inv(X.T @ X)
            se_beta = np.sqrt(np.diag(var_beta))
            t_stats = beta / se_beta if se_beta.all() else [0, 0, 0]
        except np.linalg.LinAlgError:
            se_beta = np.array([0, 0, 0])
            t_stats = np.array([0, 0, 0])

        print(f"  N = {n}, R² = {r2:.3f}", flush=True)
        print(f"  {'var':20s} {'beta':>8} {'se':>8} {'t':>6}", flush=True)
        print(f"  {'intercept':20s} {beta[0]:8.3f} {se_beta[0]:8.3f} {t_stats[0]:6.2f}", flush=True)
        print(
            f"  {'direction(imp=1)':20s} {beta[1]:8.3f} {se_beta[1]:8.3f} {t_stats[1]:6.2f}",
            flush=True,
        )
        print(
            f"  {'frm_score(norm)':20s} {beta[2]:8.3f} {se_beta[2]:8.3f} {t_stats[2]:6.2f}",
            flush=True,
        )

        b2 = beta[2]
        t2 = t_stats[2]
        print(f"\n  b2 (frm_score coefficient) = {b2:+.3f}pp per unit score", flush=True)
        print(f"  t-statistic = {t2:.2f}  (|t| > 2 = significant at 5%)", flush=True)
        if abs(b2) < H2_BETA2_TOLERANCE and abs(t2) < 2:
            verdict = "H2: b2 ≈ 0 and not significant - score adds NO incremental info"
        elif b2 > 0 and t2 > 2:
            verdict = "H1: b2 > 0 and significant - score adds positive info"
        elif b2 < 0 and t2 < -2:
            verdict = "H3/INVERTED: b2 < 0 - higher score predicts LOWER alpha"
        else:
            verdict = "INCONCLUSIVE: b2 not clearly zero or significant"
        print(f"  Verdict: {verdict}", flush=True)

        return {
            "n": n,
            "r2": float(r2),
            "b0_intercept": float(beta[0]),
            "b1_direction": float(beta[1]),
            "b2_frm_score": float(b2),
            "b2_t_stat": float(t2),
            "b2_se": float(se_beta[2]),
            "verdict": verdict,
        }
    except Exception as e:
        print(f"  Regression failed: {e}", flush=True)
        return {"n": n, "error": str(e)}


def test4_temporal_stability(rows):
    """Test 4: Temporal stability.

    Split by year to verify the FRM signal is not a single-regime
    artifact. If alpha is concentrated in one year, the signal may be
    regime-specific, not a stable investment capability.
    """
    print("\n" + "=" * 70, flush=True)
    print("TEST 4: Temporal Stability (by year)", flush=True)
    print("=" * 70, flush=True)

    by_year = defaultdict(list)
    for r in rows:
        y = r["trade_date"][:4]
        by_year[y].append(r)

    results = {}
    print(
        f"  {'year':6s} {'n':>4} {'alpha':>8} {'positive':>9} {'imp%':>6} {'stable%':>8}",
        flush=True,
    )
    for y in sorted(by_year.keys()):
        sub = by_year[y]
        s = _stats(sub, y)
        n_imp = sum(1 for r in sub if r["frm_direction"] == "improving")
        n_stab = sum(1 for r in sub if r["frm_direction"] == "stable")
        imp_pct = 100.0 * n_imp / len(sub) if sub else 0
        stab_pct = 100.0 * n_stab / len(sub) if sub else 0
        print(
            f"  {y:6s} {s['n']:4d} {s['alpha_pct']:+7.2f}% "
            f"{s['positive_rate_pct']:7.1f}% {imp_pct:5.1f}% {stab_pct:7.1f}%",
            flush=True,
        )
        results[y] = {
            "n": s["n"],
            "alpha_pct": s["alpha_pct"],
            "positive_rate_pct": s["positive_rate_pct"],
            "improving_pct": float(imp_pct),
            "stable_pct": float(stab_pct),
        }

    # Also split improving direction by year (is the signal stable within direction?)
    print("\n  Improving-only by year:", flush=True)
    print(f"  {'year':6s} {'n':>4} {'alpha':>8} {'positive':>9}", flush=True)
    imp_by_year = defaultdict(list)
    for r in rows:
        if r["frm_direction"] == "improving":
            imp_by_year[r["trade_date"][:4]].append(r)
    for y in sorted(imp_by_year.keys()):
        s = _stats(imp_by_year[y], y)
        print(
            f"  {y:6s} {s['n']:4d} {s['alpha_pct']:+7.2f}% {s['positive_rate_pct']:7.1f}%",
            flush=True,
        )
        results[f"improving_{y}"] = {
            "n": s["n"],
            "alpha_pct": s["alpha_pct"],
            "positive_rate_pct": s["positive_rate_pct"],
        }

    # Stability verdict
    alphas = [results[y]["alpha_pct"] for y in sorted(by_year.keys()) if results[y]["n"] >= 5]
    if len(alphas) >= 2:
        all_positive = all(a > 0 for a in alphas)
        spread = max(alphas) - min(alphas)
        print(
            f"\n  Yearly alpha spread: {spread:.2f}pp (min {min(alphas):+.2f}%, max {max(alphas):+.2f}%)",
            flush=True,
        )
        if all_positive:
            print("  STABLE: alpha positive in all years with N>=5", flush=True)
        else:
            print(
                "  UNSTABLE: alpha negative in at least one year - signal may be regime-specific",
                flush=True,
            )
        results["stability_spread_pp"] = float(spread)
        results["all_years_positive"] = bool(all_positive)

    return results


def run_exp1():
    rows = load_group_a()
    print("=" * 70, flush=True)
    print("v3.5 Phase 1 Exp1: FRM Direction vs Score Attribution (6-S.17.1)", flush=True)
    print("=" * 70, flush=True)
    print(f"\nGroup A (FRM-only, v3.2.1 Group B): N={len(rows)}", flush=True)
    print("Baseline: full FRM +2.52%, 58.3% positive", flush=True)

    test1_results, dir_delta = test1_direction_baseline(rows)
    test2_results = test2_conditional_score(rows)
    test3_results = test3_incremental_regression(rows)
    test4_results = test4_temporal_stability(rows)

    # ─── Verdict synthesis ───
    print("\n" + "=" * 70, flush=True)
    print("VERDICT SYNTHESIS", flush=True)
    print("=" * 70, flush=True)

    # H2 signals
    h2_signals = []
    if abs(dir_delta) < 0.5:
        h2_signals.append(f"direction-only ≈ full FRM (delta {dir_delta:+.2f}pp < 0.5pp)")
    if isinstance(test3_results, dict) and "b2_frm_score" in test3_results:
        b2 = test3_results["b2_frm_score"]
        t2 = test3_results.get("b2_t_stat", 0)
        if abs(b2) < H2_BETA2_TOLERANCE and abs(t2) < 2:
            h2_signals.append(
                f"regression b2={b2:+.3f} (|t|={abs(t2):.2f}<2) - score not significant"
            )

    # H1 signals
    h1_signals = []
    for direction in ["improving", "stable"]:
        q = test2_results.get(direction, {})
        if isinstance(q, dict) and "q5_minus_q1_pp" in q:
            if q["q5_minus_q1_pp"] >= H1_Q5_MINUS_Q1_PP:
                h1_signals.append(f"{direction}: Q5-Q1 = {q['q5_minus_q1_pp']:+.2f}pp >= +1pp")
    if isinstance(test3_results, dict) and "b2_frm_score" in test3_results:
        b2 = test3_results["b2_frm_score"]
        t2 = test3_results.get("b2_t_stat", 0)
        if b2 > 0 and t2 > 2:
            h1_signals.append(
                f"regression b2={b2:+.3f} (t={t2:.2f}>2) - score significant positive"
            )

    # H3 signals
    h3_signals = []
    for direction in ["improving", "stable"]:
        q = test2_results.get(direction, {})
        if isinstance(q, dict) and q.get("h3_signal"):
            h3_signals.append(f"{direction}: inverted-U (Q3 > Q1 AND Q3 > Q5)")

    print(f"\n  H2 signals (direction-only): {len(h2_signals)}", flush=True)
    for s in h2_signals:
        print(f"    + {s}", flush=True)
    print(f"  H1 signals (score incremental): {len(h1_signals)}", flush=True)
    for s in h1_signals:
        print(f"    + {s}", flush=True)
    print(f"  H3 signals (uncertainty/inverted-U): {len(h3_signals)}", flush=True)
    for s in h3_signals:
        print(f"    + {s}", flush=True)

    # Decision
    if len(h2_signals) >= 2 and len(h1_signals) == 0:
        verdict = "H2 PASS - FRM score is unnecessary complexity. Direction is the signal."
        recommendation = "Simplify Security Analyst to Recovery Direction Detector. FRM 0-100 score can be replaced by direction gate."
    elif len(h1_signals) >= 2:
        verdict = "H1 PASS - FRM score adds incremental value beyond direction."
        recommendation = "Keep FRM score. Proceed to Exp2 (layer ablation) to identify which score layer drives alpha."
    elif len(h3_signals) >= 1:
        verdict = "H3 SIGNAL - FRM score shows inverted-U (uncertainty proxy)."
        recommendation = "Route to H3 investigation. Score is not a quality signal but an uncertainty proxy. Combine with gap/sustainability inverted-U findings."
    else:
        verdict = "INCONCLUSIVE - signals mixed or insufficient. Keep FRM as frozen black-box."
        recommendation = (
            "Do not simplify or decompose further. Accept FRM as validated-but-unexplained."
        )

    print(f"\n  >>> VERDICT: {verdict}", flush=True)
    print(f"  >>> RECOMMENDATION: {recommendation}", flush=True)

    # Export report
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.17.1",
        "phase": "1",
        "experiment": "exp1_direction_vs_score",
        "date": str(date.today()),
        "baseline": "v3.2.1 Group B (FRM-only): N=60, +2.52%, 58.3% positive",
        "test1_direction_baseline": test1_results,
        "test2_conditional_score": test2_results,
        "test3_regression": test3_results if isinstance(test3_results, dict) else {},
        "test4_temporal_stability": test4_results,
        "h2_signal_count": len(h2_signals),
        "h1_signal_count": len(h1_signals),
        "h3_signal_count": len(h3_signals),
        "verdict": verdict,
        "recommendation": recommendation,
    }
    report_path = os.path.join(REPORT_DIR, f"v3_5_exp1_direction_vs_score_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_exp1()
