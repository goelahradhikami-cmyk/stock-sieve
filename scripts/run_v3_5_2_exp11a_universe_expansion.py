"""
v3.5.2 Exp11A: Universe Expansion Validation - Commit 6-S.17.8.

Turn the N=8 story (Mid×Mid +8.43%) into an N=194 statistical fact.
Three experimental disciplines (per design review) prevent 'beautiful
result misjudgment' at larger N:

  Discipline 1 - Threshold-free continuous test:
    Rank-based bucketing (Q3×Q3) has arbitrary boundaries that shift
    when N expands. The REAL theory is not 'Q3 is best' but 'distance
    from extreme predicts alpha'. Test continuous:
      uncertainty_score = -|FRM_z| - |Gap_z|  (higher = closer to center)
    If continuous beats rank-based, the theory is about DISTANCE from
    consensus extreme, not a specific bucket.

  Discipline 2 - Null model benchmark:
    A +8% cell with N=30 could be random. Bootstrap 10000 random samples
    of same N from the full pool. Compare Mid×Mid alpha to the random
    distribution. Must exceed 95th percentile to be significant.

  Discipline 3 - Group A/C split + single-variable decomposition:
    Group A (no RS gate) and Group C (RS-filtered) have different
    baselines. Report separately. Also compare:
      FRM Mid alone vs Gap Mid alone vs Mid×Mid
    to confirm the INTERSECTION creates alpha (not two weak signals stacked).

  Discipline 4 - Extreme penalty persistence:
    The strongest economic finding is not 'Mid×Mid is high' but
    'extreme certainty zones are bad'. Verify H×H < Mid×Mid persists.

Architecture insight (frozen 6-S.17.7):
  Stock Sieve does not find the 'best' stocks. It finds stocks where
  the market cannot reach consensus. The question is whether this
  'middle zone' is stable at N=194 or was a small-sample artifact.

Success criteria (frozen 6-S.17.7):
  alpha > 0, positive_rate > 55%, N_mid_mid >= 30, spread > 3pp
  AND Mid×Mid positive in BOTH Group A and Group C
  AND Mid×Mid exceeds 95th percentile of random bootstrap
  AND continuous uncertainty_score correlates positively with alpha

Usage:
    python scripts/run_v3_5_2_exp11a_universe_expansion.py
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

N_BOOTSTRAP = 10000
BOOTSTRAP_PERCENTILE = 95


def load_all_candidates():
    """Load ALL candidates (Group A + Group C) with FRM score and gap."""
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    rows = shadow.execute(
        """SELECT v.security_id, v.trade_date, v.residual_alpha,
                  v.frm_score, v.frm_direction, v.rs_data_available,
                  v.stock_return_t20
           FROM shadow_candidates_v3 v
           WHERE v.residual_alpha IS NOT NULL
           ORDER BY v.id"""
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
        enriched.append({
            "sid": r["security_id"], "td": r["trade_date"],
            "alpha": r["residual_alpha"],
            "frm": r["frm_score"],
            "frm_dir": r["frm_direction"],
            "rs_avail": r["rs_data_available"],
            "gap": gap,
        })
    return enriched


def _stats(rows, label=""):
    if not rows:
        return {"label": label, "n": 0}
    ra = np.array([r["alpha"] for r in rows])
    return {
        "label": label, "n": len(rows),
        "alpha_pct": float(np.mean(ra) * 100),
        "median_pct": float(np.median(ra) * 100),
        "positive_rate_pct": float(100.0 * np.sum(ra > 0) / len(ra)),
        "std_pct": float(np.std(ra) * 100),
    }


def _print_stats(s, indent=4):
    pad = " " * indent
    if s["n"] == 0:
        print(f"{pad}{s['label']}: EMPTY", flush=True)
        return
    print(f"{pad}{s['label']}: N={s['n']}  alpha={s['alpha_pct']:+.2f}%  "
          f"positive={s['positive_rate_pct']:.1f}%  median={s['median_pct']:+.2f}%",
          flush=True)


def _quintile(values, n_quintiles=5):
    """Assign quintile labels (1-5) to values, handling None."""
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return [None] * len(values)
    valid.sort(key=lambda x: x[1])
    q_labels = [None] * len(values)
    q_size = len(valid) // n_quintiles
    for qi in range(n_quintiles):
        start = qi * q_size
        end = (qi + 1) * q_size if qi < n_quintiles - 1 else len(valid)
        for idx, _ in valid[start:end]:
            q_labels[idx] = qi + 1
    return q_labels


def discipline1_rank_vs_continuous(enriched):
    """Discipline 1: Rank-based Q3×Q3 vs continuous uncertainty_score."""
    print("\n" + "=" * 70, flush=True)
    print("DISCIPLINE 1: Rank-based (Q3×Q3) vs Continuous (uncertainty_score)", flush=True)
    print("=" * 70, flush=True)

    # Compute z-scores (cross-sectional)
    frm_vals = np.array([e["frm"] or np.nan for e in enriched], dtype=float)
    gap_vals = np.array([e["gap"] if e["gap"] is not None else np.nan for e in enriched], dtype=float)

    frm_z = (frm_vals - np.nanmean(frm_vals)) / np.nanstd(frm_vals)
    gap_z = (gap_vals - np.nanmean(gap_vals)) / np.nanstd(gap_vals)

    for i, e in enumerate(enriched):
        e["frm_z"] = float(frm_z[i]) if not np.isnan(frm_z[i]) else None
        e["gap_z"] = float(gap_z[i]) if not np.isnan(gap_z[i]) else None
        # uncertainty_score = -|FRM_z| - |Gap_z| (higher = closer to center)
        if e["frm_z"] is not None and e["gap_z"] is not None:
            e["uncertainty_score"] = -abs(e["frm_z"]) - abs(e["gap_z"])
        else:
            e["uncertainty_score"] = None

    # Rank-based quintiles
    frm_q = _quintile([e["frm"] for e in enriched])
    gap_q = _quintile([e["gap"] for e in enriched])
    for i, e in enumerate(enriched):
        e["frm_q"] = frm_q[i]
        e["gap_q"] = gap_q[i]

    # --- Rank-based: Q3×Q3 ---
    print(f"\n  --- Rank-based (quintile) ---", flush=True)
    q3xq3 = [e for e in enriched if e["frm_q"] == 3 and e["gap_q"] == 3]
    s_q3 = _stats(q3xq3, "Q3 FRM × Q3 Gap")
    _print_stats(s_q3)

    # Full 5×5 matrix (compact)
    print(f"\n  Rank-based 5×5 matrix (alpha%):", flush=True)
    print(f"  {'':8s}", end="", flush=True)
    for gq in range(1, 6):
        print(f"  Gap Q{gq:1d}      ", end="", flush=True)
    print(flush=True)
    for fq in range(1, 6):
        print(f"  FRM Q{fq:1d}  ", end="", flush=True)
        for gq in range(1, 6):
            sub = [e for e in enriched if e["frm_q"] == fq and e["gap_q"] == gq]
            s = _stats(sub)
            if s["n"] > 0:
                print(f" {s['alpha_pct']:+5.1f}%(N={s['n']:2d})", end="", flush=True)
            else:
                print(f"    EMPTY    ", end="", flush=True)
        print(flush=True)

    # --- Continuous: uncertainty_score ---
    print(f"\n  --- Continuous (uncertainty_score = -|FRM_z| - |Gap_z|) ---", flush=True)
    valid_us = [e for e in enriched if e["uncertainty_score"] is not None]
    print(f"  N with uncertainty_score: {len(valid_us)}", flush=True)

    # Quintile by uncertainty_score
    us_vals = [e["uncertainty_score"] for e in valid_us]
    us_q = _quintile(us_vals)
    for i, e in enumerate(valid_us):
        e["us_q"] = us_q[i]

    print(f"\n  uncertainty_score quintile vs alpha:", flush=True)
    print(f"  {'quintile':10s} {'n':>4} {'us_range':>16} {'alpha':>8} {'positive':>9}", flush=True)
    us_quintile_stats = {}
    for qi in range(1, 6):
        sub = [e for e in valid_us if e["us_q"] == qi]
        s = _stats(sub, f"Q{qi}")
        us_vals_sub = [e["uncertainty_score"] for e in sub]
        if us_vals_sub:
            us_range = f"[{min(us_vals_sub):.2f},{max(us_vals_sub):.2f}]"
        else:
            us_range = "EMPTY"
        us_quintile_stats[f"Q{qi}"] = s
        if s["n"] > 0:
            print(f"  Q{qi:1d}         {s['n']:4d} {us_range:>16} "
                  f"{s['alpha_pct']:+6.2f}% {s['positive_rate_pct']:7.1f}%", flush=True)

    # Correlation: uncertainty_score vs alpha
    us_arr = np.array([e["uncertainty_score"] for e in valid_us])
    alpha_arr = np.array([e["alpha"] for e in valid_us])
    corr = float(np.corrcoef(us_arr, alpha_arr)[0, 1])
    print(f"\n  Correlation(uncertainty_score, alpha) = {corr:.4f}", flush=True)
    if corr > 0:
        print(f"  -> POSITIVE: higher uncertainty_score (closer to center) -> higher alpha", flush=True)
        print(f"     Theory SUPPORTED: distance from extreme predicts alpha", flush=True)
    else:
        print(f"  -> NEGATIVE/ZERO: continuous uncertainty does NOT predict alpha", flush=True)

    return {
        "q3xq3": s_q3,
        "us_quintiles": us_quintile_stats,
        "correlation": corr,
        "q3xq3_alpha": s_q3["alpha_pct"] if s_q3["n"] > 0 else None,
    }


def discipline2_null_model(enriched, target_n, target_alpha):
    """Discipline 2: Bootstrap null model - is target_alpha significant?"""
    print("\n" + "=" * 70, flush=True)
    print(f"DISCIPLINE 2: Null Model Bootstrap (N={target_n}, target alpha={target_alpha:+.2f}%)", flush=True)
    print("=" * 70, flush=True)

    all_alphas = np.array([e["alpha"] for e in enriched])
    n_total = len(all_alphas)

    if target_n > n_total:
        print(f"  SKIP: target_n {target_n} > total N {n_total}", flush=True)
        return {"skipped": True}

    # Bootstrap: sample target_n randomly, compute mean, repeat N_BOOTSTRAP times
    rng = np.random.default_rng(42)
    boot_means = np.zeros(N_BOOTSTRAP)
    for i in range(N_BOOTSTRAP):
        sample = rng.choice(all_alphas, size=target_n, replace=False)
        boot_means[i] = np.mean(sample) * 100

    p95 = float(np.percentile(boot_means, BOOTSTRAP_PERCENTILE))
    p99 = float(np.percentile(boot_means, 99))
    boot_mean = float(np.mean(boot_means))
    boot_std = float(np.std(boot_means))

    # Where does target_alpha fall in the bootstrap distribution?
    percentile_rank = float(100.0 * np.sum(boot_means <= target_alpha) / N_BOOTSTRAP)

    print(f"  Bootstrap: {N_BOOTSTRAP} random samples of N={target_n}", flush=True)
    print(f"  Random distribution: mean={boot_mean:+.2f}%, std={boot_std:.2f}%", flush=True)
    print(f"  95th percentile: {p95:+.2f}%", flush=True)
    print(f"  99th percentile: {p99:+.2f}%", flush=True)
    print(f"  Target alpha:    {target_alpha:+.2f}%", flush=True)
    print(f"  Percentile rank: {percentile_rank:.1f}% (target exceeds {percentile_rank:.1f}% of random)", flush=True)

    significant_95 = target_alpha > p95
    significant_99 = target_alpha > p99
    print(f"\n  Significant at 5%:  {'YES' if significant_95 else 'NO'} "
          f"(target > 95th percentile {p95:+.2f}%)", flush=True)
    print(f"  Significant at 1%:  {'YES' if significant_99 else 'NO'} "
          f"(target > 99th percentile {p99:+.2f}%)", flush=True)

    return {
        "bootstrap_mean": boot_mean, "bootstrap_std": boot_std,
        "p95": p95, "p99": p99,
        "target_alpha": target_alpha,
        "percentile_rank": percentile_rank,
        "significant_95": bool(significant_95),
        "significant_99": bool(significant_99),
    }


def discipline3_group_split(enriched):
    """Discipline 3: Group A / Group C split + single-variable decomposition."""
    print("\n" + "=" * 70, flush=True)
    print("DISCIPLINE 3: Group A/C Split + Single-Variable Decomposition", flush=True)
    print("=" * 70, flush=True)

    group_a = [e for e in enriched if e["rs_avail"] == 0]
    group_c = [e for e in enriched if e["rs_avail"] == 1]

    # Mid×Mid (Q3×Q3) in each group
    print(f"\n  --- Mid×Mid (Q3×Q3) by Group ---", flush=True)
    print(f"  {'segment':25s} {'n_total':>7} {'n_mid':>7} {'alpha':>8} {'positive':>9}", flush=True)

    results = {}
    for label, grp in [("Group A (no RS)", group_a),
                       ("Group C (RS-filtered)", group_c),
                       ("Combined", enriched)]:
        mid_mid = [e for e in grp if e["frm_q"] == 3 and e["gap_q"] == 3]
        s = _stats(mid_mid, label)
        results[label] = {"n_total": len(grp), "n_mid_mid": s["n"],
                          "alpha_pct": s["alpha_pct"], "positive_rate_pct": s["positive_rate_pct"]}
        if s["n"] > 0:
            print(f"  {label:25s} {len(grp):7d} {s['n']:7d} {s['alpha_pct']:+7.2f}% "
                  f"{s['positive_rate_pct']:7.1f}%", flush=True)
        else:
            print(f"  {label:25s} {len(grp):7d} {s['n']:7d}    EMPTY", flush=True)

    # Single-variable decomposition
    print(f"\n  --- Single-Variable Decomposition (Combined) ---", flush=True)
    print(f"  {'strategy':30s} {'n':>5} {'alpha':>8} {'positive':>9}", flush=True)
    print(f"  {'-'*55}", flush=True)

    comparisons = {
        "FRM direction only (all)": enriched,
        "FRM Q3 only (all gap)": [e for e in enriched if e["frm_q"] == 3],
        "Gap Q3 only (all frm)": [e for e in enriched if e["gap_q"] == 3],
        "FRM Q3 × Gap Q3 (Mid×Mid)": [e for e in enriched if e["frm_q"] == 3 and e["gap_q"] == 3],
        "FRM Q5 (highest)": [e for e in enriched if e["frm_q"] == 5],
        "Gap Q5 (highest)": [e for e in enriched if e["gap_q"] == 5],
    }
    for label, sub in comparisons.items():
        s = _stats(sub, label)
        if s["n"] > 0:
            print(f"  {label:30s} {s['n']:5d} {s['alpha_pct']:+7.2f}% {s['positive_rate_pct']:7.1f}%",
                  flush=True)
        else:
            print(f"  {label:30s}     0    EMPTY", flush=True)
        results[label] = s

    # Intersection test: is Mid×Mid > max(FRM Q3 only, Gap Q3 only)?
    mid_mid_alpha = results.get("FRM Q3 × Gap Q3 (Mid×Mid)", {}).get("alpha_pct")
    frm_q3_alpha = results.get("FRM Q3 only (all gap)", {}).get("alpha_pct")
    gap_q3_alpha = results.get("Gap Q3 only (all frm)", {}).get("alpha_pct")

    if all(a is not None for a in [mid_mid_alpha, frm_q3_alpha, gap_q3_alpha]):
        print(f"\n  Intersection test:", flush=True)
        print(f"    FRM Q3 only:    {frm_q3_alpha:+.2f}%", flush=True)
        print(f"    Gap Q3 only:    {gap_q3_alpha:+.2f}%", flush=True)
        print(f"    Mid×Mid:        {mid_mid_alpha:+.2f}%", flush=True)
        delta_frm = mid_mid_alpha - frm_q3_alpha
        delta_gap = mid_mid_alpha - gap_q3_alpha
        print(f"    Mid×Mid - FRM Q3: {delta_frm:+.2f}pp", flush=True)
        print(f"    Mid×Mid - Gap Q3: {delta_gap:+.2f}pp", flush=True)
        if delta_frm > 1 and delta_gap > 1:
            print(f"    -> INTERSECTION creates alpha (not two weak signals stacked)", flush=True)
        else:
            print(f"    -> Intersection does NOT add value beyond single variables", flush=True)
        results["intersection_delta_frm"] = float(delta_frm)
        results["intersection_delta_gap"] = float(delta_gap)

    return results


def discipline4_extreme_penalty(enriched):
    """Discipline 4: Extreme penalty persistence (H×H < Mid×Mid)."""
    print("\n" + "=" * 70, flush=True)
    print("DISCIPLINE 4: Extreme Penalty Persistence", flush=True)
    print("=" * 70, flush=True)

    mid_mid = [e for e in enriched if e["frm_q"] == 3 and e["gap_q"] == 3]
    high_high = [e for e in enriched if e["frm_q"] == 5 and e["gap_q"] == 5]
    low_low = [e for e in enriched if e["frm_q"] == 1 and e["gap_q"] == 1]

    s_mm = _stats(mid_mid, "Mid×Mid (Q3×Q3)")
    s_hh = _stats(high_high, "High×High (Q5×Q5)")
    s_ll = _stats(low_low, "Low×Low (Q1×Q1)")

    print(f"\n  {'cell':20s} {'n':>5} {'alpha':>8} {'positive':>9}", flush=True)
    for s in [s_mm, s_hh, s_ll]:
        if s["n"] > 0:
            print(f"  {s['label']:20s} {s['n']:5d} {s['alpha_pct']:+7.2f}% {s['positive_rate_pct']:7.1f}%",
                  flush=True)

    if s_mm["n"] > 0 and s_hh["n"] > 0:
        penalty = s_mm["alpha_pct"] - s_hh["alpha_pct"]
        print(f"\n  Mid×Mid - High×High = {penalty:+.2f}pp", flush=True)
        if penalty > 3:
            print(f"  -> Extreme penalty PERSISTS at N=194 (market punishes certainty)", flush=True)
        elif penalty > 0:
            print(f"  -> Extreme penalty present but weak", flush=True)
        else:
            print(f"  -> Extreme penalty does NOT persist (H×H >= Mid×Mid)", flush=True)
        return {"mid_mid_alpha": s_mm["alpha_pct"], "hh_alpha": s_hh["alpha_pct"],
                "penalty_pp": float(penalty)}
    return {"skipped": True}


def run_exp11a():
    enriched = load_all_candidates()
    print("=" * 70, flush=True)
    print("v3.5.2 Exp11A: Universe Expansion Validation (6-S.17.8)", flush=True)
    print("Turn N=8 story into N=194 statistical fact", flush=True)
    print("=" * 70, flush=True)
    print(f"\nTotal candidates: N={len(enriched)}", flush=True)
    print(f"  Group A (no RS): {sum(1 for e in enriched if e['rs_avail']==0)}", flush=True)
    print(f"  Group C (RS):    {sum(1 for e in enriched if e['rs_avail']==1)}", flush=True)

    # Run all 4 disciplines
    d1 = discipline1_rank_vs_continuous(enriched)
    d2 = discipline2_null_model(enriched,
                                 target_n=d1["q3xq3"]["n"],
                                 target_alpha=d1["q3xq3"]["alpha_pct"])
    d3 = discipline3_group_split(enriched)
    d4 = discipline4_extreme_penalty(enriched)

    # ─── Verdict ───
    print("\n" + "=" * 70, flush=True)
    print("VERDICT SYNTHESIS", flush=True)
    print("=" * 70, flush=True)

    checks = {}
    # Check 1: Mid×Mid alpha > 0 at N>=30
    mm_n = d1["q3xq3"]["n"]
    mm_alpha = d1["q3xq3"]["alpha_pct"]
    checks["mid_mid_positive"] = mm_alpha is not None and mm_alpha > 0 and mm_n >= 30
    checks["mid_mid_n_30"] = mm_n >= 30

    # Check 2: positive_rate > 55%
    checks["positive_rate_55"] = d1["q3xq3"]["positive_rate_pct"] > 55

    # Check 3: spread > 3pp (Mid×Mid - overall)
    overall_alpha = float(np.mean([e["alpha"] for e in enriched]) * 100)
    spread = (mm_alpha - overall_alpha) if mm_alpha is not None else None
    checks["spread_3pp"] = spread is not None and spread > 3

    # Check 4: Group A and Group C both positive
    ga_mm = d3.get("Group A (no RS)", {})
    gc_mm = d3.get("Group C (RS-filtered)", {})
    checks["both_groups_positive"] = (ga_mm.get("alpha_pct", 0) > 0 and gc_mm.get("alpha_pct", 0) > 0)

    # Check 5: null model significant at 5%
    checks["null_significant"] = d2.get("significant_95", False) if not d2.get("skipped") else False

    # Check 6: continuous correlation positive
    checks["continuous_positive"] = d1.get("correlation", 0) > 0

    # Check 7: extreme penalty persists
    checks["extreme_penalty"] = d4.get("penalty_pp", 0) > 3 if "penalty_pp" in d4 else False

    print(f"\n  Validation checks:", flush=True)
    print(f"    Mid×Mid alpha > 0, N>=30:     {'PASS' if checks['mid_mid_positive'] else 'FAIL'} "
          f"(alpha={mm_alpha}, N={mm_n})", flush=True)
    print(f"    Positive rate > 55%:          {'PASS' if checks['positive_rate_55'] else 'FAIL'} "
          f"({d1['q3xq3']['positive_rate_pct']:.1f}%)", flush=True)
    print(f"    Spread vs baseline > 3pp:     {'PASS' if checks['spread_3pp'] else 'FAIL'} "
          f"(spread={spread:+.2f}pp)", flush=True)
    print(f"    Both Group A & C positive:    {'PASS' if checks['both_groups_positive'] else 'FAIL'} "
          f"(A={ga_mm.get('alpha_pct','?')}, C={gc_mm.get('alpha_pct','?')})", flush=True)
    print(f"    Null model significant 5%:    {'PASS' if checks['null_significant'] else 'FAIL'} "
          f"(rank={d2.get('percentile_rank','?')}%)", flush=True)
    print(f"    Continuous corr > 0:          {'PASS' if checks['continuous_positive'] else 'FAIL'} "
          f"(corr={d1.get('correlation','?'):.4f})", flush=True)
    print(f"    Extreme penalty > 3pp:        {'PASS' if checks['extreme_penalty'] else 'FAIL'} "
          f"(penalty={d4.get('penalty_pp','?')}pp)", flush=True)

    all_pass = all(checks.values())
    n_pass = sum(checks.values())
    n_total = len(checks)

    if all_pass:
        verdict = "ALL CHECKS PASS - Mid×Mid survives N=194. Statistical fact confirmed."
        recommendation = "Proceed to Exp11B (time split out-of-sample). v3.6 architecture candidate viable."
    elif n_pass >= 5:
        verdict = f"PARTIAL ({n_pass}/{n_total} pass) - promising but not all checks met"
        recommendation = "Analyze which checks failed. May need to adjust before Exp11B."
    else:
        verdict = f"FAIL ({n_pass}/{n_total} pass) - Mid×Mid does NOT survive N=194"
        recommendation = "Mid×Mid was sample-specific. Do NOT build v3.6 on it. Explore other intersections."

    print(f"\n  >>> VERDICT: {verdict}", flush=True)
    print(f"  >>> RECOMMENDATION: {recommendation}", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.17.8",
        "experiment": "exp11a_universe_expansion",
        "date": str(date.today()),
        "n_total": len(enriched),
        "discipline1_rank_vs_continuous": d1,
        "discipline2_null_model": d2,
        "discipline3_group_split": d3,
        "discipline4_extreme_penalty": d4,
        "overall_alpha_pct": overall_alpha,
        "checks": checks,
        "n_pass": n_pass, "n_total_checks": n_total,
        "verdict": verdict, "recommendation": recommendation,
    }
    report_path = os.path.join(REPORT_DIR, f"v3_5_2_exp11a_universe_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_exp11a()
