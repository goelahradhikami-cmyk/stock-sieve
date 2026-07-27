"""
v3.5.1 Exp10A/B: 2D Attribution + Sustainability Incremental Test - Commit 6-S.17.6.

Exp9 revealed that FRM direction is a GATE (portfolio filter), not a
RANKER (per-stock predictor). This reframes the architecture question:
  - FRM direction gate selects the candidate POOL (+2.52% group alpha)
  - Within the pool, what selects individual stocks?

This experiment tests whether Mid FRM × Mid Gap is the stable alpha
cell, and whether sustainability adds or dilutes incremental value.

Exp10A - FRM × Gap 2D Attribution Matrix:
  The strongest signal from Exp8 was Mid FRM + Mid Gap (N=8, +8.43%,
  75% positive). This experiment formalizes the 3×3 matrix and tests:
    1. Is Mid×Mid the best cell? (vs other cells)
    2. Is the pattern stable? (not driven by 1-2 outliers)
    3. Does the diagonal (Mid×Mid) beat the extremes?

  If Mid FRM × Mid Gap is confirmed as the alpha cell:
    -> v3.6 architecture: FRM direction gate + Gap disagreement ranker
    -> Sustainability can be demoted (tested in Exp10B)

Exp10B - Sustainability Incremental Test:
  Sustainability showed inverted-U (v3.4) but the filter REMOVED alpha.
  Is sustainability adding ANY incremental value beyond FRM×Gap, or is
  it just noise that dilutes the signal?

  Method: compare Mid FRM×Mid Gap alpha WITH vs WITHOUT sustainability
  filter. If sustainability dilutes -> demote. If it adds -> keep.

Architecture insight (frozen from Exp9):
  FRM is a GATE, not a RANKER. All previous failures (RS, EGE,
  sustainability, high FRM) made the same error: treating a state
  variable as a ranking variable. The correct architecture is:
    State gate (FRM direction) -> Candidate pool -> Disagreement ranker (Gap)

Usage:
    python scripts/run_v3_5_1_exp10_2d_attribution.py
"""

from __future__ import annotations

import json
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

FRM_LOW_MAX = 57.0
FRM_HIGH_MIN = 68.0
MIN_N_PER_CELL = 3


def load_enriched():
    """Load Group A with FRM score, gap, sustainability, and attribution."""
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    cache = sqlite3.connect(CACHE_DB)
    cache.row_factory = sqlite3.Row

    rows = shadow.execute(
        """SELECT v.security_id, v.trade_date, v.residual_alpha,
                  v.frm_score, v.frm_direction, v.stock_return_t20,
                  v.market_beta, v.sector_beta
           FROM shadow_candidates_v3 v
           WHERE v.residual_alpha IS NOT NULL AND v.rs_data_available = 0
           ORDER BY v.id"""
    ).fetchall()

    ege = ExpectationGapEngine(cache_db=CACHE_DB)

    # Sustainability lookup
    sustain_map = {}
    for r in cache.execute(
        "SELECT security_id, as_of_date, sustainability_pass, profit_elasticity, "
        "accel_trend, company_margin_zscore FROM earnings_sustainability"
    ).fetchall():
        sustain_map[(r["security_id"], r["as_of_date"])] = dict(r)

    enriched = []
    gap_vals = []
    for r in rows:
        sid, td = r["security_id"], r["trade_date"]
        gap = None
        try:
            score = ege.compute(sid, td)
            gap = score.gap_score
        except Exception:
            pass
        gap_vals.append(gap)

        s = sustain_map.get((sid, td), {})
        sustain_pass = s.get("sustainability_pass")
        # Sustainability composite proxy (for tercile bucketing)
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

        enriched.append(
            {
                "sid": sid,
                "td": td,
                "alpha": r["residual_alpha"],
                "frm": r["frm_score"],
                "frm_dir": r["frm_direction"],
                "gap": gap,
                "sustain_pass": sustain_pass,
                "sustain_proxy": sp,
                "stock_return": r["stock_return_t20"],
            }
        )

    shadow.close()
    cache.close()

    # Gap terciles
    gap_valid = sorted([v for v in gap_vals if v is not None])
    gap_t1 = gap_valid[len(gap_valid) // 3]
    gap_t2 = gap_valid[2 * len(gap_valid) // 3]

    return enriched, (gap_t1, gap_t2)


def _stats(rows, label=""):
    if not rows:
        return {"label": label, "n": 0}
    ra = np.array([r["alpha"] for r in rows])
    return {
        "label": label,
        "n": len(rows),
        "alpha_pct": float(np.mean(ra) * 100),
        "median_pct": float(np.median(ra) * 100),
        "positive_rate_pct": float(100.0 * np.sum(ra > 0) / len(ra)),
        "std_pct": float(np.std(ra) * 100),
    }


def _bucket_frm(score):
    if score is None:
        return None
    if score < FRM_LOW_MAX:
        return "L"
    elif score < FRM_HIGH_MIN:
        return "M"
    else:
        return "H"


def _bucket_gap(gap, t1, t2):
    if gap is None:
        return None
    if gap < t1:
        return "L"
    elif gap < t2:
        return "M"
    else:
        return "H"


def exp10a_frm_gap_matrix(enriched, gap_t1, gap_t2):
    """Exp10A: FRM × Gap 2D attribution matrix."""
    print("\n" + "=" * 70, flush=True)
    print("Exp10A: FRM × Gap 2D Attribution Matrix", flush=True)
    print("=" * 70, flush=True)
    print(f"\n  Gap terciles: L < {gap_t1:.4f}, M < {gap_t2:.4f}, H >= {gap_t2:.4f}", flush=True)
    print(f"  FRM buckets:  L < {FRM_LOW_MAX}, M < {FRM_HIGH_MIN}, H >= {FRM_HIGH_MIN}", flush=True)

    # Bucket
    for e in enriched:
        e["frm_b"] = _bucket_frm(e["frm"])
        e["gap_b"] = _bucket_gap(e["gap"], gap_t1, gap_t2)

    # 3x3 matrix
    print("\n  Alpha matrix (% per cell):", flush=True)
    print(f"  {'':12s} {'Gap Low':>18s} {'Gap Mid':>18s} {'Gap High':>18s}", flush=True)
    matrix = {}
    for frm_name, frm_b in [("FRM Low", "L"), ("FRM Mid", "M"), ("FRM High", "H")]:
        cells = []
        for gap_b in ["L", "M", "H"]:
            sub = [e for e in enriched if e["frm_b"] == frm_b and e["gap_b"] == gap_b]
            s = _stats(sub)
            key = f"{frm_b}{gap_b}"
            matrix[key] = s
            if s["n"] > 0:
                cells.append(f"{s['alpha_pct']:+.2f}% (N={s['n']}, {s['positive_rate_pct']:.0f}%)")
            else:
                cells.append("EMPTY")
        print(f"  {frm_name:12s} {cells[0]:>18s} {cells[1]:>18s} {cells[2]:>18s}", flush=True)

    # Highlight Mid×Mid
    mid_mid = matrix.get("MM", {"n": 0})
    print("\n  Mid FRM × Mid Gap:", flush=True)
    if mid_mid["n"] >= MIN_N_PER_CELL:
        print(
            f"    N={mid_mid['n']}, alpha={mid_mid['alpha_pct']:+.2f}%, "
            f"positive={mid_mid['positive_rate_pct']:.1f}%",
            flush=True,
        )

    # Key comparisons
    print("\n  Key comparisons:", flush=True)

    # 1. Mid×Mid vs all other cells
    other_cells = [matrix[k] for k in matrix if k != "MM" and matrix[k]["n"] >= MIN_N_PER_CELL]
    other_rows = []
    for e in enriched:
        if not (e["frm_b"] == "M" and e["gap_b"] == "M"):
            other_rows.append(e)
    other_stats = _stats(other_rows, "All non-Mid×Mid")
    if mid_mid["n"] > 0 and other_stats["n"] > 0:
        delta = mid_mid["alpha_pct"] - other_stats["alpha_pct"]
        print(
            f"    Mid×Mid vs rest: {mid_mid['alpha_pct']:+.2f}% vs {other_stats['alpha_pct']:+.2f}% "
            f"(delta {delta:+.2f}pp)",
            flush=True,
        )

    # 2. Diagonal (L×L, M×M, H×H) vs off-diagonal
    diag_rows = [e for e in enriched if e["frm_b"] == e["gap_b"] and e["frm_b"] is not None]
    offdiag_rows = [
        e
        for e in enriched
        if e["frm_b"] != e["gap_b"] and e["frm_b"] is not None and e["gap_b"] is not None
    ]
    diag_s = _stats(diag_rows, "Diagonal (LL+MM+HH)")
    offdiag_s = _stats(offdiag_rows, "Off-diagonal")
    print(
        f"    Diagonal: {diag_s['alpha_pct']:+.2f}% (N={diag_s['n']}) vs "
        f"Off-diagonal: {offdiag_s['alpha_pct']:+.2f}% (N={offdiag_s['n']})",
        flush=True,
    )

    # 3. Mid row vs Mid column (which dimension drives Mid×Mid?)
    mid_frm_row = [e for e in enriched if e["frm_b"] == "M"]
    mid_gap_col = [e for e in enriched if e["gap_b"] == "M"]
    mid_frm_s = _stats(mid_frm_row, "FRM=Mid (all gap)")
    mid_gap_s = _stats(mid_gap_col, "Gap=Mid (all frm)")
    print(f"    FRM=Mid row: {mid_frm_s['alpha_pct']:+.2f}% (N={mid_frm_s['n']})", flush=True)
    print(f"    Gap=Mid col: {mid_gap_s['alpha_pct']:+.2f}% (N={mid_gap_s['n']})", flush=True)

    # 4. Extreme danger cells (H×H)
    hh = matrix.get("HH", {"n": 0})
    if hh["n"] > 0:
        print(f"    H×H (danger): {hh['alpha_pct']:+.2f}% (N={hh['n']})", flush=True)

    return matrix


def exp10b_sustainability_incremental(enriched, gap_t1, gap_t2):
    """Exp10B: Does sustainability add or dilute value beyond FRM×Gap?"""
    print("\n" + "=" * 70, flush=True)
    print("Exp10B: Sustainability Incremental Test", flush=True)
    print("  Does sustainability ADD value beyond FRM×Gap, or DILUTE it?", flush=True)
    print("=" * 70, flush=True)

    # Focus on Mid FRM × Mid Gap cell (the strongest signal)
    mid_mid = [
        e
        for e in enriched
        if e["frm_b"] == "M" and e["gap_b"] == "M" and e["sustain_pass"] is not None
    ]
    print(f"\n  Mid FRM × Mid Gap with sustainability data: N={len(mid_mid)}", flush=True)

    if len(mid_mid) < 5:
        print("  SKIP: insufficient N in Mid×Mid with sustainability", flush=True)
        return {"skipped": True, "reason": "insufficient N"}

    # Split by sustainability_pass
    pass_1 = [e for e in mid_mid if e["sustain_pass"] == 1]
    pass_0 = [e for e in mid_mid if e["sustain_pass"] == 0]
    s_pass = _stats(pass_1, "Mid×Mid + Sustain PASS")
    s_fail = _stats(pass_0, "Mid×Mid + Sustain FAIL")
    s_all = _stats(mid_mid, "Mid×Mid (all)")

    print(f"\n  {'group':30s} {'n':>4} {'alpha':>8} {'positive':>9}", flush=True)
    print(f"  {'-' * 55}", flush=True)
    for s in [s_all, s_pass, s_fail]:
        if s["n"] > 0:
            print(
                f"  {s['label']:30s} {s['n']:4d} {s['alpha_pct']:+7.2f}% {s['positive_rate_pct']:7.1f}%",
                flush=True,
            )

    # Verdict
    print("\n  Verdict:", flush=True)
    if s_pass["n"] >= 3 and s_fail["n"] >= 3:
        delta = s_pass["alpha_pct"] - s_fail["alpha_pct"]
        if delta > 2:
            verdict = f"Sustainability ADDS value (pass - fail = {delta:+.2f}pp) -> KEEP"
        elif delta < -2:
            verdict = f"Sustainability DILUTES value (pass - fail = {delta:+.2f}pp) -> DEMOTE"
        else:
            verdict = f"Sustainability is NEUTRAL (pass - fail = {delta:+.2f}pp) -> OPTIONAL"
        print(f"    {verdict}", flush=True)
        return {
            "n_mid_mid": len(mid_mid),
            "n_pass": s_pass["n"],
            "n_fail": s_fail["n"],
            "alpha_pass": s_pass["alpha_pct"],
            "alpha_fail": s_fail["alpha_pct"],
            "delta": float(delta),
            "verdict": verdict,
        }
    else:
        print(
            f"    Insufficient N in pass/fail split (pass={s_pass['n']}, fail={s_fail['n']})",
            flush=True,
        )
        return {"skipped": True, "n_pass": s_pass["n"], "n_fail": s_fail["n"]}

    # Also test: sustainability tercile within Mid×Mid (is it monotonic or inverted-U?)
    # (deferred - needs more N)


def exp10c_stability_check(enriched):
    """Quick stability check: Mid×Mid by year."""
    print("\n" + "=" * 70, flush=True)
    print("Stability Check: Mid FRM × Mid Gap by year", flush=True)
    print("=" * 70, flush=True)

    from collections import defaultdict

    by_year = defaultdict(list)
    for e in enriched:
        if e["frm_b"] == "M" and e["gap_b"] == "M":
            by_year[e["td"][:4]].append(e)

    print(f"\n  {'year':6s} {'n':>4} {'alpha':>8} {'positive':>9}", flush=True)
    results = {}
    for y in sorted(by_year.keys()):
        sub = by_year[y]
        s = _stats(sub, y)
        print(
            f"  {y:6s} {s['n']:4d} {s['alpha_pct']:+7.2f}% {s['positive_rate_pct']:7.1f}%",
            flush=True,
        )
        results[y] = {
            "n": s["n"],
            "alpha_pct": s["alpha_pct"],
            "positive_rate_pct": s["positive_rate_pct"],
        }

    if len(results) >= 2:
        alphas = [v["alpha_pct"] for v in results.values() if v["n"] >= 3]
        if len(alphas) >= 2:
            all_pos = all(a > 0 for a in alphas)
            print(
                f"\n  Stability: {'STABLE' if all_pos else 'UNSTABLE'} "
                f"(alpha positive in {sum(1 for a in alphas if a > 0)}/{len(alphas)} years)",
                flush=True,
            )
            results["stable"] = bool(all_pos)

    return results


def run_exp10():
    enriched, (gap_t1, gap_t2) = load_enriched()
    print("=" * 70, flush=True)
    print("v3.5.1 Exp10: 2D Attribution + Sustainability Incremental (6-S.17.6)", flush=True)
    print("=" * 70, flush=True)
    print(f"\nGroup A: N={len(enriched)}", flush=True)

    matrix = exp10a_frm_gap_matrix(enriched, gap_t1, gap_t2)
    sustain_result = exp10b_sustainability_incremental(enriched, gap_t1, gap_t2)
    stability = exp10c_stability_check(enriched)

    # ─── Verdict ───
    print("\n" + "=" * 70, flush=True)
    print("VERDICT SYNTHESIS", flush=True)
    print("=" * 70, flush=True)

    mid_mid = matrix.get("MM", {"n": 0, "alpha_pct": None})
    hh = matrix.get("HH", {"n": 0, "alpha_pct": None})

    # Mid×Mid assessment
    mid_mid_strong = (
        mid_mid["n"] >= 5
        and mid_mid["alpha_pct"] is not None
        and mid_mid["alpha_pct"] > 3
        and mid_mid["positive_rate_pct"] >= 60
    )

    print(
        f"\n  Mid FRM × Mid Gap: N={mid_mid['n']}, "
        f"alpha={mid_mid.get('alpha_pct', 'N/A')}%, "
        f"positive={mid_mid.get('positive_rate_pct', 'N/A')}%",
        flush=True,
    )

    if mid_mid_strong:
        print("  -> STRONG: Mid×Mid is a candidate alpha cell", flush=True)
    else:
        print("  -> WEAK or insufficient: Mid×Mid needs confirmation", flush=True)

    # Sustainability verdict
    if isinstance(sustain_result, dict) and "verdict" in sustain_result:
        print(f"\n  Sustainability: {sustain_result['verdict']}", flush=True)

    # Stability
    if isinstance(stability, dict) and stability.get("stable") is not None:
        print(
            f"  Temporal stability: {'STABLE' if stability['stable'] else 'UNSTABLE'}", flush=True
        )

    # Architecture insight
    print("\n  ARCHITECTURE INSIGHT (from Exp9 + Exp10):", flush=True)
    print("  FRM is a GATE (portfolio filter), not a RANKER (per-stock predictor).", flush=True)
    print("  All previous failures (RS, EGE, sustainability, high FRM) made the", flush=True)
    print("  same error: treating a state variable as a ranking variable.", flush=True)
    print("  Correct architecture: State gate -> Candidate pool -> Disagreement ranker", flush=True)

    if mid_mid_strong:
        verdict = "Mid FRM × Mid Gap confirmed as alpha cell (candidate architecture)"
        recommendation = (
            "v3.6 architecture candidate: FRM direction gate (portfolio filter) "
            "+ Gap disagreement ranker (per-stock selector within pool). "
            "Sustainability "
            + ("DEMOTED" if "DEMOTE" in str(sustain_result.get("verdict", "")) else "optional")
            + ". Extend N to confirm before paradigm shift."
        )
    else:
        verdict = "Mid FRM × Mid Gap promising but needs larger N for confirmation"
        recommendation = (
            "Extend N to N>=200, re-test Mid×Mid stability before architecture decision."
        )

    print(f"\n  >>> VERDICT: {verdict}", flush=True)
    print(f"  >>> RECOMMENDATION: {recommendation}", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.17.6",
        "experiment": "exp10_2d_attribution",
        "date": str(date.today()),
        "gap_terciles": [gap_t1, gap_t2],
        "frm_buckets": [FRM_LOW_MAX, FRM_HIGH_MIN],
        "matrix": {k: v for k, v in matrix.items()},
        "sustainability_incremental": sustain_result,
        "stability": stability,
        "verdict": verdict,
        "recommendation": recommendation,
        "architecture_insight": "FRM is GATE not RANKER. State gate -> pool -> disagreement ranker.",
    }
    report_path = os.path.join(REPORT_DIR, f"v3_5_1_exp10_2d_attribution_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_exp10()
