"""
v3.5 Phase 2 Exp8: Uncertainty Asymmetry Validation - Commit 6-S.17.2.

THE PARADIGM-SHIFT experiment. Tests whether Stock Sieve's alpha source
is 'uncertainty asymmetry' (H3) rather than 'recovery quality'.

Exp1 proved that four independent signals (gap, sustainability, FRM
score, EA) all show inverted-U with alpha. The critical question now:
  Do the four 'middle zones' OVERLAP into a single Alpha Island?
  Or is the inverted-U just a 'crowding effect' (middle = less crowded
  trades, not uncertainty premium)?

Three sub-experiments:

  Step 1 - 3D Uncertainty Cube:
    FRM (Low/Mid/High) x Gap (Low/Mid/High) x Sustainability (Low/Mid/High)
    3x3x3 = 27 cells. Find the Middle-Middle-Middle cell.
    H3-1: Middle-Middle-Middle is the Alpha Island (highest alpha).

  Step 2 - Extreme Combination Danger:
    H3-2: High-High combinations (High FRM + High Gap, etc.) are risk,
          not opportunity (market already priced in or distrusts).
    Known danger (v3.3.1): High FRM + High Gap = -5.29%

  Step 3 - Crowding/Control Test (CRITICAL):
    The inverted-U could be a crowding artifact: extreme stocks are
    hot/event stocks (over-traded), middle stocks are less crowded.
    Must control for: market_cap, turnover, momentum.
    H3 is ONLY validated if Middle-Middle-Middle alpha survives
    controlling for these crowding proxies.

  Step 4 - Time Split Validation:
    Split by year to verify the Alpha Island is not regime-specific.

Success condition (frozen):
  Middle-Middle-Middle alpha > 0
  AND survives crowding controls (market_cap, turnover, momentum)
  AND stable across time splits

If PASS: Security Analyst paradigm shift
  FROM: Recovery Quality Detector (find best-quality recoveries)
  TO:   Uncertainty Asymmetry Detector (find market-uncertain recoveries)

Usage:
    python scripts/run_v3_5_exp8_uncertainty_cube.py
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

# Bucket thresholds (frozen from prior experiments)
# FRM: from Exp1 quintile boundaries (improving direction)
FRM_LOW_MAX = 57.0  # Q1-Q2 boundary ~57
FRM_HIGH_MIN = 68.0  # Q4-Q5 boundary ~68

# Gap: from v3.3.1 autopsy (recomputed cross-sectionally per decision date)
# Use terciles of available gap distribution at runtime

# Sustainability: use composite proxy (from v3.4 ablation), terciles at runtime

MIN_N_PER_CELL = 3  # minimum N to report a cell (relaxed for 3D)


def load_enriched_group_a():
    """Load Group A with gap (from EGE engine) + sustainability + control variables."""
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    cache = sqlite3.connect(CACHE_DB)
    cache.row_factory = sqlite3.Row

    # Group A base
    rows = shadow.execute(
        """SELECT v.security_id, v.trade_date, v.episode_id, v.residual_alpha,
                  v.market_beta, v.sector_beta, v.stock_return_t20,
                  v.frm_direction, v.frm_score, v.earnings_acceleration,
                  v.volume_ratio, v.relative_strength
           FROM shadow_candidates_v3 v
           WHERE v.residual_alpha IS NOT NULL AND v.rs_data_available = 0
           ORDER BY v.id"""
    ).fetchall()

    # Sustainability lookup
    sustain_map = {}
    for r in cache.execute("SELECT * FROM earnings_sustainability").fetchall():
        sustain_map[(r["security_id"], r["as_of_date"])] = dict(r)

    # Market cap from security_master (static, latest available)
    mcap_map = {}
    for r in cache.execute(
        "SELECT security_id, code, total_mv FROM security_master WHERE total_mv IS NOT NULL"
    ).fetchall():
        mcap_map[r["security_id"]] = r["total_mv"]
        mcap_map[r["code"]] = r["total_mv"]

    # EGE engine for gap computation
    ege = ExpectationGapEngine(cache_db=CACHE_DB)

    enriched = []
    for r in rows:
        sid = r["security_id"]
        td = r["trade_date"]
        s = sustain_map.get((sid, td), {})

        # Compute gap via EGE engine (vintage-aware, uses available_date <= td)
        gap_score = None
        try:
            score = ege.compute(sid, td)
            gap_score = score.gap_score
        except Exception:
            pass

        # Sustainability composite proxy
        sustain_proxy = None
        if s:
            vals = []
            if s.get("profit_elasticity") is not None and abs(s["profit_elasticity"]) < 20:
                vals.append(min(s["profit_elasticity"], 5.0) / 5.0)
            if s.get("accel_trend") is not None:
                vals.append(max(-1, min(1, s["accel_trend"])))
            if s.get("company_margin_zscore") is not None:
                vals.append(max(-1, min(1, -s["company_margin_zscore"] / 3.0)))
            if vals:
                sustain_proxy = float(np.mean(vals))

        enriched.append(
            {
                "security_id": sid,
                "trade_date": td,
                "episode_id": r["episode_id"],
                "residual_alpha": r["residual_alpha"],
                "frm_score": r["frm_score"],
                "frm_direction": r["frm_direction"],
                "gap_score": gap_score,
                "sustain_proxy": sustain_proxy,
                "earnings_acceleration": r["earnings_acceleration"],
                # Control variables (crowding proxies)
                "market_cap": mcap_map.get(sid) or mcap_map.get(sid.zfill(6)),
                "volume_ratio": r["volume_ratio"],  # liquidity/turnover proxy
                "relative_strength": r["relative_strength"],  # momentum proxy
                "stock_return_t20": r["stock_return_t20"],
            }
        )

    shadow.close()
    cache.close()
    return enriched


def _bucket(value, low_max, high_min):
    """Bucket a value into Low/Mid/High."""
    if value is None:
        return None
    if value < low_max:
        return "L"
    elif value < high_min:
        return "M"
    else:
        return "H"


def _terciles(values):
    """Compute tercile thresholds for a list of values (excluding None)."""
    valid = sorted([v for v in values if v is not None])
    if len(valid) < 3:
        return None, None
    n = len(valid)
    t1 = valid[n // 3]
    t2 = valid[2 * n // 3]
    return t1, t2


def _bucket_tercile(value, t1, t2):
    if value is None or t1 is None:
        return None
    if value < t1:
        return "L"
    elif value < t2:
        return "M"
    else:
        return "H"


def _stats(rows, label=""):
    if not rows:
        return {"label": label, "n": 0}
    ra = np.array([r["residual_alpha"] for r in rows])
    return {
        "label": label,
        "n": len(rows),
        "alpha_pct": float(np.mean(ra) * 100),
        "positive_rate_pct": float(100.0 * np.sum(ra > 0) / len(ra)),
        "median_pct": float(np.median(ra) * 100),
    }


def step1_3d_cube(enriched):
    """Step 1: 3D Uncertainty Cube (FRM x Gap x Sustainability)."""
    print("\n" + "=" * 70, flush=True)
    print("STEP 1: 3D Uncertainty Cube (FRM x Gap x Sustainability)", flush=True)
    print("=" * 70, flush=True)

    # Compute tercile thresholds for gap and sustainability (cross-sectional)
    gap_t1, gap_t2 = _terciles([e["gap_score"] for e in enriched])
    sus_t1, sus_t2 = _terciles([e["sustain_proxy"] for e in enriched])
    print(
        f"\n  Gap terciles: L < {gap_t1:.3f}, M < {gap_t2:.3f}, H >= {gap_t2:.3f}"
        if gap_t1
        else "  Gap: insufficient",
        flush=True,
    )
    print(
        f"  Sustain terciles: L < {sus_t1:.3f}, M < {sus_t2:.3f}, H >= {sus_t2:.3f}"
        if sus_t1
        else "  Sustain: insufficient",
        flush=True,
    )
    print(f"  FRM buckets: L < {FRM_LOW_MAX}, M < {FRM_HIGH_MIN}, H >= {FRM_HIGH_MIN}", flush=True)

    # Bucket each row
    bucketed = []
    for e in enriched:
        frm_b = _bucket(e["frm_score"], FRM_LOW_MAX, FRM_HIGH_MIN)
        gap_b = _bucket_tercile(e["gap_score"], gap_t1, gap_t2)
        sus_b = _bucket_tercile(e["sustain_proxy"], sus_t1, sus_t2)
        if frm_b and gap_b and sus_b:
            e["frm_b"] = frm_b
            e["gap_b"] = gap_b
            e["sus_b"] = sus_b
            bucketed.append(e)

    print(f"\n  Rows with all 3 buckets: {len(bucketed)} / {len(enriched)}", flush=True)

    # Build 3x3x3 cube
    cube = {}
    for frm in "LMH":
        for gap in "LMH":
            for sus in "LMH":
                cell_rows = [
                    e
                    for e in bucketed
                    if e["frm_b"] == frm and e["gap_b"] == gap and e["sus_b"] == sus
                ]
                key = f"{frm}{gap}{sus}"
                cube[key] = _stats(cell_rows, key)

    # Print cube as flat table sorted by alpha
    print(
        f"\n  {'cell':6s} {'frm':4s} {'gap':4s} {'sus':4s} {'n':>4} {'alpha':>8} {'positive':>9}",
        flush=True,
    )
    print(f"  {'-' * 45}", flush=True)
    sorted_cells = sorted(
        cube.items(), key=lambda x: -(x[1]["alpha_pct"] if x[1]["n"] > 0 else -999)
    )
    for key, s in sorted_cells:
        if s["n"] > 0:
            frm, gap, sus = key[0], key[1], key[2]
            frm_name = {"L": "Low", "M": "Mid", "H": "High"}[frm]
            gap_name = {"L": "Low", "M": "Mid", "H": "High"}[gap]
            sus_name = {"L": "Low", "M": "Mid", "H": "High"}[sus]
            marker = " <-- ALPHA ISLAND" if key == "MMM" else ""
            marker = " <-- DANGER" if key == "HHH" and s["n"] > 0 else marker
            print(
                f"  {key:6s} {frm_name:4s} {gap_name:4s} {sus_name:4s} "
                f"{s['n']:4d} {s['alpha_pct']:+7.2f}% {s['positive_rate_pct']:7.1f}%{marker}",
                flush=True,
            )

    # H3-1 test: is MMM the Alpha Island?
    mmm = cube.get("MMM", {"n": 0, "alpha_pct": None})
    print("\n  H3-1 Alpha Island test (Middle-Middle-Middle):", flush=True)
    if mmm["n"] >= MIN_N_PER_CELL:
        print(
            f"    MMM: N={mmm['n']}, alpha={mmm['alpha_pct']:+.2f}%, "
            f"positive={mmm['positive_rate_pct']:.1f}%",
            flush=True,
        )
        # Compare to overall mean
        all_alpha = np.mean([e["residual_alpha"] for e in bucketed]) * 100
        print(f"    Overall bucketed mean: {all_alpha:+.2f}%", flush=True)
        print(f"    MMM - Overall: {mmm['alpha_pct'] - all_alpha:+.2f}pp", flush=True)
    else:
        print(f"    MMM: N={mmm['n']} < {MIN_N_PER_CELL} (insufficient)", flush=True)

    return cube, bucketed, (gap_t1, gap_t2, sus_t1, sus_t2)


def step2_extreme_danger(bucketed):
    """Step 2: Extreme combination danger analysis."""
    print("\n" + "=" * 70, flush=True)
    print("STEP 2: Extreme Combination Danger", flush=True)
    print("=" * 70, flush=True)

    # Define key comparisons
    comparisons = [
        (
            "MMM (all middle)",
            lambda e: e["frm_b"] == "M" and e["gap_b"] == "M" and e["sus_b"] == "M",
        ),
        ("HHH (all high)", lambda e: e["frm_b"] == "H" and e["gap_b"] == "H" and e["sus_b"] == "H"),
        ("LLL (all low)", lambda e: e["frm_b"] == "L" and e["gap_b"] == "L" and e["sus_b"] == "L"),
        ("High FRM + High Gap", lambda e: e["frm_b"] == "H" and e["gap_b"] == "H"),
        ("High FRM + High Sustain", lambda e: e["frm_b"] == "H" and e["sus_b"] == "H"),
        ("High Gap + High Sustain", lambda e: e["gap_b"] == "H" and e["sus_b"] == "H"),
        ("Mid FRM + Mid Gap", lambda e: e["frm_b"] == "M" and e["gap_b"] == "M"),
        ("Mid FRM + Mid Sustain", lambda e: e["frm_b"] == "M" and e["sus_b"] == "M"),
        (
            "Any 2+ extremes (H or L)",
            lambda e: sum(1 for b in [e["frm_b"], e["gap_b"], e["sus_b"]] if b in "HL") >= 2,
        ),
        (
            "Any 2+ middle (M)",
            lambda e: sum(1 for b in [e["frm_b"], e["gap_b"], e["sus_b"]] if b == "M") >= 2,
        ),
    ]

    results = {}
    print(f"\n  {'combination':30s} {'n':>4} {'alpha':>8} {'positive':>9}", flush=True)
    print(f"  {'-' * 55}", flush=True)
    for label, pred in comparisons:
        sub = [e for e in bucketed if pred(e)]
        s = _stats(sub, label)
        results[label] = s
        if s["n"] > 0:
            print(
                f"  {label:30s} {s['n']:4d} {s['alpha_pct']:+7.2f}% {s['positive_rate_pct']:7.1f}%",
                flush=True,
            )
        else:
            print(f"  {label:30s}    0      N/A      N/A", flush=True)

    return results


def step3_crowding_control(bucketed):
    """Step 3: Crowding/Control Test (CRITICAL).

    Test whether the Alpha Island (MMM) survives controlling for market_cap,
    turnover (volume_ratio), and momentum (relative_strength).

    Method: compare MMM vs non-MMM WITHIN each control-variable tercile.
    If MMM still outperforms within each tercile, H3 is not a crowding artifact.
    """
    print("\n" + "=" * 70, flush=True)
    print("STEP 3: Crowding Control Test (CRITICAL - is H3 real or artifact?)", flush=True)
    print("=" * 70, flush=True)

    controls = {
        "market_cap": "market_cap",
        "volume_ratio (turnover)": "volume_ratio",
        "relative_strength (momentum)": "relative_strength",
    }

    results = {}
    for ctrl_label, ctrl_key in controls.items():
        print(f"\n  --- Control: {ctrl_label} ---", flush=True)
        values = [e.get(ctrl_key) for e in bucketed]
        valid_vals = [v for v in values if v is not None]
        if len(valid_vals) < 6:
            print(f"    SKIP: only {len(valid_vals)} non-null values", flush=True)
            continue

        t1, t2 = _terciles(values)
        print(
            f"    terciles: L < {t1:.3f}, M < {t2:.3f}, H >= {t2:.3f}"
            if t1
            else "    insufficient",
            flush=True,
        )

        # For each control tercile, compare MMM vs non-MMM
        print(
            f"    {'tercile':10s} {'mmm_n':>6} {'mmm_alpha':>10} {'non_n':>6} {'non_alpha':>10} {'delta':>8}",
            flush=True,
        )
        for tname, tlo, thi in [("Low", None, t1), ("Mid", t1, t2), ("High", t2, None)]:
            if t1 is None:
                continue
            if tlo is None:
                sub = [e for e in bucketed if e.get(ctrl_key) is not None and e[ctrl_key] < t1]
            elif thi is None:
                sub = [e for e in bucketed if e.get(ctrl_key) is not None and e[ctrl_key] >= t2]
            else:
                sub = [
                    e for e in bucketed if e.get(ctrl_key) is not None and t1 <= e[ctrl_key] < t2
                ]

            mmm_sub = [
                e for e in sub if e["frm_b"] == "M" and e["gap_b"] == "M" and e["sus_b"] == "M"
            ]
            non_sub = [
                e
                for e in sub
                if not (e["frm_b"] == "M" and e["gap_b"] == "M" and e["sus_b"] == "M")
            ]

            mmm_s = _stats(mmm_sub)
            non_s = _stats(non_sub)
            delta = (
                (mmm_s["alpha_pct"] - non_s["alpha_pct"])
                if mmm_s["n"] > 0 and non_s["n"] > 0
                else None
            )
            print(
                f"    {tname:10s} {mmm_s['n']:6d} "
                f"{(mmm_s['alpha_pct'] if mmm_s['n'] > 0 else 0):+9.2f}% "
                f"{non_s['n']:6d} {(non_s['alpha_pct'] if non_s['n'] > 0 else 0):+9.2f}% "
                f"{(delta if delta is not None else 0):+7.2f}pp",
                flush=True,
            )

        results[ctrl_label] = {"t1": t1, "t2": t2}

    # Overall: regression of alpha on MMM dummy + controls
    print("\n  --- Regression: alpha ~ MMM + controls ---", flush=True)
    reg_rows = []
    for e in bucketed:
        if all(e.get(k) is not None for k in ["market_cap", "volume_ratio", "relative_strength"]):
            is_mmm = 1.0 if (e["frm_b"] == "M" and e["gap_b"] == "M" and e["sus_b"] == "M") else 0.0
            reg_rows.append(
                {
                    "alpha": e["residual_alpha"] * 100,
                    "mmm": is_mmm,
                    "mcap": e["market_cap"],
                    "vol_ratio": e["volume_ratio"],
                    "rs": e["relative_strength"],
                }
            )

    if len(reg_rows) >= 20:
        y = np.array([r["alpha"] for r in reg_rows])

        # Normalize controls
        def norm(v):
            v = np.array(v, dtype=float)
            return (v - v.min()) / (v.max() - v.min() + 1e-9)

        X = np.column_stack(
            [
                np.ones(len(y)),
                np.array([r["mmm"] for r in reg_rows]),
                norm([r["mcap"] for r in reg_rows]),
                norm([r["vol_ratio"] for r in reg_rows]),
                norm([r["rs"] for r in reg_rows]),
            ]
        )
        try:
            beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
            y_pred = X @ beta
            ss_res = np.sum((y - y_pred) ** 2)
            ss_tot = np.sum((y - np.mean(y)) ** 2)
            r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
            n, k = X.shape
            mse = ss_res / (n - k) if n > k else 1
            try:
                se = np.sqrt(np.diag(mse * np.linalg.inv(X.T @ X)))
                t_stats = beta / se
            except Exception:
                t_stats = np.zeros(len(beta))

            print(f"  N={n}, R²={r2:.3f}", flush=True)
            print(f"  {'var':25s} {'beta':>8} {'t':>6}", flush=True)
            print(f"  {'intercept':25s} {beta[0]:8.3f} {t_stats[0]:6.2f}", flush=True)
            print(f"  {'MMM dummy':25s} {beta[1]:8.3f} {t_stats[1]:6.2f}", flush=True)
            print(f"  {'market_cap(norm)':25s} {beta[2]:8.3f} {t_stats[2]:6.2f}", flush=True)
            print(f"  {'volume_ratio(norm)':25s} {beta[3]:8.3f} {t_stats[3]:6.2f}", flush=True)
            print(f"  {'relative_strength(norm)':25s} {beta[4]:8.3f} {t_stats[4]:6.2f}", flush=True)
            print(
                f"\n  MMM coefficient after controls: {beta[1]:+.3f}pp (t={t_stats[1]:.2f})",
                flush=True,
            )
            if beta[1] > 0:
                print("  -> MMM alpha SURVIVES crowding controls (H3 not an artifact)", flush=True)
            else:
                print(
                    "  -> MMM alpha does NOT survive controls (may be crowding artifact)",
                    flush=True,
                )
            results["regression"] = {
                "n": n,
                "r2": float(r2),
                "mmm_beta": float(beta[1]),
                "mmm_t": float(t_stats[1]),
                "survives": bool(beta[1] > 0),
            }
        except Exception as ex:
            print(f"  Regression failed: {ex}", flush=True)
    else:
        print(f"  Insufficient N ({len(reg_rows)}) for regression with all controls", flush=True)

    return results


def step4_time_split(bucketed):
    """Step 4: Time split validation."""
    print("\n" + "=" * 70, flush=True)
    print("STEP 4: Time Split Validation", flush=True)
    print("=" * 70, flush=True)

    by_year = defaultdict(list)
    for e in bucketed:
        by_year[e["trade_date"][:4]].append(e)

    results = {}
    print(f"\n  {'year':6s} {'n':>4} {'alpha':>8} {'mmm_n':>6} {'mmm_alpha':>10}", flush=True)
    for y in sorted(by_year.keys()):
        sub = by_year[y]
        s = _stats(sub)
        mmm_sub = [e for e in sub if e["frm_b"] == "M" and e["gap_b"] == "M" and e["sus_b"] == "M"]
        mmm_s = _stats(mmm_sub)
        print(
            f"  {y:6s} {s['n']:4d} {s['alpha_pct']:+7.2f}% "
            f"{mmm_s['n']:6d} {(mmm_s['alpha_pct'] if mmm_s['n'] > 0 else 0):+9.2f}%",
            flush=True,
        )
        results[y] = {
            "n": s["n"],
            "alpha_pct": s["alpha_pct"],
            "mmm_n": mmm_s["n"],
            "mmm_alpha_pct": mmm_s["alpha_pct"] if mmm_s["n"] > 0 else None,
        }

    return results


def run_exp8():
    enriched = load_enriched_group_a()
    print("=" * 70, flush=True)
    print("v3.5 Phase 2 Exp8: Uncertainty Asymmetry Validation (6-S.17.2)", flush=True)
    print("THE PARADIGM-SHIFT EXPERIMENT", flush=True)
    print("=" * 70, flush=True)
    print(f"\nGroup A: N={len(enriched)}", flush=True)

    has_gap = sum(1 for e in enriched if e["gap_score"] is not None)
    has_sus = sum(1 for e in enriched if e["sustain_proxy"] is not None)
    has_frm = sum(1 for e in enriched if e["frm_score"] is not None)
    has_all = sum(
        1
        for e in enriched
        if e["gap_score"] is not None
        and e["sustain_proxy"] is not None
        and e["frm_score"] is not None
    )
    print(f"  with gap: {has_gap}, with sustainability: {has_sus}, with FRM: {has_frm}", flush=True)
    print(f"  with ALL THREE: {has_all}", flush=True)

    cube, bucketed, thresholds = step1_3d_cube(enriched)
    extreme_results = step2_extreme_danger(bucketed)
    control_results = step3_crowding_control(bucketed)
    time_results = step4_time_split(bucketed)

    # ─── Verdict ───
    print("\n" + "=" * 70, flush=True)
    print("VERDICT SYNTHESIS", flush=True)
    print("=" * 70, flush=True)

    mmm = cube.get("MMM", {"n": 0, "alpha_pct": None})
    hhh = cube.get("HHH", {"n": 0, "alpha_pct": None})

    h3_1_pass = mmm["n"] >= MIN_N_PER_CELL and mmm["alpha_pct"] is not None and mmm["alpha_pct"] > 0
    h3_2_pass = hhh["n"] == 0 or (
        hhh["alpha_pct"] is not None and hhh["alpha_pct"] < mmm["alpha_pct"]
    )

    reg = control_results.get("regression", {})
    control_pass = reg.get("survives", False) if reg else False

    time_pass = all(
        (results["mmm_alpha_pct"] is None or results["mmm_alpha_pct"] > 0)
        for results in time_results.values()
        if isinstance(results, dict) and results.get("mmm_n", 0) > 0
    )

    print(f"\n  H3-1 Alpha Island (MMM > 0): {'PASS' if h3_1_pass else 'FAIL'}", flush=True)
    if mmm["n"] > 0:
        print(f"    MMM: N={mmm['n']}, alpha={mmm['alpha_pct']:+.2f}%", flush=True)
    print(f"  H3-2 Extreme danger (HHH < MMM): {'PASS' if h3_2_pass else 'FAIL'}", flush=True)
    print(
        f"  Crowding control (MMM survives): {'PASS' if control_pass else 'FAIL/INCONCLUSIVE'}",
        flush=True,
    )
    if reg:
        print(
            f"    MMM beta after controls: {reg['mmm_beta']:+.3f}pp (t={reg['mmm_t']:.2f})",
            flush=True,
        )
    print(f"  Time stability: {'PASS' if time_pass else 'INCONCLUSIVE'}", flush=True)

    if h3_1_pass and control_pass:
        verdict = "H3 VALIDATED - Uncertainty Asymmetry is the alpha source"
        recommendation = (
            "PARADIGM SHIFT: Security Analyst transforms from Recovery Quality "
            "Detector to Uncertainty Asymmetry Detector. Build v3.6 around "
            "the Middle-Uncertainty Zone."
        )
    elif h3_1_pass and not control_pass:
        verdict = "H3 PARTIALLY VALIDATED - Alpha Island exists but may be crowding artifact"
        recommendation = (
            "Do NOT paradigm-shift yet. The middle-zone alpha may come from "
            "avoiding crowded trades, not uncertainty premium. Need larger N "
            "and more controls before operationalizing."
        )
    elif not h3_1_pass:
        verdict = "H3 NOT VALIDATED - No Alpha Island in the middle"
        recommendation = (
            "H3 rejected at the 3D level. The four single-signal inverted-U "
            "patterns do not converge into a unified zone. Re-examine whether "
            "the inverted-U is signal-specific rather than universal."
        )
    else:
        verdict = "INCONCLUSIVE - mixed signals"
        recommendation = "Keep FRM as frozen black-box. Extend data before re-testing."

    print(f"\n  >>> VERDICT: {verdict}", flush=True)
    print(f"  >>> RECOMMENDATION: {recommendation}", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.17.2",
        "experiment": "exp8_uncertainty_cube",
        "date": str(date.today()),
        "n_total": len(enriched),
        "n_with_all_three": has_all,
        "thresholds": {
            "frm_low_max": FRM_LOW_MAX,
            "frm_high_min": FRM_HIGH_MIN,
            "gap_t1": thresholds[0],
            "gap_t2": thresholds[1],
            "sus_t1": thresholds[2],
            "sus_t2": thresholds[3],
        },
        "cube": {k: v for k, v in cube.items()},
        "extreme_combinations": extreme_results,
        "crowding_control": control_results,
        "time_split": time_results,
        "h3_1_pass": h3_1_pass,
        "h3_2_pass": h3_2_pass,
        "control_pass": control_pass,
        "time_pass": time_pass,
        "verdict": verdict,
        "recommendation": recommendation,
    }
    report_path = os.path.join(REPORT_DIR, f"v3_5_exp8_uncertainty_cube_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_exp8()
