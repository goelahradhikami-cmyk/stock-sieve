"""
v3.5.1 Exp9: Alpha Attribution Regression - Commit 6-S.17.5.

THE CRITICAL TEST for H3-A vs H3-B mechanism fork. Uses nested regression
(3 models) to isolate whether alpha comes from uncertainty asymmetry,
crowding avoidance, or both.

Why nested (not single model):
  A single model conflates mechanisms. Nested regression reveals which
  variable ADDS incremental explanatory power:
    Model 0 (baseline): alpha ~ frm_direction + market_cap + volatility
      -> confirms FRM direction baseline
    Model 1 (+uncertainty): Model 0 + uncertainty_zone
      -> tests H3-A: does uncertainty add incremental R²?
    Model 2 (+crowding): Model 1 + crowding_score
      -> tests H3-B: does crowding add incremental R² beyond uncertainty?
      -> AND: does uncertainty survive crowding control?

Decision rules (frozen 6-S.17.3, refined by design review):
  Model 2 verdict:
    b_uncertainty positive significant + b_crowding insignificant
      -> H3-A VALIDATED: Uncertainty Asymmetry Premium
      -> Paradigm shift to Uncertainty Asymmetry Detector

    b_uncertainty insignificant + b_crowding negative significant
      -> H3-B VALIDATED: Crowding Avoidance
      -> No paradigm shift, add crowding filter

    b_uncertainty positive + b_crowding negative (both significant)
      -> MIXED: uncertainty produces alpha, crowding suppresses it
      -> v3.5.2 decompose further; architecture = uncertainty zone + low crowding

    both insignificant
      -> INCONCLUSIVE: extend N, do not guess

Key control: frm_direction MUST be in all models. Without it,
uncertainty_zone may just be a direction proxy (Exp1 showed direction
= 99.6% of FRM alpha). The regression measures uncertainty_zone's
INCREMENTAL contribution beyond direction.

Usage:
    python scripts/run_v3_5_1_exp9_attribution.py
"""

from __future__ import annotations

import os
import sys
import sqlite3
import json
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.expectation_gap import ExpectationGapEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"
REPORT_DIR = "data/reports"

# Bucket thresholds (frozen from Exp8)
FRM_LOW_MAX = 57.0
FRM_HIGH_MIN = 68.0


def load_regression_data():
    """Load Group A with all regression variables joined."""
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    cache = sqlite3.connect(CACHE_DB)
    cache.row_factory = sqlite3.Row

    rows = shadow.execute(
        """SELECT v.security_id, v.trade_date, v.residual_alpha,
                  v.frm_direction, v.frm_score, v.stock_return_t20
           FROM shadow_candidates_v3 v
           WHERE v.residual_alpha IS NOT NULL AND v.rs_data_available = 0
           ORDER BY v.id"""
    ).fetchall()

    # Crowding
    crowd_map = {}
    for r in cache.execute("SELECT * FROM crowding_snapshot").fetchall():
        crowd_map[(r["security_id"], r["trade_date"])] = dict(r)

    # Sustainability
    sustain_map = {}
    for r in cache.execute(
        "SELECT security_id, as_of_date, profit_elasticity, accel_trend, company_margin_zscore "
        "FROM earnings_sustainability"
    ).fetchall():
        sustain_map[(r["security_id"], r["as_of_date"])] = dict(r)

    # Gap (compute via EGE)
    ege = ExpectationGapEngine(cache_db=CACHE_DB)

    # First pass: collect gap and sustain values for terciles
    gap_vals = []
    sustain_vals = []
    enriched = []
    for r in rows:
        sid, td = r["security_id"], r["trade_date"]
        gap = None
        try:
            score = ege.compute(sid, td)
            gap = score.gap_score
        except Exception:
            pass
        gap_vals.append(gap)

        s = sustain_map.get((sid, td))
        sp = None
        if s:
            vals = []
            if s.get("profit_elasticity") is not None and abs(s["profit_elasticity"]) < 20:
                vals.append(min(s["profit_elasticity"], 5.0) / 5.0)
            if s.get("accel_trend") is not None:
                vals.append(max(-1, min(1, s["accel_trend"])))
            if s.get("company_margin_zscore") is not None:
                vals.append(max(-1, min(1, -s["company_margin_zscore"] / 3.0)))
            if vals:
                sp = float(np.mean(vals))
        sustain_vals.append(sp)

        crowd = crowd_map.get((sid, td), {})
        enriched.append({
            "sid": sid, "td": td,
            "alpha": r["residual_alpha"],
            "frm_dir": r["frm_direction"],
            "frm_score": r["frm_score"],
            "gap": gap,
            "sustain": sp,
            "crowd_score": crowd.get("crowding_score_v1"),
            "market_cap": crowd.get("market_cap"),
            "realized_vol": crowd.get("realized_vol_20d"),
        })

    shadow.close()
    cache.close()

    # Terciles
    gap_valid = sorted([v for v in gap_vals if v is not None])
    sus_valid = sorted([v for v in sustain_vals if v is not None])
    gap_t1 = gap_valid[len(gap_valid) // 3] if len(gap_valid) >= 3 else None
    gap_t2 = gap_valid[2 * len(gap_valid) // 3] if len(gap_valid) >= 3 else None
    sus_t1 = sus_valid[len(sus_valid) // 3] if len(sus_valid) >= 3 else None
    sus_t2 = sus_valid[2 * len(sus_valid) // 3] if len(sus_valid) >= 3 else None

    # Bucket and compute uncertainty_zone
    for e in enriched:
        frm_b = "L" if (e["frm_score"] or 0) < FRM_LOW_MAX else ("M" if (e["frm_score"] or 0) < FRM_HIGH_MIN else "H")
        gap_b = None
        if e["gap"] is not None and gap_t1 is not None:
            gap_b = "L" if e["gap"] < gap_t1 else ("M" if e["gap"] < gap_t2 else "H")
        sus_b = None
        if e["sustain"] is not None and sus_t1 is not None:
            sus_b = "L" if e["sustain"] < sus_t1 else ("M" if e["sustain"] < sus_t2 else "H")
        middle_count = sum(1 for b in [frm_b, gap_b, sus_b] if b == "M")
        e["uncertainty_zone"] = 1 if middle_count >= 2 else 0

    return enriched, (gap_t1, gap_t2, sus_t1, sus_t2)


def _ols(y, X, labels):
    """OLS regression with standard errors and t-stats."""
    n, k = X.shape
    if n < k + 5:
        return None
    try:
        beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        y_pred = X @ beta
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        adj_r2 = 1 - (1 - r2) * (n - 1) / (n - k - 1) if n > k + 1 else r2
        mse = ss_res / (n - k) if n > k else 1
        try:
            var_beta = mse * np.linalg.inv(X.T @ X)
            se = np.sqrt(np.diag(var_beta))
            t_stats = beta / se
        except np.linalg.LinAlgError:
            se = np.zeros(k)
            t_stats = np.zeros(k)
        return {
            "n": n, "r2": float(r2), "adj_r2": float(adj_r2),
            "betas": {labels[i]: float(beta[i]) for i in range(k)},
            "t_stats": {labels[i]: float(t_stats[i]) for i in range(k)},
            "se": {labels[i]: float(se[i]) for i in range(k)},
        }
    except Exception as e:
        return {"error": str(e)}


def _print_model(result, label, var_order):
    print(f"\n  --- {label} ---", flush=True)
    if not result or "error" in result:
        print(f"    FAILED: {result.get('error', 'unknown') if result else 'no result'}", flush=True)
        return
    print(f"    N={result['n']}, R²={result['r2']:.4f}, adj R²={result['adj_r2']:.4f}", flush=True)
    print(f"    {'var':25s} {'beta':>8} {'se':>8} {'t':>6} {'sig':>5}", flush=True)
    for v in var_order:
        if v in result["betas"]:
            b = result["betas"][v]
            t = result["t_stats"][v]
            se = result["se"][v]
            sig = "***" if abs(t) > 2.58 else ("**" if abs(t) > 1.96 else ("*" if abs(t) > 1.645 else ""))
            print(f"    {v:25s} {b:8.3f} {se:8.3f} {t:6.2f} {sig:>5}", flush=True)


def run_exp9():
    enriched, thresholds = load_regression_data()
    print("=" * 70, flush=True)
    print("v3.5.1 Exp9: Alpha Attribution Regression (6-S.17.5)", flush=True)
    print("THE CRITICAL TEST for H3-A vs H3-B mechanism fork", flush=True)
    print("=" * 70, flush=True)

    gap_t1, gap_t2, sus_t1, sus_t2 = thresholds
    print(f"\nGroup A: N={len(enriched)}", flush=True)
    print(f"  Gap terciles: {gap_t1:.4f} / {gap_t2:.4f}", flush=True)
    print(f"  Sustain terciles: {sus_t1:.4f} / {sus_t2:.4f}", flush=True)
    print(f"  FRM buckets: L<{FRM_LOW_MAX}, M<{FRM_HIGH_MIN}, H>={FRM_HIGH_MIN}", flush=True)

    # Filter to rows with all regression variables
    reg_data = [e for e in enriched
                if e["frm_dir"] in ("improving", "stable")
                and e["crowd_score"] is not None
                and e["market_cap"] is not None
                and e["realized_vol"] is not None]
    print(f"  Rows with all regression vars: {len(reg_data)}", flush=True)
    unc_n = sum(1 for e in reg_data if e["uncertainty_zone"] == 1)
    print(f"  Uncertainty zone (2+ middle): {unc_n}", flush=True)

    if len(reg_data) < 20:
        print("  INSUFFICIENT N for regression", flush=True)
        return

    # Prepare regression arrays
    y = np.array([e["alpha"] * 100 for e in reg_data])  # alpha in pp

    def norm(v):
        v = np.array(v, dtype=float)
        return (v - v.min()) / (v.max() - v.min() + 1e-9)

    x_dir = np.array([1.0 if e["frm_dir"] == "improving" else 0.0 for e in reg_data])
    x_unc = np.array([float(e["uncertainty_zone"]) for e in reg_data])
    x_crowd = norm([e["crowd_score"] for e in reg_data])
    x_mcap = norm([e["market_cap"] for e in reg_data])
    x_vol = norm([e["realized_vol"] for e in reg_data])

    # ─── Model 0: Baseline (direction + controls) ───
    print("\n" + "=" * 70, flush=True)
    print("NESTED REGRESSION: Model 0 -> Model 1 -> Model 2", flush=True)
    print("=" * 70, flush=True)

    X0 = np.column_stack([np.ones(len(y)), x_dir, x_mcap, x_vol])
    m0 = _ols(y, X0, ["intercept", "frm_direction", "market_cap", "volatility"])
    _print_model(m0, "Model 0: Baseline (direction + controls)",
                 ["intercept", "frm_direction", "market_cap", "volatility"])

    # ─── Model 1: + uncertainty_zone (tests H3-A) ───
    X1 = np.column_stack([np.ones(len(y)), x_dir, x_unc, x_mcap, x_vol])
    m1 = _ols(y, X1, ["intercept", "frm_direction", "uncertainty_zone", "market_cap", "volatility"])
    _print_model(m1, "Model 1: + uncertainty_zone (tests H3-A)",
                 ["intercept", "frm_direction", "uncertainty_zone", "market_cap", "volatility"])

    # ─── Model 2: + crowding_score (tests H3-B) ───
    X2 = np.column_stack([np.ones(len(y)), x_dir, x_unc, x_crowd, x_mcap, x_vol])
    m2 = _ols(y, X2, ["intercept", "frm_direction", "uncertainty_zone", "crowding_score", "market_cap", "volatility"])
    _print_model(m2, "Model 2: + crowding_score (tests H3-B)",
                 ["intercept", "frm_direction", "uncertainty_zone", "crowding_score", "market_cap", "volatility"])

    # ─── Nested comparison ───
    print("\n" + "=" * 70, flush=True)
    print("NESTED COMPARISON (incremental R²)", flush=True)
    print("=" * 70, flush=True)
    if m0 and m1 and m2:
        delta_r2_m1 = m1["r2"] - m0["r2"]
        delta_r2_m2 = m2["r2"] - m1["r2"]
        print(f"  Model 0 R²: {m0['r2']:.4f}", flush=True)
        print(f"  Model 1 R²: {m1['r2']:.4f}  (ΔR² from uncertainty: {delta_r2_m1:+.4f})", flush=True)
        print(f"  Model 2 R²: {m2['r2']:.4f}  (ΔR² from crowding: {delta_r2_m2:+.4f})", flush=True)

    # ─── Verdict ───
    print("\n" + "=" * 70, flush=True)
    print("VERDICT SYNTHESIS", flush=True)
    print("=" * 70, flush=True)

    b_unc = m2["betas"].get("uncertainty_zone") if m2 else None
    t_unc = m2["t_stats"].get("uncertainty_zone") if m2 else None
    b_crowd = m2["betas"].get("crowding_score") if m2 else None
    t_crowd = m2["t_stats"].get("crowding_score") if m2 else None

    unc_sig = abs(t_unc) > 1.645 if t_unc is not None else False  # 10% level (small N)
    crowd_sig = abs(t_crowd) > 1.645 if t_crowd is not None else False

    print(f"\n  Model 2 coefficients:", flush=True)
    print(f"    uncertainty_zone: beta={b_unc:+.3f}, t={t_unc:.2f}, sig={'YES' if unc_sig else 'no'}", flush=True)
    print(f"    crowding_score:   beta={b_crowd:+.3f}, t={t_crowd:.2f}, sig={'YES' if crowd_sig else 'no'}", flush=True)

    if unc_sig and not crowd_sig and b_unc > 0:
        verdict = "H3-A VALIDATED: Uncertainty Asymmetry Premium"
        recommendation = "Paradigm shift to Uncertainty Asymmetry Detector. Alpha comes from information asymmetry, not crowding avoidance."
    elif not unc_sig and crowd_sig and b_crowd < 0:
        verdict = "H3-B VALIDATED: Crowding Avoidance"
        recommendation = "No paradigm shift. Add crowding filter to FRM direction gate. Alpha was from avoiding crowded trades."
    elif unc_sig and crowd_sig:
        if b_unc > 0 and b_crowd < 0:
            verdict = "MIXED: Uncertainty produces alpha, crowding suppresses it"
            recommendation = "v3.5.2 decompose further. Architecture: uncertainty zone + low crowding (2D filter)."
        elif b_unc > 0 and b_crowd > 0:
            verdict = "MIXED (unexpected): both positive. Investigate crowding score construction."
            recommendation = "v3.5.2 investigate - crowding positive is economically unusual."
        else:
            verdict = "MIXED: complex interaction. Investigate sign patterns."
            recommendation = "v3.5.2 further decomposition needed."
    elif not unc_sig and not crowd_sig:
        verdict = "INCONCLUSIVE: neither mechanism significant at 10% level"
        recommendation = "Extend N (currently small). Do NOT guess. FRM stays frozen black-box."
    else:
        verdict = "PARTIAL: one mechanism weakly significant"
        recommendation = "Inconclusive at this N. Extend data before deciding."

    print(f"\n  >>> VERDICT: {verdict}", flush=True)
    print(f"  >>> RECOMMENDATION: {recommendation}", flush=True)

    # Caveat
    print(f"\n  CAVEAT: N={len(reg_data)}, uncertainty_zone N={unc_n}.", flush=True)
    print(f"  Significance at 10% level (|t|>1.645) due to small sample.", flush=True)
    print(f"  Results are indicative, not conclusive. Extend N for confirmation.", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.17.5",
        "experiment": "exp9_attribution_regression",
        "date": str(date.today()),
        "n": len(reg_data),
        "n_uncertainty_zone": unc_n,
        "thresholds": {"gap_t1": gap_t1, "gap_t2": gap_t2,
                       "sus_t1": sus_t1, "sus_t2": sus_t2},
        "model_0_baseline": m0,
        "model_1_plus_uncertainty": m1,
        "model_2_plus_crowding": m2,
        "delta_r2_uncertainty": (m1["r2"] - m0["r2"]) if m0 and m1 else None,
        "delta_r2_crowding": (m2["r2"] - m1["r2"]) if m1 and m2 else None,
        "verdict": verdict,
        "recommendation": recommendation,
    }
    report_path = os.path.join(REPORT_DIR, f"v3_5_1_exp9_attribution_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_exp9()
