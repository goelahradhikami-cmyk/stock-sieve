"""
v3.2 A/B Attribution Comparison - Commit 6-S.13.6 Step 3/4.

Three-group comparison to isolate where v3's improvement comes from:
  Group A (v2_anomaly):     v1/v2 candidates (doctrine/anomaly, no FRM/RS)
  Group B (v3_recovery):    v3 candidates (FRM gate, no RS filtering)
  Group C (v3_recovery_rs): v3 candidates (FRM + RS, full funnel)

This answers: did improvement come from FRM (Stage 1) or RS (Stage 2)?

Gates (6-S.13.1 Design Freeze):
  Gate 1: residual_alpha > -2% (v3.2 target)
  Gate 2: positive alpha rate > 30% (avoid extreme-value distortion)
  Gate 3: beta reduction (market_beta + sector_beta should decrease)

Expected outcomes (user's framework):
  Situation A: residual_alpha > 0  -> deserved cheapness + beta trap solved
  Situation B: -2% < alpha < 0    -> direction right, Stage 3 mispricing weak
  Situation C: still ~-5%         -> Stage 1/2 only filter garbage,
                                      mispricing hypothesis itself is wrong

Usage:
    python scripts/run_v3_ab_comparison.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SHADOW_DB = "data/shadow_trading.db"
REPORT_DIR = "data/reports"


def run_comparison():
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row

    print("=" * 60, flush=True)
    print("v3.2 A/B Attribution Comparison (6-S.13.6)", flush=True)
    print("=" * 60, flush=True)

    # Group A: v2_anomaly - v1/v2 candidates (from shadow_candidates, BUY episodes)
    # Use funnel_rank <= 5 to match v3's top-5 scope
    group_a = conn.execute(
        """SELECT c.residual_alpha, c.market_beta, c.sector_beta,
                  c.stock_return_t20, c.earnings_revision_direction,
                  c.frm_score
           FROM shadow_candidates c
           JOIN shadow_episode e ON e.episode_id = c.episode_id
           WHERE e.decision='BUY' AND e.status='evaluated'
             AND c.selected=1 AND c.residual_alpha IS NOT NULL"""
    ).fetchall()

    # Group B: v3_recovery - v3 candidates where RS data NOT available
    # (Stage 1 only, pre-2024-06 or RS missing)
    group_b = conn.execute(
        """SELECT residual_alpha, market_beta, sector_beta,
                  stock_return_t20, frm_direction, rs_data_available
           FROM shadow_candidates_v3
           WHERE rs_data_available=0 AND residual_alpha IS NOT NULL"""
    ).fetchall()

    # Group C: v3_recovery_rs - v3 candidates with RS data (full funnel)
    group_c = conn.execute(
        """SELECT residual_alpha, market_beta, sector_beta,
                  stock_return_t20, frm_direction, rs_data_available
           FROM shadow_candidates_v3
           WHERE rs_data_available=1 AND residual_alpha IS NOT NULL"""
    ).fetchall()

    # Also: all v3 candidates (B + C combined) for headline number
    group_all_v3 = conn.execute(
        """SELECT residual_alpha, market_beta, sector_beta,
                  stock_return_t20, frm_direction
           FROM shadow_candidates_v3
           WHERE residual_alpha IS NOT NULL"""
    ).fetchall()

    print(f"\nGroup A (v2_anomaly, selected):     {len(group_a)} candidates", flush=True)
    print(f"Group B (v3_recovery, no RS):        {len(group_b)} candidates", flush=True)
    print(f"Group C (v3_recovery_rs, full):      {len(group_c)} candidates", flush=True)
    print(f"All v3 (B+C combined):               {len(group_all_v3)} candidates", flush=True)

    def _stats(rows, label):
        if not rows:
            print(f"\n  {label}: N=0 (no data)", flush=True)
            return None
        ra = np.array([r["residual_alpha"] for r in rows])
        mb = np.array([r["market_beta"] for r in rows if r["market_beta"] is not None])
        sb = np.array([r["sector_beta"] for r in rows if r["sector_beta"] is not None])
        sr = np.array([r["stock_return_t20"] for r in rows if r["stock_return_t20"] is not None])
        result = {
            "label": label,
            "n": len(ra),
            "ra_mean": float(np.mean(ra)),
            "ra_median": float(np.median(ra)),
            "ra_positive_rate": float(np.mean(ra > 0)),
            "mb_mean": float(np.mean(mb)) if len(mb) else None,
            "sb_mean": float(np.mean(sb)) if len(sb) else None,
            "sr_mean": float(np.mean(sr)) if len(sr) else None,
            "beta_sum": (float(np.mean(mb)) + float(np.mean(sb))) if len(mb) and len(sb) else None,
        }
        print(f"\n  {label} (N={result['n']}):", flush=True)
        print(
            f"    residual_alpha: mean={result['ra_mean']:+.4f} "
            f"median={result['ra_median']:+.4f} >0: {result['ra_positive_rate']:.1%}",
            flush=True,
        )
        print(
            f"    market_beta:    mean={result['mb_mean']:+.4f}"
            if result["mb_mean"] is not None
            else "    market_beta:    n/a",
            flush=True,
        )
        print(
            f"    sector_beta:    mean={result['sb_mean']:+.4f}"
            if result["sb_mean"] is not None
            else "    sector_beta:    n/a",
            flush=True,
        )
        print(
            f"    stock_return:   mean={result['sr_mean']:+.4f}"
            if result["sr_mean"] is not None
            else "    stock_return:   n/a",
            flush=True,
        )
        if result["beta_sum"] is not None:
            print(f"    beta_sum (mb+sb): {result['beta_sum']:+.4f}", flush=True)
        return result

    print("\n--- Three-Group Comparison ---", flush=True)
    a_stats = _stats(group_a, "Group A (v2_anomaly)")
    b_stats = _stats(group_b, "Group B (v3_recovery, no RS)")
    c_stats = _stats(group_c, "Group C (v3_recovery_rs, full)")
    v3_stats = _stats(group_all_v3, "All v3 (B+C)")

    # Gate evaluation
    print("\n--- Gate Evaluation ---", flush=True)
    gates = {}
    if v3_stats:
        gates["gate1_residual_alpha"] = {
            "target": "> -2%",
            "v2": f"{a_stats['ra_mean']:+.4f}" if a_stats else "n/a",
            "v3": f"{v3_stats['ra_mean']:+.4f}",
            "passed": v3_stats["ra_mean"] > -0.02,
        }
        gates["gate2_positive_rate"] = {
            "target": "> 30%",
            "v2": f"{a_stats['ra_positive_rate']:.1%}" if a_stats else "n/a",
            "v3": f"{v3_stats['ra_positive_rate']:.1%}",
            "passed": v3_stats["ra_positive_rate"] > 0.30,
        }
    if a_stats and v3_stats and a_stats["beta_sum"] and v3_stats["beta_sum"]:
        gates["gate3_beta_reduction"] = {
            "target": "v3 beta_sum < v2 beta_sum",
            "v2": f"{a_stats['beta_sum']:+.4f}",
            "v3": f"{v3_stats['beta_sum']:+.4f}",
            "passed": v3_stats["beta_sum"] < a_stats["beta_sum"],
        }

    for gname, g in gates.items():
        mark = "✅" if g["passed"] else "❌"
        print(f"  {mark} {gname}: v2={g['v2']} -> v3={g['v3']} (target: {g['target']})", flush=True)

    # Situation determination
    print("\n--- Situation Assessment ---", flush=True)
    if v3_stats:
        if v3_stats["ra_mean"] > 0:
            situation = "A"
            interpretation = (
                "residual_alpha > 0: deserved cheapness + "
                "beta trap SOLVED. Proceed to v3.3 validation."
            )
        elif v3_stats["ra_mean"] > -0.02:
            situation = "B"
            interpretation = (
                "-2% < alpha < 0: direction right, but Stage 3 "
                "mispricing detector is weak. Optimize Stage 3."
            )
        else:
            situation = "C"
            interpretation = (
                "alpha still < -2%: Stage 1/2 only filter "
                "garbage. Mispricing hypothesis itself may be "
                "wrong. Need to redesign anomaly detection."
            )
        print(f"  Situation {situation}: {interpretation}", flush=True)
        print(
            f"  residual_alpha: v2 {a_stats['ra_mean']:+.4f} -> "
            f"v3 {v3_stats['ra_mean']:+.4f} "
            f"(delta {v3_stats['ra_mean'] - a_stats['ra_mean']:+.4f})",
            flush=True,
        )

    # Export report
    report_path = _export_report(
        a_stats, b_stats, c_stats, v3_stats, gates, situation, interpretation
    )
    print(f"\n=== Report: {report_path} ===", flush=True)

    conn.close()
    return {"situation": situation, "gates": gates, "v3_stats": v3_stats, "v2_stats": a_stats}


def _export_report(a, b, c, v3, gates, situation, interpretation):
    today = date.today().isoformat()
    path = os.path.join(REPORT_DIR, f"v3_2_ab_comparison_{today}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    lines = []
    lines.append("# v3.2 A/B Attribution Comparison Report")
    lines.append("# Commit 6-S.13.6")
    lines.append(f"# Date: {today}")
    lines.append(f"# Situation: {situation}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Three-Group Comparison")
    lines.append("")
    lines.append(
        "| Group | N | residual_alpha mean | median | >0 rate | market_beta | sector_beta | beta_sum |"
    )
    lines.append(
        "|-------|---|---------------------|--------|---------|-------------|-------------|----------|"
    )
    for s in [a, b, c, v3]:
        if s:
            lines.append(
                f"| {s['label']} | {s['n']} | "
                f"{s['ra_mean']:+.4f} | {s['ra_median']:+.4f} | "
                f"{s['ra_positive_rate']:.1%} | "
                f"{s['mb_mean']:+.4f} | {s['sb_mean']:+.4f} | "
                f"{s['beta_sum']:+.4f} |"
            )
    lines.append("")
    lines.append("## Gate Evaluation")
    lines.append("")
    lines.append("| Gate | Target | V2 | V3 | Result |")
    lines.append("|------|--------|----|----|--------|")
    for gname, g in gates.items():
        mark = "✅ PASS" if g["passed"] else "❌ FAIL"
        lines.append(f"| {gname} | {g['target']} | {g['v2']} | {g['v3']} | {mark} |")
    lines.append("")
    lines.append("## Situation Assessment")
    lines.append("")
    lines.append(f"**Situation {situation}: {interpretation}**")
    lines.append("")
    if a and v3:
        delta = v3["ra_mean"] - a["ra_mean"]
        lines.append(
            f"residual_alpha: V2 {a['ra_mean']:+.4f} -> "
            f"V3 {v3['ra_mean']:+.4f} (delta {delta:+.4f})"
        )
        lines.append(
            f"positive rate:  V2 {a['ra_positive_rate']:.1%} -> V3 {v3['ra_positive_rate']:.1%}"
        )
        if a["beta_sum"] and v3["beta_sum"]:
            lines.append(f"beta exposure:  V2 {a['beta_sum']:+.4f} -> V3 {v3['beta_sum']:+.4f}")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    if situation == "A":
        lines.append(
            "V3 SOLVED both deserved cheapness (FRM gate) and "
            "Recovery Beta Trap (RS gate). True selection alpha "
            "is positive. Proceed to v3.3 full validation."
        )
    elif situation == "B":
        lines.append(
            "V3 improved direction (FRM) and reduced beta exposure "
            "(RS), but residual_alpha is still negative. The "
            "Stage 3 mispricing detector (anomaly) is the remaining "
            "weakness. Next: optimize Stage 3 or redesign anomaly "
            "detection."
        )
    else:
        lines.append(
            "V3's Stage 1/2 filter out garbage but do not produce "
            "alpha. The mispricing hypothesis itself may be wrong. "
            "The anomaly detector's core assumption "
            "('drawdown + cheap = mispriced') needs fundamental "
            "redesign."
        )
    lines.append("")
    lines.append("## A/B Isolation (FRM vs RS contribution)")
    lines.append("")
    if b and c:
        lines.append(f"Group B (FRM only, no RS): residual_alpha = {b['ra_mean']:+.4f}")
        lines.append(f"Group C (FRM + RS):        residual_alpha = {c['ra_mean']:+.4f}")
        delta_rs = c["ra_mean"] - b["ra_mean"]
        lines.append(f"RS contribution:           {delta_rs:+.4f}")
        if delta_rs > 0:
            lines.append("RS gate ADDS value (improves alpha).")
        else:
            lines.append("RS gate does NOT add value (or makes it worse).")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def main():
    run_comparison()


if __name__ == "__main__":
    main()
