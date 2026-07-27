"""
Phase 1.5 Sustainability Ablation - Commit 6-S.16.2 (v3.4 Phase 1.5).

THE LIFE-OR-DEATH EXPERIMENT for v3.4.

Answers ONE question: does the market reward 'earnings improvement +
credibility'? (NOT 'is EQE effective?')

This isolates sustainability's contribution INDEPENDENT of gap. If we
combined gap + sustainability now, we'd repeat v3.3's mistake: binding
two variables together and not knowing which produced alpha.

Experimental design:
  Group A (FRM-only baseline): v3 candidates, Stage 1 FRM pass, no
                               sustainability filter.
                               = v3.2.1 Group B (rs_data_available=0)
                               baseline: +2.52% alpha, 58.3% positive
  Group B (FRM + Sustainability): Group A AND sustainability_pass = 1

  Baseline is FRM-only (v3.2.1 Group B +2.52%), NOT EGE. EGE is already
  falsified by v3.3 and does not qualify as a baseline.

Three Gates (frozen 6-S.16.0a):
  Gate B1 (Alpha lift):     Group B alpha - Group A alpha >= +1.5pp
  Gate B2 (Hit rate lift):  positive_rate(B) - positive_rate(A) >= +5pp
  Gate B3 (Not just risk):  alpha_delta > 0 AND return dispersion does
                            NOT collapse to ~0 (avoids mistaking a pure
                            risk filter for alpha)

Quintile diagnostic (distinguishes alpha factor vs garbage filter):
  Sort Group A by sustainability_pass (binary) AND by a continuous
  proxy (profit_elasticity, accel_trend, company_margin_zscore) into
  quintiles. Plot residual_alpha per quintile.
    Case A (monotonic):     sustainability is an alpha FACTOR
    Case B (step function): sustainability is only a GARBAGE FILTER
    Case C (random):        market does not care about earnings quality

Decision rule (frozen):
  PASS (all 3 gates) -> Phase 2 EQE Composer
  FAIL (any gate)    -> STOP. Do not tune parameters. Freeze decision:
                        FRM alpha comes from recovery itself, not
                        recovery quality. Re-examine why FRM has alpha.

Usage:
    python scripts/run_v3_4_sustainability_ablation.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"
REPORT_DIR = "data/reports"

# Frozen gate thresholds (6-S.16.0a)
GATE_B1_ALPHA_LIFT_PP = 1.5  # +1.5pp
GATE_B2_HITRATE_LIFT_PP = 5.0  # +5pp
GATE_B3_DISPERSION_FLOOR = 0.005  # return std must stay above 0.5%
MIN_N_FOR_STATISTICAL_POWER = 30


def load_candidate_rows():
    """Load v3 candidates with residual_alpha, joined to sustainability.

    Returns list of dicts with attribution + sustainability fields.
    Group A (FRM-only) = rs_data_available=0 (v3.2.1 Group B definition).
    """
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    cache = sqlite3.connect(CACHE_DB)
    cache.row_factory = sqlite3.Row

    # All v3 candidates with residual_alpha (v3.2.1 universe, N=194)
    cand_rows = shadow.execute(
        """SELECT v.security_id, v.trade_date, v.episode_id,
                  v.residual_alpha, v.market_beta, v.sector_beta,
                  v.stock_return_t20, v.rs_data_available,
                  v.frm_direction, v.frm_score, v.earnings_acceleration
           FROM shadow_candidates_v3 v
           WHERE v.residual_alpha IS NOT NULL
           ORDER BY v.id"""
    ).fetchall()

    # Sustainability lookup: (security_id, as_of_date) -> row
    sustain_map = {}
    for r in cache.execute("""SELECT * FROM earnings_sustainability""").fetchall():
        sustain_map[(r["security_id"], r["as_of_date"])] = dict(r)

    shadow.close()
    cache.close()

    # Merge
    merged = []
    for c in cand_rows:
        key = (c["security_id"], c["trade_date"])
        s = sustain_map.get(key, {})
        merged.append(
            {
                "security_id": c["security_id"],
                "trade_date": c["trade_date"],
                "episode_id": c["episode_id"],
                "residual_alpha": c["residual_alpha"],
                "market_beta": c["market_beta"],
                "sector_beta": c["sector_beta"],
                "stock_return_t20": c["stock_return_t20"],
                "rs_data_available": c["rs_data_available"],
                "frm_direction": c["frm_direction"],
                "frm_score": c["frm_score"],
                # Sustainability fields (None if not covered)
                "sustainability_pass": s.get("sustainability_pass"),
                "alignment_flag": s.get("alignment_flag"),
                "consistency_flag": s.get("consistency_flag"),
                "margin_normalization_flag": s.get("margin_normalization_flag"),
                "failure_reason": s.get("failure_reason"),
                "profit_elasticity": s.get("profit_elasticity"),
                "accel_trend": s.get("accel_trend"),
                "accel_volatility": s.get("accel_volatility"),
                "reversal_count": s.get("reversal_count"),
                "company_margin_zscore": s.get("company_margin_zscore"),
                "industry_margin_zscore": s.get("industry_margin_zscore"),
                "operating_margin_current": s.get("operating_margin_current"),
            }
        )
    return merged


def stats(rows, label):
    """Compute residual_alpha stats for a group."""
    if not rows:
        return {"label": label, "n": 0}
    ra = np.array([r["residual_alpha"] for r in rows if r["residual_alpha"] is not None])
    mb = np.array([r["market_beta"] for r in rows if r["market_beta"] is not None])
    sb = np.array([r["sector_beta"] for r in rows if r["sector_beta"] is not None])
    ret = np.array([r["stock_return_t20"] for r in rows if r["stock_return_t20"] is not None])
    pos = int(sum(1 for x in ra if x > 0))
    return {
        "label": label,
        "n": len(rows),
        "residual_alpha_mean_pct": float(np.mean(ra) * 100) if len(ra) else None,
        "residual_alpha_median_pct": float(np.median(ra) * 100) if len(ra) else None,
        "positive_rate_pct": float(100.0 * pos / len(ra)) if len(ra) else None,
        "market_beta_pct": float(np.mean(mb) * 100) if len(mb) else None,
        "sector_beta_pct": float(np.mean(sb) * 100) if len(sb) else None,
        "return_mean_pct": float(np.mean(ret) * 100) if len(ret) else None,
        "return_std_pct": float(np.std(ret) * 100) if len(ret) else None,
    }


def print_stats(s, indent=2):
    pad = " " * indent
    if s["n"] == 0:
        print(f"{pad}{s['label']}: EMPTY", flush=True)
        return
    print(f"{pad}{s['label']}: N={s['n']}", flush=True)
    print(
        f"{pad}  residual_alpha: mean={s['residual_alpha_mean_pct']:.2f}%  "
        f"median={s['residual_alpha_median_pct']:.2f}%  "
        f"positive_rate={s['positive_rate_pct']:.1f}%",
        flush=True,
    )
    print(
        f"{pad}  market_beta={s['market_beta_pct']:.2f}%  sector_beta={s['sector_beta_pct']:.2f}%",
        flush=True,
    )
    print(
        f"{pad}  return: mean={s['return_mean_pct']:.2f}%  std={s['return_std_pct']:.2f}%",
        flush=True,
    )


def run_ablation():
    rows = load_candidate_rows()
    print("=" * 70, flush=True)
    print("PHASE 1.5: Sustainability Ablation (6-S.16.2)", flush=True)
    print("THE LIFE-OR-DEATH EXPERIMENT for v3.4", flush=True)
    print("=" * 70, flush=True)
    print(f"\nTotal v3 candidates with residual_alpha: {len(rows)}", flush=True)

    # ─────────────────────────────────────────────────────────────
    # Group A: FRM-only baseline (v3.2.1 Group B, rs_data_available=0)
    # ─────────────────────────────────────────────────────────────
    group_a = [r for r in rows if r["rs_data_available"] == 0]
    stats_a = stats(group_a, "Group A (FRM-only baseline)")

    print("\n" + "-" * 70, flush=True)
    print("Group A: FRM-only baseline (v3.2.1 Group B)", flush=True)
    print("-" * 70, flush=True)
    print_stats(stats_a)

    # ─────────────────────────────────────────────────────────────
    # Group B: FRM + Sustainability (subset of Group A with pass=1)
    # ─────────────────────────────────────────────────────────────
    group_a_covered = [r for r in group_a if r["sustainability_pass"] is not None]
    group_b = [r for r in group_a if r["sustainability_pass"] == 1]
    group_b_fail = [r for r in group_a if r["sustainability_pass"] == 0]
    stats_b = stats(group_b, "Group B (FRM + Sustainability)")

    print("\n" + "-" * 70, flush=True)
    print("Group B: FRM + Sustainability (subset of Group A, pass=1)", flush=True)
    print("-" * 70, flush=True)
    print(f"  Group A total:          {len(group_a)}", flush=True)
    print(
        f"  Group A covered:        {len(group_a_covered)} "
        f"({100.0 * len(group_a_covered) / max(len(group_a), 1):.1f}%)",
        flush=True,
    )
    print(f"  Group B (pass=1):       {len(group_b)}", flush=True)
    print(f"  Group A fail (pass=0):  {len(group_b_fail)}", flush=True)
    print_stats(stats_b)

    # ─────────────────────────────────────────────────────────────
    # Gates evaluation
    # ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 70, flush=True)
    print("GATE EVALUATION", flush=True)
    print("=" * 70, flush=True)

    gates = {}

    # Gate B1: Alpha lift >= +1.5pp
    if stats_a["n"] > 0 and stats_b["n"] > 0:
        alpha_lift = stats_b["residual_alpha_mean_pct"] - stats_a["residual_alpha_mean_pct"]
        gates["B1_alpha_lift"] = {
            "required": f">= +{GATE_B1_ALPHA_LIFT_PP}pp",
            "actual": f"{alpha_lift:+.2f}pp",
            "group_a_alpha": stats_a["residual_alpha_mean_pct"],
            "group_b_alpha": stats_b["residual_alpha_mean_pct"],
            "verdict": "PASS" if alpha_lift >= GATE_B1_ALPHA_LIFT_PP else "FAIL",
        }
        print("\n  Gate B1 (Alpha lift):", flush=True)
        print(f"    required: {gates['B1_alpha_lift']['required']}", flush=True)
        print(
            f"    actual:   {gates['B1_alpha_lift']['actual']}  "
            f"(A={stats_a['residual_alpha_mean_pct']:.2f}% -> "
            f"B={stats_b['residual_alpha_mean_pct']:.2f}%)",
            flush=True,
        )
        print(f"    verdict:  {gates['B1_alpha_lift']['verdict']}", flush=True)

    # Gate B2: Hit rate lift >= +5pp
    if stats_a["n"] > 0 and stats_b["n"] > 0:
        hitrate_lift = stats_b["positive_rate_pct"] - stats_a["positive_rate_pct"]
        gates["B2_hitrate_lift"] = {
            "required": f">= +{GATE_B2_HITRATE_LIFT_PP}pp",
            "actual": f"{hitrate_lift:+.2f}pp",
            "group_a_positive_rate": stats_a["positive_rate_pct"],
            "group_b_positive_rate": stats_b["positive_rate_pct"],
            "verdict": "PASS" if hitrate_lift >= GATE_B2_HITRATE_LIFT_PP else "FAIL",
        }
        print("\n  Gate B2 (Hit rate lift):", flush=True)
        print(f"    required: {gates['B2_hitrate_lift']['required']}", flush=True)
        print(
            f"    actual:   {gates['B2_hitrate_lift']['actual']}  "
            f"(A={stats_a['positive_rate_pct']:.1f}% -> "
            f"B={stats_b['positive_rate_pct']:.1f}%)",
            flush=True,
        )
        print(f"    verdict:  {gates['B2_hitrate_lift']['verdict']}", flush=True)

    # Gate B3: Not just risk filter (alpha_delta > 0 AND dispersion preserved)
    if stats_a["n"] > 0 and stats_b["n"] > 0:
        alpha_delta = stats_b["residual_alpha_mean_pct"] - stats_a["residual_alpha_mean_pct"]
        dispersion_b = stats_b["return_std_pct"]
        dispersion_preserved = dispersion_b >= GATE_B3_DISPERSION_FLOOR * 100
        gates["B3_not_just_risk"] = {
            "required": f"alpha_delta > 0 AND return_std >= {GATE_B3_DISPERSION_FLOOR * 100}%",
            "actual_alpha_delta": f"{alpha_delta:+.2f}pp",
            "actual_return_std": f"{dispersion_b:.2f}%",
            "verdict": "PASS" if (alpha_delta > 0 and dispersion_preserved) else "FAIL",
        }
        print("\n  Gate B3 (Not just risk filter):", flush=True)
        print(f"    required: {gates['B3_not_just_risk']['required']}", flush=True)
        print(
            f"    actual:   alpha_delta={gates['B3_not_just_risk']['actual_alpha_delta']}, "
            f"return_std={gates['B3_not_just_risk']['actual_return_std']}",
            flush=True,
        )
        print(f"    verdict:  {gates['B3_not_just_risk']['verdict']}", flush=True)

    # ─────────────────────────────────────────────────────────────
    # Quintile diagnostic (alpha factor vs garbage filter vs noise)
    # ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 70, flush=True)
    print("QUINTILE DIAGNOSTIC (alpha factor vs garbage filter vs noise)", flush=True)
    print("=" * 70, flush=True)
    print("Sort Group A by composite sustainability proxy, bin into quintiles.", flush=True)

    # Composite proxy: average of available normalized raw signals
    # Higher = more sustainable
    def sustain_proxy(r):
        vals = []
        # alignment: profit_elasticity (cap extreme), higher better but bounded
        if r["profit_elasticity"] is not None and abs(r["profit_elasticity"]) < 20:
            vals.append(("alignment", min(r["profit_elasticity"], 5.0) / 5.0))
        # persistence: accel_trend, higher better
        if r["accel_trend"] is not None:
            vals.append(("persistence", max(-1, min(1, r["accel_trend"]))))
        # margin: company_zscore, lower (not at peak) better -> invert
        if r["company_margin_zscore"] is not None:
            vals.append(("margin", max(-1, min(1, -r["company_margin_zscore"] / 3.0))))
        if not vals:
            return None
        return float(np.mean([v for _, v in vals]))

    # Only rows with sustainability data
    rows_with_proxy = [(r, sustain_proxy(r)) for r in group_a_covered]
    rows_with_proxy = [(r, p) for r, p in rows_with_proxy if p is not None]

    if len(rows_with_proxy) >= 10:
        # Sort by proxy ascending, split into 5 quintiles
        rows_with_proxy.sort(key=lambda x: x[1])
        n = len(rows_with_proxy)
        q_size = n // 5
        quintiles = {}
        print(f"\n  N with composite proxy: {n}\n", flush=True)
        print(
            f"  {'quintile':10s} {'n':>4} {'proxy':>8} {'alpha':>8} "
            f"{'positive':>9} {'sustain_pass%':>14}",
            flush=True,
        )
        for qi in range(5):
            start = qi * q_size
            end = (qi + 1) * q_size if qi < 4 else n
            q_rows = [r for r, _ in rows_with_proxy[start:end]]
            q_proxy = np.mean([p for _, p in rows_with_proxy[start:end]])
            s = stats(q_rows, f"Q{qi + 1}")
            pass_rate = (
                100.0
                * sum(1 for r in q_rows if r["sustainability_pass"] == 1)
                / max(len(q_rows), 1)
            )
            quintiles[f"Q{qi + 1}"] = {
                "n": s["n"],
                "proxy_mean": float(q_proxy),
                "alpha_pct": s["residual_alpha_mean_pct"],
                "positive_rate_pct": s["positive_rate_pct"],
                "sustain_pass_rate_pct": float(pass_rate),
            }
            print(
                f"  Q{qi + 1:1d}         {s['n']:4d} {q_proxy:8.3f} "
                f"{s['residual_alpha_mean_pct']:7.2f}% "
                f"{s['positive_rate_pct']:8.1f}% {pass_rate:13.1f}%",
                flush=True,
            )

        # Diagnose Case A / B / C
        alphas = [quintiles[f"Q{i + 1}"]["alpha_pct"] for i in range(5)]
        print(f"\n  Quintile alpha profile: {alphas}", flush=True)
        if len(alphas) == 5:
            # Monotonic trend test (Spearman-like: correlation with rank)
            ranks = list(range(5))
            corr = float(np.corrcoef(ranks, alphas)[0, 1]) if np.std(alphas) > 0 else 0
            print(f"  Rank correlation (proxy vs alpha): {corr:.3f}", flush=True)
            q1, q5 = alphas[0], alphas[-1]
            if corr > 0.6 and q5 > q1:
                case = "A (monotonic) - sustainability is an ALPHA FACTOR"
            elif abs(corr) < 0.3:
                # Check step function: low Q1, flat Q2-Q5
                if alphas[0] < np.mean(alphas[1:]) - 2:
                    case = "B (step function) - sustainability is only a GARBAGE FILTER"
                else:
                    case = "C (random) - market does NOT care about earnings quality"
            else:
                case = "MIXED - inspect quintile profile manually"
            print(f"\n  DIAGNOSIS: {case}", flush=True)
            gates["quintile_diagnosis"] = {"case": case, "rank_corr": corr, "alphas": alphas}
    else:
        print(f"  Insufficient N ({len(rows_with_proxy)}) for quintile analysis", flush=True)

    # ─────────────────────────────────────────────────────────────
    # Verdict
    # ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 70, flush=True)
    print("PHASE 1.5 VERDICT", flush=True)
    print("=" * 70, flush=True)

    gate_results = {
        k: v.get("verdict") for k, v in gates.items() if isinstance(v, dict) and "verdict" in v
    }
    all_pass = all(v == "PASS" for v in gate_results.values()) and len(gate_results) >= 3
    n_b = stats_b["n"]

    print(f"\n  Gate B1 (Alpha lift):     {gate_results.get('B1_alpha_lift', 'N/A')}", flush=True)
    print(f"  Gate B2 (Hit rate lift):  {gate_results.get('B2_hitrate_lift', 'N/A')}", flush=True)
    print(f"  Gate B3 (Not just risk):  {gate_results.get('B3_not_just_risk', 'N/A')}", flush=True)
    print(f"  N(Group B):               {n_b}  (min {MIN_N_FOR_STATISTICAL_POWER})", flush=True)

    if n_b < MIN_N_FOR_STATISTICAL_POWER:
        print(f"\n  WARNING: N < {MIN_N_FOR_STATISTICAL_POWER}. Results underpowered.", flush=True)

    if all_pass:
        verdict = "PASS - proceed to Phase 2 EQE Composer"
        print(f"\n  >>> VERDICT: {verdict}", flush=True)
        print(
            "  >>> Sustainability lifts FRM alpha. 'Recovery + credibility' is rewarded.",
            flush=True,
        )
        print(
            "  >>> Next: Phase 2 EQE Composer (FRM + Sustainability + Gap sweet spot)", flush=True
        )
    else:
        verdict = "FAIL - STOP. Do not tune parameters."
        print(f"\n  >>> VERDICT: {verdict}", flush=True)
        print("  >>> Frozen decision: FRM alpha comes from recovery itself,", flush=True)
        print("  >>> NOT from recovery quality. Re-examine why FRM has alpha.", flush=True)
        print(
            "  >>> Do NOT proceed to Phase 2. Pivot to v3.5 per failure_decision_tree.", flush=True
        )

    # Export report
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.16.2",
        "phase": "1.5",
        "experiment": "FRM Sustainability Incremental Value Test",
        "date": str(date.today()),
        "baseline": "v3.2.1 Group B (FRM-only, rs_data_available=0)",
        "group_a": stats_a,
        "group_b": stats_b,
        "gates": gates,
        "verdict": verdict,
        "n_group_b": n_b,
    }
    report_path = os.path.join(REPORT_DIR, f"v3_4_phase1_5_ablation_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)

    return report


if __name__ == "__main__":
    run_ablation()
