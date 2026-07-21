"""
RS Ablation Study - Commit 6-S.14.2.

Investigates WHY the RS gate degrades FRM's alpha (v3.2.1 finding).
Three experiments on the 194 v3.2.1 candidates with residual_alpha:

  Experiment 1: RS quintile analysis
    - Sort all FRM-passed candidates by RS (rs_vs_sector)
    - Bin into Q1(weakest) ... Q5(strongest)
    - Compare residual_alpha across quintiles
    - Test Hypothesis A: weak RS (Phase 1, price not yet reacted) > strong RS

  Experiment 2: RS threshold sweep
    - Vary the RS gate threshold: -5%, -2%, 0%, +2%, +5%, +10%
    - For each threshold, compute mean residual_alpha of candidates passing
    - Find the optimal threshold (may not be 0)

  Experiment 3: FRM × RS 2D matrix
    - FRM score tertiles (weak/medium/strong) × RS quintiles
    - Find the highest-alpha cell
    - Test if "FRM strong + RS neutral/slightly negative" is best

  Experiment 4: Early vs Late RS (Hypothesis B)
    - Compare RS_20d (current) vs RS_60d (longer lookback)
    - Test if early RS (60d) is more predictive than late RS (20d)

  Experiment 5: Sample composition (Hypothesis D)
    - Year/market_state/industry breakdown of Group B vs Group C
    - Rule out sample selection bias

Usage:
    python scripts/run_rs_ablation_study.py
"""

from __future__ import annotations

import os
import sys
import sqlite3
from datetime import date
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SHADOW_DB = "data/shadow_trading.db"
REPORT_DIR = "data/reports"


def run_ablation():
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row

    print("=" * 60, flush=True)
    print("RS Ablation Study (6-S.14.2)", flush=True)
    print("=" * 60, flush=True)

    # Load all v3.2.1 candidates with residual_alpha
    rows = conn.execute(
        """SELECT v.*, e.market_state
           FROM shadow_candidates_v3 v
           JOIN shadow_episode e ON e.episode_id = v.episode_id
           WHERE v.residual_alpha IS NOT NULL
           ORDER BY v.id"""
    ).fetchall()
    print(f"Candidates with residual_alpha: {len(rows)}", flush=True)

    results = {}

    # --- Experiment 1: RS Quintile ---
    results["exp1_quintile"] = _exp1_rs_quintile(rows)

    # --- Experiment 2: RS Threshold Sweep ---
    results["exp2_threshold"] = _exp2_threshold_sweep(rows)

    # --- Experiment 3: FRM × RS 2D Matrix ---
    results["exp3_2d_matrix"] = _exp3_frm_rs_matrix(rows)

    # --- Experiment 4: Early vs Late RS ---
    results["exp4_early_late"] = _exp4_early_late_rs(rows, conn)

    # --- Experiment 5: Sample Composition ---
    results["exp5_composition"] = _exp5_sample_composition(rows)

    # Export report
    report_path = _export_report(results)
    print(f"\n=== Report: {report_path} ===", flush=True)

    conn.close()
    return results


def _stats(arr, label=""):
    if len(arr) == 0:
        return {"n": 0, "mean": None, "median": None, "positive_rate": None}
    a = np.array(arr)
    return {
        "n": len(a),
        "mean": float(np.mean(a)),
        "median": float(np.median(a)),
        "positive_rate": float(np.mean(a > 0)),
    }


def _print_stats(label, s):
    if s["n"] == 0:
        print(f"  {label}: N=0", flush=True)
    else:
        print(f"  {label}: N={s['n']} mean={s['mean']:+.4f} "
              f"median={s['median']:+.4f} >0: {s['positive_rate']:.1%}",
              flush=True)


# ------------------------------------------------------------------
# Experiment 1: RS Quintile
# ------------------------------------------------------------------

def _exp1_rs_quintile(rows):
    """Sort by rs_vs_sector, bin into 5 quintiles, compare residual_alpha."""
    print("\n--- Experiment 1: RS Quintile Analysis ---", flush=True)
    print("Test Hypothesis A: weak RS (price not yet reacted) > strong RS",
          flush=True)

    # Use relative_strength (rs_vs_sector) as the RS metric
    valid = [r for r in rows if r["relative_strength"] is not None]
    print(f"Candidates with RS data: {len(valid)}", flush=True)

    if len(valid) < 5:
        return {"error": "insufficient data"}

    # Sort by RS ascending (Q1 = weakest)
    valid.sort(key=lambda r: r["relative_strength"])
    n = len(valid)
    q_size = n // 5

    quintiles = {}
    for q in range(5):
        start = q * q_size
        end = (q + 1) * q_size if q < 4 else n
        subset = valid[start:end]
        rs_vals = [r["relative_strength"] for r in subset]
        ra_vals = [r["residual_alpha"] for r in subset]
        s = _stats(ra_vals)
        quintiles[f"Q{q+1}"] = {
            "n": s["n"],
            "rs_range": [min(rs_vals), max(rs_vals)],
            "rs_mean": float(np.mean(rs_vals)),
            "residual_mean": s["mean"],
            "residual_median": s["median"],
            "positive_rate": s["positive_rate"],
        }
        _print_stats(f"Q{q+1} (RS {min(rs_vals):+.4f} to {max(rs_vals):+.4f})",
                     s)

    # Key test: Q1 (weakest RS) vs Q5 (strongest RS)
    q1 = quintiles["Q1"]
    q5 = quintiles["Q5"]
    print(f"\n  KEY TEST: Q1 (weakest RS) vs Q5 (strongest RS):", flush=True)
    print(f"    Q1 residual: {q1['residual_mean']:+.4f} "
          f"(positive {q1['positive_rate']:.1%})", flush=True)
    print(f"    Q5 residual: {q5['residual_mean']:+.4f} "
          f"(positive {q5['positive_rate']:.1%})", flush=True)

    if q1["residual_mean"] is not None and q5["residual_mean"] is not None:
        if q1["residual_mean"] > q5["residual_mean"]:
            print(f"    ✅ Hypothesis A SUPPORTED: weak RS > strong RS", flush=True)
            verdict = "SUPPORTED"
        else:
            print(f"    ❌ Hypothesis A NOT supported: strong RS >= weak RS",
                  flush=True)
            verdict = "NOT_SUPPORTED"
    else:
        verdict = "INSUFFICIENT_DATA"

    return {"quintiles": quintiles, "hypothesis_a_verdict": verdict}


# ------------------------------------------------------------------
# Experiment 2: RS Threshold Sweep
# ------------------------------------------------------------------

def _exp2_threshold_sweep(rows):
    """Vary RS gate threshold, find optimal."""
    print("\n--- Experiment 2: RS Threshold Sweep ---", flush=True)

    valid = [r for r in rows if r["relative_strength"] is not None]
    thresholds = [-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10]

    sweep = {}
    for t in thresholds:
        if t == -0.10:
            # All candidates (no gate)
            subset = valid
        else:
            # Candidates with RS > t (current gate logic)
            subset = [r for r in valid if r["relative_strength"] > t]
        ra_vals = [r["residual_alpha"] for r in subset]
        s = _stats(ra_vals)
        sweep[f"rs_gt_{t:.2f}"] = {
            "threshold": t,
            "n": s["n"],
            "residual_mean": s["mean"],
            "positive_rate": s["positive_rate"],
        }
        _print_stats(f"RS > {t:+.2f}", s)

    # Find optimal (highest residual_mean)
    best = max(sweep.values(), key=lambda x: x["residual_mean"] or -999)
    print(f"\n  OPTIMAL threshold: RS > {best['threshold']:+.2f} "
          f"(residual {best['residual_mean']:+.4f}, N={best['n']})",
          flush=True)

    # Compare no-gate vs current gate (RS > 0)
    no_gate = sweep["rs_gt_-0.10"]
    current = sweep["rs_gt_0.00"]
    if no_gate["residual_mean"] and current["residual_mean"]:
        delta = no_gate["residual_mean"] - current["residual_mean"]
        print(f"  No-gate vs RS>0: {no_gate['residual_mean']:+.4f} vs "
              f"{current['residual_mean']:+.4f} (delta {delta:+.4f})", flush=True)
        if delta > 0:
            print(f"    ✅ Removing RS gate IMPROVES alpha", flush=True)
        else:
            print(f"    RS gate helps (alpha higher with gate)", flush=True)

    return {"sweep": sweep, "optimal_threshold": best}


# ------------------------------------------------------------------
# Experiment 3: FRM × RS 2D Matrix
# ------------------------------------------------------------------

def _exp3_frm_rs_matrix(rows):
    """FRM score tertiles × RS quintiles 2D matrix."""
    print("\n--- Experiment 3: FRM × RS 2D Matrix ---", flush=True)

    valid = [r for r in rows
             if r["relative_strength"] is not None
             and r["frm_score"] is not None]
    print(f"Candidates with both FRM and RS: {len(valid)}", flush=True)

    if len(valid) < 9:
        return {"error": "insufficient data"}

    # FRM tertiles
    frm_vals = sorted([r["frm_score"] for r in valid])
    frm_t1 = frm_vals[len(frm_vals) // 3]
    frm_t2 = frm_vals[2 * len(frm_vals) // 3]

    # RS quintiles
    rs_vals = sorted([r["relative_strength"] for r in valid])
    q_size = len(rs_vals) // 5
    q_thresholds = [rs_vals[(i + 1) * q_size] for i in range(4)]

    def _frm_bucket(frm):
        if frm < frm_t1:
            return "weak"
        elif frm < frm_t2:
            return "medium"
        else:
            return "strong"

    def _rs_bucket(rs):
        for i, t in enumerate(q_thresholds):
            if rs <= t:
                return f"Q{i+1}"
        return "Q5"

    matrix = defaultdict(lambda: {"n": 0, "residuals": []})
    for r in valid:
        fb = _frm_bucket(r["frm_score"])
        rb = _rs_bucket(r["relative_strength"])
        key = f"{fb}_{rb}"
        matrix[key]["n"] += 1
        matrix[key]["residuals"].append(r["residual_alpha"])

    print(f"\n  FRM tertile thresholds: weak<{frm_t1:.1f}, "
          f"medium<{frm_t2:.1f}, strong>={frm_t2:.1f}", flush=True)
    print(f"\n  Residual Alpha Matrix (mean):", flush=True)
    print(f"    {'':10s} {'Q1(weak RS)':>14s} {'Q2':>14s} {'Q3':>14s} "
          f"{'Q4':>14s} {'Q5(strong)':>14s}", flush=True)
    for fb in ["weak", "medium", "strong"]:
        row_str = f"    {fb:10s}"
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            key = f"{fb}_{q}"
            cell = matrix[key]
            if cell["n"] > 0:
                mean_ra = float(np.mean(cell["residuals"]))
                row_str += f" {mean_ra:+10.4f}(N={cell['n']:2d})"
            else:
                row_str += f" {'--':>14s}"
        print(row_str, flush=True)

    # Find best cell
    best_cell = None
    best_mean = -999
    for key, cell in matrix.items():
        if cell["n"] >= 3:  # min sample
            m = float(np.mean(cell["residuals"]))
            if m > best_mean:
                best_mean = m
                best_cell = key
    if best_cell:
        print(f"\n  BEST CELL: {best_cell} (residual {best_mean:+.4f})",
              flush=True)

    return {
        "matrix": {k: {"n": v["n"],
                       "residual_mean": float(np.mean(v["residuals"]))
                       if v["n"] > 0 else None}
                   for k, v in matrix.items()},
        "best_cell": best_cell,
        "best_residual": best_mean if best_cell else None,
    }


# ------------------------------------------------------------------
# Experiment 4: Early vs Late RS
# ------------------------------------------------------------------

def _exp4_early_late_rs(rows, conn):
    """Compare RS_20d vs RS_60d. Test if early RS is more predictive."""
    print("\n--- Experiment 4: Early vs Late RS ---", flush=True)
    print("Test Hypothesis B: RS_60d (early) more predictive than RS_20d (late)",
          flush=True)

    # Current RS is 20d lookback. We need to compute 60d RS.
    # Use the existing SectorConfirmationScorer with lookback_days=60
    from src.thesis.sector_confirmation import SectorConfirmationScorer
    scorer = SectorConfirmationScorer()

    # Sample up to 50 candidates (RS_60d computation is expensive)
    sample = [r for r in rows if r["relative_strength"] is not None][:50]
    print(f"Computing RS_60d for {len(sample)} candidates...", flush=True)

    results = []
    for i, r in enumerate(sample):
        try:
            rs60 = scorer.compute(r["security_id"], r["trade_date"],
                                  lookback_days=60)
            results.append({
                "rs_20d": r["relative_strength"],
                "rs_60d": rs60.rs_vs_sector,
                "residual_alpha": r["residual_alpha"],
                "frm_score": r["frm_score"],
            })
        except Exception:
            pass
        if (i + 1) % 20 == 0:
            print(f"  ... {i+1}/{len(sample)}", flush=True)

    if len(results) < 10:
        return {"error": "insufficient data", "n": len(results)}

    # Correlation analysis
    rs20 = np.array([r["rs_20d"] for r in results if r["rs_20d"] is not None])
    rs60 = np.array([r["rs_60d"] for r in results if r["rs_60d"] is not None
                     and r["rs_20d"] is not None])
    ra = np.array([r["residual_alpha"] for r in results if r["rs_20d"] is not None])

    # Align
    aligned = [(r["rs_20d"], r["rs_60d"], r["residual_alpha"])
               for r in results
               if r["rs_20d"] is not None and r["rs_60d"] is not None]
    if len(aligned) < 10:
        return {"error": "insufficient aligned data", "n": len(aligned)}

    rs20_a = np.array([a[0] for a in aligned])
    rs60_a = np.array([a[1] for a in aligned])
    ra_a = np.array([a[2] for a in aligned])

    corr_20 = float(np.corrcoef(rs20_a, ra_a)[0, 1]) if len(rs20_a) > 2 else None
    corr_60 = float(np.corrcoef(rs60_a, ra_a)[0, 1]) if len(rs60_a) > 2 else None

    print(f"  N={len(aligned)}", flush=True)
    print(f"  Correlation RS_20d vs residual_alpha: {corr_20:+.4f}", flush=True)
    print(f"  Correlation RS_60d vs residual_alpha: {corr_60:+.4f}", flush=True)

    # Quintile comparison for RS_60d
    sorted_idx = np.argsort(rs60_a)
    n = len(sorted_idx)
    q_size = n // 5
    print(f"\n  RS_60d Quintiles:", flush=True)
    for q in range(5):
        start = q * q_size
        end = (q + 1) * q_size if q < 4 else n
        subset_ra = ra_a[sorted_idx[start:end]]
        subset_rs = rs60_a[sorted_idx[start:end]]
        if len(subset_ra) > 0:
            print(f"    Q{q+1} (RS60 {subset_rs.min():+.4f} to "
                  f"{subset_rs.max():+.4f}): residual {np.mean(subset_ra):+.4f} "
                  f"(N={len(subset_ra)})", flush=True)

    verdict = "INSUFFICIENT"
    if corr_20 is not None and corr_60 is not None:
        if abs(corr_60) > abs(corr_20) and corr_60 > 0:
            verdict = "RS_60d_MORE_PREDICTIVE"
        elif corr_20 < 0:
            verdict = "RS_20d_NEGATIVE_CORRELATION"

    return {
        "n": len(aligned),
        "corr_rs20_residual": corr_20,
        "corr_rs60_residual": corr_60,
        "verdict": verdict,
    }


# ------------------------------------------------------------------
# Experiment 5: Sample Composition
# ------------------------------------------------------------------

def _exp5_sample_composition(rows):
    """Rule out sample selection bias (Hypothesis D)."""
    print("\n--- Experiment 5: Sample Composition ---", flush=True)
    print("Test Hypothesis D: Group B vs C differ by year/state/industry",
          flush=True)

    group_b = [r for r in rows if r["rs_data_available"] == 0]
    group_c = [r for r in rows if r["rs_data_available"] == 1]

    print(f"  Group B (no RS data): {len(group_b)}", flush=True)
    print(f"  Group C (with RS):    {len(group_c)}", flush=True)

    # By year
    print(f"\n  By Year:", flush=True)
    for year in sorted(set(r["trade_date"][:4] for r in rows)):
        b = [r for r in group_b if r["trade_date"][:4] == year]
        c = [r for r in group_c if r["trade_date"][:4] == year]
        b_ra = np.mean([r["residual_alpha"] for r in b]) if b else None
        c_ra = np.mean([r["residual_alpha"] for r in c]) if c else None
        b_str = f"{b_ra:+.4f}(N={len(b)})" if b_ra is not None else "n/a"
        c_str = f"{c_ra:+.4f}(N={len(c)})" if c_ra is not None else "n/a"
        print(f"    {year}: B={b_str:20s}  C={c_str:20s}", flush=True)

    # By market_state
    print(f"\n  By Market State:", flush=True)
    for state in sorted(set(r["market_state"] for r in rows)):
        b = [r for r in group_b if r["market_state"] == state]
        c = [r for r in group_c if r["market_state"] == state]
        b_ra = np.mean([r["residual_alpha"] for r in b]) if b else None
        c_ra = np.mean([r["residual_alpha"] for r in c]) if c else None
        b_str = f"{b_ra:+.4f}(N={len(b)})" if b_ra is not None else "n/a"
        c_str = f"{c_ra:+.4f}(N={len(c)})" if c_ra is not None else "n/a"
        print(f"    {state:22s}: B={b_str:20s}  C={c_str:20s}", flush=True)

    # By FRM score bucket
    print(f"\n  By FRM Score Bucket:", flush=True)
    for lo, hi, label in [(0, 50, "weak(0-50)"), (50, 70, "med(50-70)"),
                           (70, 100, "strong(70+)")]:
        b = [r for r in group_b if r["frm_score"] is not None
             and lo <= r["frm_score"] < hi]
        c = [r for r in group_c if r["frm_score"] is not None
             and lo <= r["frm_score"] < hi]
        b_ra = np.mean([r["residual_alpha"] for r in b]) if b else None
        c_ra = np.mean([r["residual_alpha"] for r in c]) if c else None
        b_str = f"{b_ra:+.4f}(N={len(b)})" if b_ra is not None else "n/a"
        c_str = f"{c_ra:+.4f}(N={len(c)})" if c_ra is not None else "n/a"
        print(f"    {label:14s}: B={b_str:20s}  C={c_str:20s}", flush=True)

    # Key question: within the SAME FRM bucket, does B still beat C?
    print(f"\n  KEY TEST: Same FRM bucket, B vs C:", flush=True)
    verdict = "INSUFFICIENT"
    for lo, hi, label in [(50, 70, "med"), (70, 100, "strong")]:
        b = [r for r in group_b if r["frm_score"] is not None
             and lo <= r["frm_score"] < hi]
        c = [r for r in group_c if r["frm_score"] is not None
             and lo <= r["frm_score"] < hi]
        if len(b) >= 3 and len(c) >= 3:
            b_ra = np.mean([r["residual_alpha"] for r in b])
            c_ra = np.mean([r["residual_alpha"] for r in c])
            delta = b_ra - c_ra
            print(f"    {label}: B={b_ra:+.4f}(N={len(b)}) "
                  f"C={c_ra:+.4f}(N={len(c)}) delta={delta:+.4f}", flush=True)
            if delta > 0:
                verdict = "B_BEATS_C_WITHIN_FRM_BUCKET"

    if verdict == "B_BEATS_C_WITHIN_FRM_BUCKET":
        print(f"\n  ✅ Hypothesis D RULED OUT: B beats C even within same "
              f"FRM bucket -> not sample bias", flush=True)
    else:
        print(f"\n  ⚠️ Cannot fully rule out sample bias", flush=True)

    return {"verdict": verdict}


# ------------------------------------------------------------------
# Report export
# ------------------------------------------------------------------

def _export_report(results):
    today = date.today().isoformat()
    path = os.path.join(REPORT_DIR, f"rs_ablation_study_{today}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    lines = []
    lines.append("# RS Ablation Study Report")
    lines.append("# Commit 6-S.14.2")
    lines.append(f"# Date: {today}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Exp 1
    e1 = results.get("exp1_quintile", {})
    lines.append("## Experiment 1: RS Quintile Analysis")
    lines.append("")
    lines.append("Test: does weak RS (price not yet reacted) produce higher "
                 "alpha than strong RS?")
    lines.append("")
    if "quintiles" in e1:
        lines.append("| Quintile | RS Range | N | Residual Mean | "
                     "Positive Rate |")
        lines.append("|----------|----------|---|---------------|---------------|")
        for q, data in e1["quintiles"].items():
            lines.append(f"| {q} | [{data['rs_range'][0]:+.4f}, "
                         f"{data['rs_range'][1]:+.4f}] | {data['n']} | "
                         f"{data['residual_mean']:+.4f} | "
                         f"{data['positive_rate']:.1%} |")
        lines.append("")
        lines.append(f"**Hypothesis A verdict: {e1.get('hypothesis_a_verdict', 'N/A')}**")
    lines.append("")

    # Exp 2
    e2 = results.get("exp2_threshold", {})
    lines.append("## Experiment 2: RS Threshold Sweep")
    lines.append("")
    if "sweep" in e2:
        lines.append("| Threshold | N | Residual Mean | Positive Rate |")
        lines.append("|-----------|---|---------------|---------------|")
        for key, data in e2["sweep"].items():
            lines.append(f"| RS > {data['threshold']:+.2f} | {data['n']} | "
                         f"{data['residual_mean']:+.4f} | "
                         f"{data['positive_rate']:.1%} |")
        if "optimal_threshold" in e2:
            opt = e2["optimal_threshold"]
            lines.append(f"\n**Optimal: RS > {opt['threshold']:+.2f} "
                         f"(residual {opt['residual_mean']:+.4f})**")
    lines.append("")

    # Exp 3
    e3 = results.get("exp3_2d_matrix", {})
    lines.append("## Experiment 3: FRM × RS 2D Matrix")
    lines.append("")
    if "best_cell" in e3 and e3["best_cell"]:
        lines.append(f"**Best cell: {e3['best_cell']} "
                     f"(residual {e3['best_residual']:+.4f})**")
    lines.append("")

    # Exp 4
    e4 = results.get("exp4_early_late", {})
    lines.append("## Experiment 4: Early vs Late RS")
    lines.append("")
    if "corr_rs20_residual" in e4:
        lines.append(f"- Correlation RS_20d vs residual: "
                     f"{e4['corr_rs20_residual']:+.4f}")
        lines.append(f"- Correlation RS_60d vs residual: "
                     f"{e4['corr_rs60_residual']:+.4f}")
        lines.append(f"- Verdict: {e4.get('verdict', 'N/A')}")
    lines.append("")

    # Exp 5
    e5 = results.get("exp5_composition", {})
    lines.append("## Experiment 5: Sample Composition")
    lines.append("")
    lines.append(f"Verdict: {e5.get('verdict', 'N/A')}")
    lines.append("")

    lines.append("## Conclusion")
    lines.append("")
    lines.append("See stdout for full analysis. Key finding determines "
                 "whether RS gate should be removed, softened, or reversed.")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def main():
    run_ablation()


if __name__ == "__main__":
    main()
