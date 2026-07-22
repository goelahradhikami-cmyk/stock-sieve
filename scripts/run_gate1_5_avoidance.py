"""
v4.0 Gate 1.5: Avoidance Calibration Test - Commit 6-S.21.0.

THE EXPERIMENT. ARS v1 frozen. Data expanded to N=1043 BLOCK counterfactuals.
Tests whether ARS (Guardian's confidence score, used as Avoidance Reliability
Score) can identify high-regret-probability entries within Guardian's
opportunity set.

Read-only analysis. ARS = Guardian's existing confidence score (v1.1 frozen).
No new model. No tuning. T+20 horizon (Guardrail 3).

Four tests in order (frozen 6-S.20.4):
  Test 1: Avoided Loss (existence - does ARS rank risk?)
  Test 2: Regret Reduction (investment value - does it reduce pain?)
  Test 3: Crisis Robustness (does it work across regimes?)
  Test 4: Opportunity Cost (terminal - does it preserve participation?)

If Test 1 fails, STOP. No point testing the rest.

Three-layer reporting (frozen 6-S.20.7):
  Layer 1: Facts (raw data)
  Layer 2: Statistics (deltas, CIs, sample sizes)
  Layer 3: System meaning (identity implications)

Usage:
    python scripts/run_gate1_5_avoidance.py
"""

from __future__ import annotations

import os
import sys
import sqlite3
import json
from datetime import date
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
REPORT_DIR = "data/reports"


def load_block_ledger():
    """Load ALL BLOCK episodes with counterfactual market returns."""
    con = sqlite3.connect(SHADOW_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT e.episode_id, e.trade_date, e.market_state, e.confidence,
                  e.confidence_band, e.vol_20d, e.breadth, e.trend_ma60,
                  e.recovery_prob, e.position_target,
                  o.market_return_t20, o.alpha_vs_hs300
           FROM shadow_episode e
           JOIN shadow_outcome o ON o.episode_id = e.episode_id
           WHERE e.decision='BLOCK' AND o.market_return_t20 != 0
           ORDER BY e.episode_id"""
    ).fetchall()
    con.close()
    enriched = []
    for r in rows:
        e = dict(r)
        # Outcome for BLOCK: market_return (negative = Guardian was right to block)
        # ARS = confidence score (higher = Guardian more confident in blocking)
        e["outcome"] = e["market_return_t20"]  # raw market return
        e["year"] = e["trade_date"][:4] if e["trade_date"] else "unknown"
        enriched.append(e)
    return enriched


def _stats(values, label=""):
    valid = [v for v in values if v is not None]
    if not valid:
        return {"label": label, "n": 0}
    arr = np.array(valid)
    return {
        "label": label, "n": len(valid),
        "mean_pct": float(np.mean(arr) * 100),
        "median_pct": float(np.median(arr) * 100),
        "win_rate_pct": float(100.0 * np.sum(arr > 0) / len(arr)),
        "p5_pct": float(np.percentile(arr, 5) * 100),
        "p95_pct": float(np.percentile(arr, 95) * 100),
        "std_pct": float(np.std(arr) * 100),
    }


def test_1_avoided_loss(episodes):
    """Test 1: Does high-ARS BLOCK identify episodes where market actually falls more?"""
    print("=" * 70, flush=True)
    print("TEST 1: Avoided Loss (Existence - does ARS rank risk?)", flush=True)
    print("=" * 70, flush=True)

    # Quintile by ARS (confidence)
    confs = [e["confidence"] for e in episodes if e["confidence"] is not None]
    sorted_confs = sorted(confs)
    q1_threshold = sorted_confs[len(sorted_confs) // 5]
    q4_threshold = sorted_confs[4 * len(sorted_confs) // 5]

    high_ars = [e for e in episodes if e["confidence"] is not None and e["confidence"] >= q4_threshold]
    low_ars = [e for e in episodes if e["confidence"] is not None and e["confidence"] < q1_threshold]

    s_high = _stats([e["outcome"] for e in high_ars], "High ARS")
    s_low = _stats([e["outcome"] for e in low_ars], "Low ARS")

    print(f"\n  --- Layer 1: Facts ---", flush=True)
    print(f"  High ARS (Q5, conf >= {q4_threshold:.1f}): N={s_high['n']}", flush=True)
    print(f"    market return: mean={s_high['mean_pct']:+.2f}%, median={s_high['median_pct']:+.2f}%", flush=True)
    print(f"    correct block (mkt<0): {s_high['win_rate_pct']:.1f}% fell, {100-s_high['win_rate_pct']:.1f}% rose", flush=True)
    print(f"  Low ARS (Q1, conf < {q1_threshold:.1f}): N={s_low['n']}", flush=True)
    print(f"    market return: mean={s_low['mean_pct']:+.2f}%, median={s_low['median_pct']:+.2f}%", flush=True)
    print(f"    correct block (mkt<0): {s_low['win_rate_pct']:.1f}% fell, {100-s_low['win_rate_pct']:.1f}% rose", flush=True)

    # Layer 2: Statistics
    delta = s_high["mean_pct"] - s_low["mean_pct"]
    # Bootstrap CI for delta
    rng = np.random.default_rng(42)
    high_outcomes = np.array([e["outcome"] for e in high_ars])
    low_outcomes = np.array([e["outcome"] for e in low_ars])
    boot_deltas = []
    for _ in range(5000):
        h = rng.choice(high_outcomes, size=len(high_outcomes), replace=True)
        l = rng.choice(low_outcomes, size=len(low_outcomes), replace=True)
        boot_deltas.append((np.mean(h) - np.mean(l)) * 100)
    ci_lo = float(np.percentile(boot_deltas, 2.5))
    ci_hi = float(np.percentile(boot_deltas, 97.5))

    print(f"\n  --- Layer 2: Statistics ---", flush=True)
    print(f"  Delta (High ARS - Low ARS market return): {delta:+.2f}pp", flush=True)
    print(f"  95% CI: [{ci_lo:+.2f}pp, {ci_hi:+.2f}pp]", flush=True)
    print(f"  Correct block rate: High ARS {100-s_high['win_rate_pct']:.1f}% vs Low ARS {100-s_low['win_rate_pct']:.1f}%", flush=True)

    # Layer 3: System meaning
    # Success: high-ARS market return < low-ARS by >= 2pp (more negative = more avoided loss)
    # i.e., delta <= -2pp
    passed = delta <= -2.0
    print(f"\n  --- Layer 3: System Meaning ---", flush=True)
    print(f"  Success criterion: delta <= -2.00pp (high ARS blocks are MORE correct)", flush=True)
    print(f"  Actual delta: {delta:+.2f}pp", flush=True)
    print(f"  VERDICT: {'PASS' if passed else 'FAIL'}", flush=True)
    if passed:
        print(f"  ARS has risk information. High-ARS BLOCK episodes have more negative market returns.", flush=True)
    else:
        print(f"  ARS does NOT rank risk. No point testing further.", flush=True)

    return {
        "n_high": s_high["n"], "n_low": s_low["n"],
        "high_mean": s_high["mean_pct"], "low_mean": s_low["mean_pct"],
        "delta": float(delta), "ci": [ci_lo, ci_hi],
        "high_correct_pct": float(100 - s_high["win_rate_pct"]),
        "low_correct_pct": float(100 - s_low["win_rate_pct"]),
        "verdict": "PASS" if passed else "FAIL",
    }


def test_2_regret_reduction(episodes):
    """Test 2: Does Guardian+ARS reduce investor pain vs buy-everything?"""
    print("\n" + "=" * 70, flush=True)
    print("TEST 2: Regret Reduction (Investment Value)", flush=True)
    print("=" * 70, flush=True)

    confs = [e["confidence"] for e in episodes if e["confidence"] is not None]
    sorted_confs = sorted(confs)
    q4_threshold = sorted_confs[4 * len(sorted_confs) // 5]

    # Strategy A: buy everything (all BLOCK counterfactuals = what if we ignored Guardian)
    # Strategy B: Guardian BLOCKED these (so we avoided them)
    # But for ARS test: Strategy C = only block HIGH ARS, buy LOW ARS

    all_returns = np.array([e["outcome"] for e in episodes])
    high_ars_returns = np.array([e["outcome"] for e in episodes if e["confidence"] >= q4_threshold])
    low_ars_returns = np.array([e["outcome"] for e in episodes if e["confidence"] < sorted_confs[len(sorted_confs) // 5]])

    print(f"\n  --- Layer 1: Facts ---", flush=True)
    print(f"  Strategy A (buy all {len(all_returns)} blocked): mean={np.mean(all_returns)*100:+.2f}%", flush=True)
    print(f"  Strategy B (block high-ARS {len(high_ars_returns)}): avoided mean={np.mean(high_ars_returns)*100:+.2f}%", flush=True)
    print(f"  Strategy C (buy low-ARS {len(low_ars_returns)}): mean={np.mean(low_ars_returns)*100:+.2f}%", flush=True)

    # Pain metrics: for blocked episodes, if we HAD bought, what would the pain be?
    # High-ARS avoided pain = -mean(high_ARS_returns) if negative
    # Ulcer Index proxy: for each episode, if market fell, depth = -return, duration = 20 days
    def ulcer_proxy(returns):
        """Simplified Ulcer Index: sqrt(mean(max(0,-r)^2)) * sqrt(20)"""
        depths = np.maximum(0, -returns)
        return float(np.sqrt(np.mean(depths**2)) * np.sqrt(20) * 100)

    ui_high = ulcer_proxy(high_ars_returns)
    ui_low = ulcer_proxy(low_ars_returns)
    ui_all = ulcer_proxy(all_returns)

    print(f"\n  Ulcer Index proxy:", flush=True)
    print(f"    High-ARS blocked (pain avoided): {ui_high:.2f}", flush=True)
    print(f"    Low-ARS blocked (pain if bought): {ui_low:.2f}", flush=True)
    print(f"    All blocked: {ui_all:.2f}", flush=True)

    # Layer 2: Statistics
    delta_ui = ui_high - ui_low
    print(f"\n  --- Layer 2: Statistics ---", flush=True)
    print(f"  Ulcer Index delta (High-ARS - Low-ARS): {delta_ui:+.2f}", flush=True)
    print(f"  If positive: high-ARS episodes have MORE pain (blocking them is more valuable)", flush=True)

    # Layer 3
    # Success: high-ARS pain > low-ARS pain (blocking high-ARS avoids more pain)
    passed = delta_ui > 0
    print(f"\n  --- Layer 3: System Meaning ---", flush=True)
    print(f"  VERDICT: {'PASS' if passed else 'FAIL'}", flush=True)
    if passed:
        print(f"  Blocking high-ARS episodes avoids more pain than blocking low-ARS.", flush=True)
    else:
        print(f"  ARS does not prioritize pain avoidance.", flush=True)

    return {
        "ulcer_high": ui_high, "ulcer_low": ui_low, "ulcer_all": ui_all,
        "delta_ui": float(delta_ui), "verdict": "PASS" if passed else "FAIL",
    }


def test_3_crisis_robustness(episodes):
    """Test 3: Does ARS work across market regimes?"""
    print("\n" + "=" * 70, flush=True)
    print("TEST 3: Crisis Robustness (5 regimes)", flush=True)
    print("=" * 70, flush=True)

    confs = [e["confidence"] for e in episodes if e["confidence"] is not None]
    sorted_confs = sorted(confs)
    q1_threshold = sorted_confs[len(sorted_confs) // 5]
    q4_threshold = sorted_confs[4 * len(sorted_confs) // 5]

    by_regime = defaultdict(list)
    for e in episodes:
        if e["market_state"] and e["market_state"] != "unknown":
            by_regime[e["market_state"]].append(e)

    print(f"\n  --- Layer 1: Facts ---", flush=True)
    print(f"  {'regime':25s} {'n':>5} {'high_mkt':>9} {'low_mkt':>9} {'delta':>8}", flush=True)
    regime_results = {}
    positive_regimes = 0
    total_regimes = 0
    for regime in sorted(by_regime.keys()):
        sub = by_regime[regime]
        h = [e for e in sub if e["confidence"] is not None and e["confidence"] >= q4_threshold]
        l = [e for e in sub if e["confidence"] is not None and e["confidence"] < q1_threshold]
        if len(h) >= 3 and len(l) >= 3:
            h_mean = np.mean([e["outcome"] for e in h]) * 100
            l_mean = np.mean([e["outcome"] for e in l]) * 100
            d = h_mean - l_mean
            total_regimes += 1
            # Positive delta = high-ARS MORE negative = ARS works (blocking high-ARS is more correct)
            # Actually: we want high-ARS market return < low-ARS (more negative)
            # So delta < 0 = ARS works in this regime
            ars_works = d < 0
            if ars_works:
                positive_regimes += 1
            regime_results[regime] = {"n": len(sub), "n_high": len(h), "n_low": len(l),
                                       "high_mean": float(h_mean), "low_mean": float(l_mean),
                                       "delta": float(d), "ars_works": bool(ars_works)}
            print(f"  {regime:25s} {len(sub):5d} {h_mean:+7.2f}% {l_mean:+7.2f}% {d:+7.2f}pp {'✓' if ars_works else '✗'}", flush=True)
        else:
            regime_results[regime] = {"n": len(sub), "skipped": True}
            print(f"  {regime:25s} {len(sub):5d}  (insufficient split)", flush=True)

    print(f"\n  --- Layer 2: Statistics ---", flush=True)
    print(f"  Regimes where ARS works (delta < 0): {positive_regimes}/{total_regimes}", flush=True)

    print(f"\n  --- Layer 3: System Meaning ---", flush=True)
    passed = positive_regimes >= 2
    print(f"  Success: >= 2 regimes with ARS working. Actual: {positive_regimes}/{total_regimes}", flush=True)
    print(f"  VERDICT: {'PASS' if passed else 'FAIL'}", flush=True)

    return {"regimes": regime_results, "positive_regimes": positive_regimes,
            "total_regimes": total_regimes, "verdict": "PASS" if passed else "FAIL"}


def test_4_opportunity_cost(episodes):
    """Test 4: Does ARS preserve participation? (TERMINAL TEST)"""
    print("\n" + "=" * 70, flush=True)
    print("TEST 4: Opportunity Cost (TERMINAL - preserves participation?)", flush=True)
    print("=" * 70, flush=True)

    confs = [e["confidence"] for e in episodes if e["confidence"] is not None]
    sorted_confs = sorted(confs)
    q4_threshold = sorted_confs[4 * len(sorted_confs) // 5]

    high_ars = [e for e in episodes if e["confidence"] is not None and e["confidence"] >= q4_threshold]
    all_returns = [e["outcome"] for e in episodes]

    # False Protection Ratio: blocked episodes where market ROSE
    false_blocks = [e for e in high_ars if e["outcome"] > 0]
    fpr = len(false_blocks) / len(high_ars) if high_ars else 0

    # False Protection Cost: sum of missed upside
    fpc = float(np.sum([e["outcome"] for e in false_blocks]) * 100)  # in pp

    # Block rate
    block_rate = len(high_ars) / len(episodes)

    # Missed upside: mean return of false blocks
    missed_upside = float(np.mean([e["outcome"] for e in false_blocks]) * 100) if false_blocks else 0
    overall_mean = float(np.mean(all_returns) * 100)

    print(f"\n  --- Layer 1: Facts ---", flush=True)
    print(f"  High-ARS blocks: {len(high_ars)}/{len(episodes)} ({block_rate*100:.1f}% block rate)", flush=True)
    print(f"  False blocks (market rose despite high ARS): {len(false_blocks)}", flush=True)
    print(f"  FPR (False Protection Ratio): {fpr*100:.1f}%", flush=True)
    print(f"  FPC (False Protection Cost): {fpc:+.2f}pp total missed", flush=True)
    print(f"  Missed upside (mean of false blocks): {missed_upside:+.2f}%", flush=True)
    print(f"  Overall mean market return: {overall_mean:+.2f}%", flush=True)

    print(f"\n  --- Layer 2: Statistics ---", flush=True)
    print(f"  FPR < 50%: {'PASS' if fpr < 0.5 else 'FAIL'} ({fpr*100:.1f}%)", flush=True)
    print(f"  Missed upside < overall_mean + 3pp: {'PASS' if missed_upside < overall_mean + 3 else 'FAIL'}", flush=True)
    print(f"  Block rate reasonable (< 30%): {'PASS' if block_rate < 0.3 else 'WARN'} ({block_rate*100:.1f}%)", flush=True)

    print(f"\n  --- Layer 3: System Meaning ---", flush=True)
    # Terminal test: FPR < 50% AND missed_upside < overall_mean + 3pp
    # AND no catastrophic (FPR > 70%)
    catastrophic = fpr > 0.70
    passed = (fpr < 0.5) and (missed_upside < overall_mean + 3) and not catastrophic
    print(f"  Terminal verdict: {'PASS' if passed else 'FAIL'}", flush=True)
    if catastrophic:
        print(f"  CATASTROPHIC: FPR > 70%. ARS destroys more opportunity than it protects.", flush=True)
    elif passed:
        print(f"  ARS reduces errors without excessive opportunity sacrifice.", flush=True)
    else:
        print(f"  ARS over-filters. Protection destroys participation (Rule 11 violation).", flush=True)

    return {
        "fpr": float(fpr), "fpc": fpc, "block_rate": float(block_rate),
        "missed_upside": missed_upside, "overall_mean": overall_mean,
        "catastrophic": bool(catastrophic), "verdict": "PASS" if passed else "FAIL",
    }


def run_gate1_5():
    episodes = load_block_ledger()
    print("=" * 70, flush=True)
    print("v4.0 Gate 1.5: Avoidance Calibration Test (6-S.21.0)", flush=True)
    print("ARS v1 frozen. N=1043 BLOCK counterfactuals. T+20 horizon.", flush=True)
    print("Scope: Guardian opportunity set only (Guardrail 1).", flush=True)
    print("=" * 70, flush=True)
    print(f"\nBLOCK episodes with counterfactual: {len(episodes)}", flush=True)

    # Test 1 (existence)
    t1 = test_1_avoided_loss(episodes)
    if t1["verdict"] != "PASS":
        print("\n\n  TEST 1 FAILED. STOPPING. ARS has no risk information.", flush=True)
        evidence = {"test_1": t1, "test_2": "SKIPPED", "test_3": "SKIPPED",
                     "test_4": "SKIPPED", "overall": "FAIL (Scenario C)"}
    else:
        # Test 2 (investment value)
        t2 = test_2_regret_reduction(episodes)
        # Test 3 (crisis robustness)
        t3 = test_3_crisis_robustness(episodes)
        # Test 4 (terminal)
        t4 = test_4_opportunity_cost(episodes)

        n_pass = sum(1 for t in [t1, t2, t3, t4] if t.get("verdict") == "PASS")
        all_pass = n_pass == 4
        no_catastrophe = not t4.get("catastrophic", True)

        if all_pass and no_catastrophe:
            overall = "PASS (Scenario A) - ARS advances to Replication"
        elif n_pass >= 3 and t4["verdict"] != "PASS":
            overall = "PARTIAL (Scenario B) - ARS is risk indicator, NOT portfolio component"
        else:
            overall = f"FAIL ({n_pass}/4 pass)"

        evidence = {"test_1": t1, "test_2": t2, "test_3": t3, "test_4": t4,
                     "n_pass": n_pass, "overall": overall}

    # Summary
    print("\n" + "=" * 70, flush=True)
    print("GATE 1.5 EVIDENCE LADDER", flush=True)
    print("=" * 70, flush=True)
    for k, v in evidence.items():
        if isinstance(v, dict) and "verdict" in v:
            print(f"  {k}: {v['verdict']}", flush=True)
        else:
            print(f"  {k}: {v}", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.21.0",
        "experiment": "gate1_5_avoidance_calibration",
        "date": str(date.today()),
        "n_block": len(episodes),
        "scope": "Guardian opportunity set only (Guardrail 1)",
        "ars_version": "v1 (frozen, Guardian v1.1 confidence score)",
        "horizon": "T+20 (Guardrail 3)",
        "evidence": evidence,
    }
    report_path = os.path.join(REPORT_DIR, f"gate1_5_avoidance_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_gate1_5()
