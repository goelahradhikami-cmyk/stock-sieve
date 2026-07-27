"""
EGE Validation - Commit 6-S.15.3 (v3.3 Phase 2 validation).

Three experiments to validate the Expectation Gap Engine:

  Experiment 1: Gap Decile Test
    - Sort all v3.2.1 candidates by EGE gap_score into 10 deciles
    - Test: D10 (highest gap) residual_alpha > D1 (lowest gap)
    - This is the CORE test of EGE's discriminative power

  Experiment 2: FRM Preservation
    - Compare FRM-only (v3.2.1 Group B) vs FRM+EGE top-30%
    - Test: EGE must NOT break FRM's +2.52% alpha
    - Required: residual >= 0 OR positive_rate >= 58%

  Experiment 3: RS Ablation Regression Check
    - Verify EGE did not secretly restore RS bias
    - Within EGE top candidates, check RS distribution
    - Expected: high-gap candidates should have LOWER RS (market hasn't noticed)

Usage:
    python scripts/run_ege_validation.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.expectation_gap import ExpectationGapEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"
REPORT_DIR = "data/reports"


def run_validation():
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row
    engine = ExpectationGapEngine()

    print("=" * 60, flush=True)
    print("EGE Validation (6-S.15.3)", flush=True)
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

    # Compute EGE gap_score for each candidate
    print("Computing EGE scores...", flush=True)
    enriched = []
    for i, r in enumerate(rows):
        score = engine.compute(r["security_id"], r["trade_date"])
        enriched.append(
            {
                "row": r,
                "gap_score": score.gap_score,
                "gap_percentile": score.gap_percentile,
                "ea": score.earnings_acceleration,
                "pr": score.price_reaction,
                "frm_direction": score.frm_direction
                or (r["frm_direction"] if "frm_direction" in r else None),
                "residual_alpha": r["residual_alpha"],
                "market_beta": r["market_beta"],
                "sector_beta": r["sector_beta"],
                "rs_vs_sector": r["relative_strength"],
                "frm_score": r["frm_score"],
            }
        )
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(rows)}", flush=True)

    # Filter to those with gap_score
    valid = [e for e in enriched if e["gap_score"] is not None]
    print(f"Candidates with gap_score: {len(valid)}", flush=True)

    results = {}
    results["exp1_gap_decile"] = _exp1_gap_decile(valid)
    results["exp2_frm_preservation"] = _exp2_frm_preservation(valid, conn)
    results["exp3_rs_regression"] = _exp3_rs_regression(valid)

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
        print(
            f"  {label}: N={s['n']} mean={s['mean']:+.4f} "
            f"median={s['median']:+.4f} >0: {s['positive_rate']:.1%}",
            flush=True,
        )


# ------------------------------------------------------------------
# Experiment 1: Gap Decile Test
# ------------------------------------------------------------------


def _exp1_gap_decile(valid):
    """Sort by gap_score into 10 deciles, compare D10 vs D1."""
    print("\n--- Experiment 1: Gap Decile Test ---", flush=True)
    print("Core test: D10 (highest gap) > D1 (lowest gap)?", flush=True)

    if len(valid) < 10:
        return {"error": "insufficient data", "n": len(valid)}

    valid.sort(key=lambda x: x["gap_score"])
    n = len(valid)
    d_size = n // 10

    deciles = {}
    for d in range(10):
        start = d * d_size
        end = (d + 1) * d_size if d < 9 else n
        subset = valid[start:end]
        ra_vals = [e["residual_alpha"] for e in subset]
        s = _stats(ra_vals)
        gap_vals = [e["gap_score"] for e in subset]
        deciles[f"D{d + 1}"] = {
            "n": s["n"],
            "gap_range": [min(gap_vals), max(gap_vals)],
            "residual_mean": s["mean"],
            "residual_median": s["median"],
            "positive_rate": s["positive_rate"],
        }
        _print_stats(f"D{d + 1} (gap {min(gap_vals):+.3f} to {max(gap_vals):+.3f})", s)

    d1 = deciles["D1"]
    d10 = deciles["D10"]
    print("\n  KEY TEST: D1 (lowest gap) vs D10 (highest gap):", flush=True)
    print(
        f"    D1 residual: {d1['residual_mean']:+.4f} (positive {d1['positive_rate']:.1%})",
        flush=True,
    )
    print(
        f"    D10 residual: {d10['residual_mean']:+.4f} (positive {d10['positive_rate']:.1%})",
        flush=True,
    )

    passed = (
        d10["residual_mean"] is not None
        and d1["residual_mean"] is not None
        and d10["residual_mean"] > d1["residual_mean"]
    )
    mark = "✅" if passed else "❌"
    print(f"    {mark} Gate 1 (D10 > D1): {'PASS' if passed else 'FAIL'}", flush=True)

    # Also check monotonicity
    means = [
        deciles[f"D{d + 1}"]["residual_mean"]
        for d in range(10)
        if deciles[f"D{d + 1}"]["residual_mean"] is not None
    ]
    if len(means) == 10:
        # Count increases
        increases = sum(1 for i in range(9) if means[i + 1] > means[i])
        print(f"    Monotonicity: {increases}/9 increasing steps", flush=True)

    return {"deciles": deciles, "gate1_passed": passed}


# ------------------------------------------------------------------
# Experiment 2: FRM Preservation
# ------------------------------------------------------------------


def _exp2_frm_preservation(valid, conn):
    """Compare FRM-only baseline vs FRM+EGE top-30%."""
    print("\n--- Experiment 2: FRM Preservation ---", flush=True)
    print("Test: EGE must NOT break FRM's alpha", flush=True)

    # FRM-only baseline (v3.2.1 Group B: all FRM survivors, no RS/EGE filtering)
    frm_only = valid  # all candidates are FRM survivors by definition
    frm_stats = _stats([e["residual_alpha"] for e in frm_only])
    print(f"\n  FRM-only baseline (all {len(frm_only)} candidates):", flush=True)
    _print_stats("    all", frm_stats)

    # FRM + EGE top-30% (by gap_score)
    valid_sorted = sorted(valid, key=lambda x: -(x["gap_score"] or -999))
    top_30pct = valid_sorted[: max(1, len(valid_sorted) * 3 // 10)]
    ege_stats = _stats([e["residual_alpha"] for e in top_30pct])
    print(f"\n  FRM + EGE top-30% (N={len(top_30pct)}):", flush=True)
    _print_stats("    top-30%", ege_stats)

    # Also bottom-30% for contrast
    bottom_30pct = valid_sorted[-(max(1, len(valid_sorted) * 3 // 10)) :]
    bottom_stats = _stats([e["residual_alpha"] for e in bottom_30pct])
    print(f"\n  FRM + EGE bottom-30% (N={len(bottom_30pct)}):", flush=True)
    _print_stats("    bottom-30%", bottom_stats)

    # Gate: EGE top-30% should be >= FRM-only (or at least not much worse)
    if ege_stats["mean"] is not None and frm_stats["mean"] is not None:
        delta = ege_stats["mean"] - frm_stats["mean"]
        print(f"\n  Delta (EGE top-30% - FRM-only): {delta:+.4f}", flush=True)
        # Gate B: EGE doesn't destroy alpha
        gate_b = (
            ege_stats["mean"] >= frm_stats["mean"]
            or ege_stats["positive_rate"] >= frm_stats["positive_rate"]
        )
        mark = "✅" if gate_b else "❌"
        print(f"  {mark} Gate 2 (FRM preservation): {'PASS' if gate_b else 'FAIL'}", flush=True)
    else:
        gate_b = False

    return {
        "frm_only": frm_stats,
        "ege_top30": ege_stats,
        "ege_bottom30": bottom_stats,
        "gate2_passed": gate_b,
    }


# ------------------------------------------------------------------
# Experiment 3: RS Ablation Regression Check
# ------------------------------------------------------------------


def _exp3_rs_regression(valid):
    """Verify EGE did not secretly restore RS bias."""
    print("\n--- Experiment 3: RS Ablation Regression Check ---", flush=True)
    print("Verify: high-gap candidates should have LOWER RS", flush=True)

    valid_sorted = sorted(valid, key=lambda x: -(x["gap_score"] or -999))
    top_30 = valid_sorted[: max(1, len(valid_sorted) * 3 // 10)]
    bottom_30 = valid_sorted[-(max(1, len(valid_sorted) * 3 // 10)) :]

    top_rs = [e["rs_vs_sector"] for e in top_30 if e["rs_vs_sector"] is not None]
    bottom_rs = [e["rs_vs_sector"] for e in bottom_30 if e["rs_vs_sector"] is not None]

    if top_rs and bottom_rs:
        top_mean = float(np.mean(top_rs))
        bottom_mean = float(np.mean(bottom_rs))
        print(f"  EGE top-30% avg RS: {top_mean:+.4f} (N={len(top_rs)})", flush=True)
        print(f"  EGE bottom-30% avg RS: {bottom_mean:+.4f} (N={len(bottom_rs)})", flush=True)
        # Expected: top-gap should have LOWER RS (market hasn't noticed)
        gate_c = top_mean < bottom_mean
        mark = "✅" if gate_c else "❌"
        print(
            f"  {mark} Gate 3 (RS inversion): "
            f"{'PASS - high gap = low RS' if gate_c else 'FAIL - RS not inverted'}",
            flush=True,
        )
    else:
        gate_c = False
        print("  Insufficient RS data for comparison", flush=True)

    # Correlation: gap_score vs RS (should be negative)
    gaps = [e["gap_score"] for e in valid if e["rs_vs_sector"] is not None]
    rss = [e["rs_vs_sector"] for e in valid if e["rs_vs_sector"] is not None]
    if len(gaps) > 5:
        corr = float(np.corrcoef(gaps, rss)[0, 1])
        print(f"  Correlation gap_score vs RS: {corr:+.4f}", flush=True)
        if corr < 0:
            print("    ✅ Negative correlation: high gap = low RS (expected)", flush=True)
        else:
            print("    ⚠️ Positive correlation: EGE may be tracking RS", flush=True)
    else:
        corr = None

    return {
        "top30_rs_mean": top_mean if top_rs else None,
        "bottom30_rs_mean": bottom_mean if bottom_rs else None,
        "gap_rs_correlation": corr,
        "gate3_passed": gate_c,
    }


# ------------------------------------------------------------------
# Report export
# ------------------------------------------------------------------


def _export_report(results):
    today = date.today().isoformat()
    path = os.path.join(REPORT_DIR, f"ege_validation_{today}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    lines = []
    lines.append("# EGE Validation Report")
    lines.append("# Commit 6-S.15.3")
    lines.append(f"# Date: {today}")
    lines.append("")
    lines.append("---")
    lines.append("")

    e1 = results.get("exp1_gap_decile", {})
    lines.append("## Experiment 1: Gap Decile Test")
    lines.append("")
    if "deciles" in e1:
        lines.append("| Decile | Gap Range | N | Residual Mean | Positive Rate |")
        lines.append("|--------|-----------|---|---------------|---------------|")
        for d, data in e1["deciles"].items():
            lines.append(
                f"| {d} | [{data['gap_range'][0]:+.3f}, "
                f"{data['gap_range'][1]:+.3f}] | {data['n']} | "
                f"{data['residual_mean']:+.4f} | "
                f"{data['positive_rate']:.1%} |"
            )
        lines.append("")
        mark = "✅" if e1.get("gate1_passed") else "❌"
        lines.append(
            f"**{mark} Gate 1 (D10 > D1): {'PASS' if e1.get('gate1_passed') else 'FAIL'}**"
        )
    lines.append("")

    e2 = results.get("exp2_frm_preservation", {})
    lines.append("## Experiment 2: FRM Preservation")
    lines.append("")
    if "frm_only" in e2:
        lines.append("| Group | N | Residual Mean | Positive Rate |")
        lines.append("|-------|---|---------------|---------------|")
        for label, key in [
            ("FRM-only", "frm_only"),
            ("EGE top-30%", "ege_top30"),
            ("EGE bottom-30%", "ege_bottom30"),
        ]:
            s = e2[key]
            if s and s["mean"] is not None:
                lines.append(
                    f"| {label} | {s['n']} | {s['mean']:+.4f} | {s['positive_rate']:.1%} |"
                )
        lines.append("")
        mark = "✅" if e2.get("gate2_passed") else "❌"
        lines.append(
            f"**{mark} Gate 2 (FRM preservation): {'PASS' if e2.get('gate2_passed') else 'FAIL'}**"
        )
    lines.append("")

    e3 = results.get("exp3_rs_regression", {})
    lines.append("## Experiment 3: RS Ablation Regression Check")
    lines.append("")
    if "gap_rs_correlation" in e3 and e3["gap_rs_correlation"] is not None:
        lines.append(f"- Correlation gap_score vs RS: {e3['gap_rs_correlation']:+.4f}")
        lines.append(f"- EGE top-30% avg RS: {e3.get('top30_rs_mean', 'n/a')}")
        lines.append(f"- EGE bottom-30% avg RS: {e3.get('bottom30_rs_mean', 'n/a')}")
        mark = "✅" if e3.get("gate3_passed") else "❌"
        lines.append(
            f"\n**{mark} Gate 3 (RS inversion): {'PASS' if e3.get('gate3_passed') else 'FAIL'}**"
        )
    lines.append("")

    # Summary verdict
    g1 = e1.get("gate1_passed", False)
    g2 = e2.get("gate2_passed", False)
    g3 = e3.get("gate3_passed", False)
    lines.append("## Summary")
    lines.append("")
    lines.append("| Gate | Result |")
    lines.append("|------|--------|")
    lines.append(f"| Gate 1 (Gap discrimination) | {'✅ PASS' if g1 else '❌ FAIL'} |")
    lines.append(f"| Gate 2 (FRM preservation) | {'✅ PASS' if g2 else '❌ FAIL'} |")
    lines.append(f"| Gate 3 (RS inversion) | {'✅ PASS' if g3 else '❌ FAIL'} |")
    lines.append("")
    all_pass = g1 and g2 and g3
    if all_pass:
        lines.append(
            "**All gates PASS. EGE has discriminative power, "
            "preserves FRM alpha, and correctly inverts RS.**"
        )
    else:
        passed = sum([g1, g2, g3])
        lines.append(f"**{passed}/3 gates PASS. See analysis above.**")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def main():
    run_validation()


if __name__ == "__main__":
    main()
