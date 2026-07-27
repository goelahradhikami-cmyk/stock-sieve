"""
v4.0 Gate 1: Self-Knowledge Gate - Commit 6-S.20.1.

THE QUALIFICATION TEST. Does Stock Sieve have the right to manage its
own errors? This is NOT about proving it is smart - it is about proving
it knows when it is NOT smart.

Read-only analysis. Does NOT modify Guardian, confidence, or any model.
Does NOT optimize parameters. Does NOT design Exposure Controller.

Output: Evidence Ladder (structured, avoids single experiment changing identity).

Order (frozen 6-S.20.0):
  Gate 0:   Data Integrity Check
  Gate 0.5: Decision Semantics Check (BUY/BLOCK consistency)
  Exp0E-1:  Stability (yearly calibration spread)
  Exp0E-3:  Regime Conditional (per-state calibration)
  Exp0E-2:  Curve Shape (linear vs inverted-U)
  Exp0E-4:  Half-life (calibration decay)

Usage:
    python scripts/run_gate1_exp0e.py
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


def load_decision_ledger():
    """Load full decision ledger: BLOCK counterfactuals + BUY actuals."""
    con = sqlite3.connect(SHADOW_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT e.episode_id, e.trade_date, e.market_state, e.confidence,
                  e.confidence_band, e.decision, e.position_target,
                  e.vol_20d, e.breadth, e.trend_ma60, e.recovery_prob,
                  o.portfolio_return_t20, o.market_return_t20, o.alpha_vs_hs300
           FROM shadow_episode e
           JOIN shadow_outcome o ON o.episode_id = e.episode_id
           WHERE (e.decision='BLOCK' AND o.market_return_t20 != 0)
              OR (e.decision='BUY' AND o.portfolio_return_t20 != 0)
           ORDER BY e.episode_id"""
    ).fetchall()
    con.close()

    enriched = []
    for r in rows:
        e = dict(r)
        # Unified outcome: for BLOCK, outcome = -market_return (positive = correct to block)
        # For BUY, outcome = portfolio_return (positive = correct to buy)
        if e["decision"] == "BLOCK":
            e["outcome"] = -(e["market_return_t20"] or 0)
        else:
            e["outcome"] = e["portfolio_return_t20"] or 0
        e["year"] = e["trade_date"][:4] if e["trade_date"] else "unknown"
        enriched.append(e)
    return enriched


def gate_0_data_integrity(episodes):
    """Gate 0: Data Integrity Check."""
    print("=" * 70, flush=True)
    print("GATE 0: Data Integrity Check", flush=True)
    print("=" * 70, flush=True)

    checks = {}
    checks["total_episodes"] = len(episodes)
    blocks = [e for e in episodes if e["decision"] == "BLOCK"]
    buys = [e for e in episodes if e["decision"] == "BUY"]
    checks["block_count"] = len(blocks)
    checks["buy_count"] = len(buys)

    # Confidence present
    conf_present = sum(1 for e in episodes if e["confidence"] is not None)
    checks["confidence_coverage"] = f"{conf_present}/{len(episodes)}"
    checks["confidence_pass"] = conf_present == len(episodes)

    # Market state present
    ms_present = sum(1 for e in episodes if e["market_state"] and e["market_state"] != "unknown")
    checks["market_state_coverage"] = f"{ms_present}/{len(episodes)}"
    checks["ms_pass"] = ms_present >= len(episodes) * 0.9

    # Outcome non-null
    outcome_present = sum(1 for e in episodes if e["outcome"] is not None)
    checks["outcome_coverage"] = f"{outcome_present}/{len(episodes)}"
    checks["outcome_pass"] = outcome_present == len(episodes)

    # BUY limitation
    checks["buy_limitation"] = len(buys) < 30
    checks["buy_limitation_note"] = (
        f"BUY N={len(buys)} (limitation, not blocker). BLOCK N={len(blocks)} is primary evidence."
    )

    verdict = checks["confidence_pass"] and checks["ms_pass"] and checks["outcome_pass"]
    checks["verdict"] = "PASS" if verdict else "FAIL"

    print(f"\n  Total episodes: {checks['total_episodes']}", flush=True)
    print(f"  BLOCK: {checks['block_count']}, BUY: {checks['buy_count']}", flush=True)
    print(f"  Confidence coverage: {checks['confidence_coverage']}", flush=True)
    print(f"  Market state coverage: {checks['market_state_coverage']}", flush=True)
    print(f"  Outcome coverage: {checks['outcome_coverage']}", flush=True)
    if checks["buy_limitation"]:
        print(f"  BUY LIMITATION: {checks['buy_limitation_note']}", flush=True)
    print(f"\n  GATE 0 VERDICT: {checks['verdict']}", flush=True)

    return checks


def gate_0_5_decision_semantics(episodes):
    """Gate 0.5: Decision Semantics - is confidence direction consistent in BUY vs BLOCK?"""
    print("\n" + "=" * 70, flush=True)
    print("GATE 0.5: Decision Semantics Check", flush=True)
    print("  Is confidence meaning consistent across BUY and BLOCK?", flush=True)
    print("=" * 70, flush=True)

    buys = [e for e in episodes if e["decision"] == "BUY" and e["confidence"] is not None]
    blocks = [e for e in episodes if e["decision"] == "BLOCK" and e["confidence"] is not None]

    results = {}

    for label, subset in [("BUY", buys), ("BLOCK", blocks)]:
        if len(subset) < 5:
            results[label] = {"n": len(subset), "skipped": True}
            print(f"\n  {label}: N={len(subset)} (insufficient)", flush=True)
            continue

        # Split by confidence median
        confs = [e["confidence"] for e in subset]
        median_conf = np.median(confs)
        high = [e for e in subset if e["confidence"] >= median_conf]
        low = [e for e in subset if e["confidence"] < median_conf]

        high_wr = 100.0 * sum(1 for e in high if e["outcome"] > 0) / len(high) if high else 0
        low_wr = 100.0 * sum(1 for e in low if e["outcome"] > 0) / len(low) if low else 0
        delta = high_wr - low_wr

        results[label] = {
            "n": len(subset),
            "median_conf": float(median_conf),
            "high_conf_win_rate": float(high_wr),
            "low_conf_win_rate": float(low_wr),
            "delta": float(delta),
        }
        print(f"\n  {label} (N={len(subset)}):", flush=True)
        print(f"    High conf win rate: {high_wr:.1f}%", flush=True)
        print(f"    Low conf win rate:  {low_wr:.1f}%", flush=True)
        print(f"    Delta: {delta:+.1f}pp", flush=True)

    # Consistency check: both should have positive delta
    buy_delta = results.get("BUY", {}).get("delta", 0)
    block_delta = results.get("BLOCK", {}).get("delta", 0)
    consistent = buy_delta > 0 and block_delta > 0
    results["consistent"] = bool(consistent)
    results["verdict"] = "CONSISTENT" if consistent else "INCONSISTENT"

    print(f"\n  BUY delta: {buy_delta:+.1f}pp, BLOCK delta: {block_delta:+.1f}pp", flush=True)
    print(f"  GATE 0.5 VERDICT: {results['verdict']}", flush=True)
    if not consistent:
        print("  WARNING: confidence is action-dependent, not a true meta-signal!", flush=True)

    return results


def exp0e_1_stability(episodes):
    """Exp0E-1: Calibration Stability across years."""
    print("\n" + "=" * 70, flush=True)
    print("Exp0E-1: Calibration Stability (yearly spread)", flush=True)
    print("=" * 70, flush=True)

    # Overall quintiles
    confs = [e["confidence"] for e in episodes if e["confidence"] is not None]
    sorted_confs = sorted(confs)
    q1_threshold = sorted_confs[len(sorted_confs) // 5]
    q4_threshold = sorted_confs[4 * len(sorted_confs) // 5]

    def high_conf(e):
        return e["confidence"] is not None and e["confidence"] >= q4_threshold

    def low_conf(e):
        return e["confidence"] is not None and e["confidence"] < q1_threshold

    # Overall
    high_all = [e for e in episodes if high_conf(e)]
    low_all = [e for e in episodes if low_conf(e)]
    high_wr = (
        100.0 * sum(1 for e in high_all if e["outcome"] > 0) / len(high_all) if high_all else 0
    )
    low_wr = 100.0 * sum(1 for e in low_all if e["outcome"] > 0) / len(low_all) if low_all else 0
    overall_delta = high_wr - low_wr
    print(
        f"\n  Overall: high_conf={high_wr:.1f}%, low_conf={low_wr:.1f}%, delta={overall_delta:+.1f}pp",
        flush=True,
    )

    # Yearly
    by_year = defaultdict(list)
    for e in episodes:
        by_year[e["year"]].append(e)

    print(f"\n  {'year':6s} {'n':>4} {'high_wr':>8} {'low_wr':>8} {'delta':>8}", flush=True)
    yearly_results = {}
    deltas = []
    for y in sorted(by_year.keys()):
        sub = by_year[y]
        h = [e for e in sub if high_conf(e)]
        l = [e for e in sub if low_conf(e)]
        if len(h) >= 2 and len(l) >= 2:
            hwr = 100.0 * sum(1 for e in h if e["outcome"] > 0) / len(h)
            lwr = 100.0 * sum(1 for e in l if e["outcome"] > 0) / len(l)
            d = hwr - lwr
            deltas.append(d)
            yearly_results[y] = {
                "n": len(sub),
                "high_wr": float(hwr),
                "low_wr": float(lwr),
                "delta": float(d),
            }
            print(f"  {y:6s} {len(sub):4d} {hwr:7.1f}% {lwr:7.1f}% {d:+7.1f}pp", flush=True)
        else:
            yearly_results[y] = {
                "n": len(sub),
                "skipped": True,
                "note": f"high={len(h)}, low={len(l)}",
            }
            print(
                f"  {y:6s} {len(sub):4d}  (insufficient: high={len(h)}, low={len(l)})", flush=True
            )

    # Criteria
    criteria = {}
    criteria["c1_overall_delta_15"] = overall_delta >= 15
    positive_years = sum(1 for d in deltas if d > 0)
    criteria["c2_temporal_4of6"] = positive_years >= 4
    criteria["c2_positive_years"] = f"{positive_years}/{len(deltas)}"
    catastrophic = any(d < -20 for d in deltas)
    criteria["c4_no_catastrophe"] = not catastrophic

    all_pass = all(criteria.values())
    criteria["verdict"] = "PASS" if all_pass else "FAIL"
    criteria["evidence_level"] = "Stability_candidate" if all_pass else "Replication (unchanged)"

    print("\n  Criteria:", flush=True)
    print(
        f"    1. Overall delta >= 15pp: {'PASS' if criteria['c1_overall_delta_15'] else 'FAIL'} ({overall_delta:+.1f}pp)",
        flush=True,
    )
    print(
        f"    2. 4/6 years positive:   {'PASS' if criteria['c2_temporal_4of6'] else 'FAIL'} ({criteria['c2_positive_years']})",
        flush=True,
    )
    print(
        f"    4. No catastrophic (<-20pp): {'PASS' if criteria['c4_no_catastrophe'] else 'FAIL'}",
        flush=True,
    )
    print(f"\n  Exp0E-1 VERDICT: {criteria['verdict']} -> {criteria['evidence_level']}", flush=True)

    return {"overall_delta": float(overall_delta), "yearly": yearly_results, "criteria": criteria}


def exp0e_3_regime(episodes):
    """Exp0E-3: Regime Conditional Calibration."""
    print("\n" + "=" * 70, flush=True)
    print("Exp0E-3: Regime Conditional Calibration", flush=True)
    print("=" * 70, flush=True)

    confs = [e["confidence"] for e in episodes if e["confidence"] is not None]
    sorted_confs = sorted(confs)
    q1_threshold = sorted_confs[len(sorted_confs) // 5]
    q4_threshold = sorted_confs[4 * len(sorted_confs) // 5]

    by_regime = defaultdict(list)
    for e in episodes:
        if e["market_state"] and e["market_state"] != "unknown":
            by_regime[e["market_state"]].append(e)

    print(f"\n  {'regime':25s} {'n':>4} {'high_wr':>8} {'low_wr':>8} {'delta':>8}", flush=True)
    regime_results = {}
    positive_regimes = 0
    total_regimes = 0
    for regime in sorted(by_regime.keys()):
        sub = by_regime[regime]
        h = [e for e in sub if e["confidence"] is not None and e["confidence"] >= q4_threshold]
        l = [e for e in sub if e["confidence"] is not None and e["confidence"] < q1_threshold]
        if len(h) >= 2 and len(l) >= 2:
            hwr = 100.0 * sum(1 for e in h if e["outcome"] > 0) / len(h)
            lwr = 100.0 * sum(1 for e in l if e["outcome"] > 0) / len(l)
            d = hwr - lwr
            total_regimes += 1
            if d > 0:
                positive_regimes += 1
            regime_results[regime] = {
                "n": len(sub),
                "high_wr": float(hwr),
                "low_wr": float(lwr),
                "delta": float(d),
            }
            print(f"  {regime:25s} {len(sub):4d} {hwr:7.1f}% {lwr:7.1f}% {d:+7.1f}pp", flush=True)
        else:
            regime_results[regime] = {"n": len(sub), "skipped": True}
            print(f"  {regime:25s} {len(sub):4d}  (insufficient)", flush=True)

    pass_2of3 = positive_regimes >= 2
    verdict = "PASS" if pass_2of3 else "FAIL"
    print(f"\n  Positive regimes: {positive_regimes}/{total_regimes}", flush=True)
    print(f"  Exp0E-3 VERDICT: {verdict}", flush=True)

    return {
        "regimes": regime_results,
        "positive_regimes": positive_regimes,
        "total_regimes": total_regimes,
        "verdict": verdict,
    }


def exp0e_2_curve(episodes):
    """Exp0E-2: Calibration Curve Shape."""
    print("\n" + "=" * 70, flush=True)
    print("Exp0E-2: Calibration Curve Shape", flush=True)
    print("=" * 70, flush=True)

    confs = [e["confidence"] for e in episodes if e["confidence"] is not None]
    sorted_confs = sorted(confs)
    n_bins = 10
    bin_size = len(sorted_confs) // n_bins

    print(f"\n  {'bin':4s} {'conf_range':>16} {'n':>4} {'win_rate':>9}", flush=True)
    bins = []
    for bi in range(n_bins):
        start = bi * bin_size
        end = (bi + 1) * bin_size if bi < n_bins - 1 else len(sorted_confs)
        threshold_lo = sorted_confs[start] if start < len(sorted_confs) else 0
        threshold_hi = sorted_confs[min(end - 1, len(sorted_confs) - 1)] if end > 0 else 100

        sub = [
            e
            for e in episodes
            if e["confidence"] is not None and threshold_lo <= e["confidence"] <= threshold_hi
        ]
        if not sub:
            continue
        wr = 100.0 * sum(1 for e in sub if e["outcome"] > 0) / len(sub)
        bins.append(
            {
                "bin": bi + 1,
                "conf_range": [float(threshold_lo), float(threshold_hi)],
                "n": len(sub),
                "win_rate": float(wr),
            }
        )
        print(
            f"  B{bi + 1:1d}   [{threshold_lo:5.1f},{threshold_hi:5.1f}] {len(sub):4d} {wr:7.1f}%",
            flush=True,
        )

    # Determine shape
    if len(bins) >= 5:
        wrs = [b["win_rate"] for b in bins]
        # Check monotonic increase
        monotonic = all(wrs[i] <= wrs[i + 1] + 5 for i in range(len(wrs) - 1))
        # Check inverted-U: middle bins higher than edge bins
        mid_avg = np.mean(wrs[len(wrs) // 3 : 2 * len(wrs) // 3])
        edge_avg = np.mean(wrs[: len(wrs) // 3] + wrs[2 * len(wrs) // 3 :])
        inverted_u = mid_avg > edge_avg + 5

        if monotonic:
            shape = "LINEAR"
        elif inverted_u:
            shape = "INVERTED_U"
        else:
            shape = "NOISE"

        print(f"\n  Shape: {shape}", flush=True)
        if shape == "INVERTED_U":
            print(
                "  -> Q4>Q5 saturation confirmed. Confidence-reliability is non-monotonic.",
                flush=True,
            )
            print(
                "     Fifth inverted-U in the chain (gap, sustainability, FRM, EA, confidence).",
                flush=True,
            )
    else:
        shape = "INSUFFICIENT"

    return {"bins": bins, "shape": shape}


def exp0e_4_half_life(episodes):
    """Exp0E-4: Calibration Half-life."""
    print("\n" + "=" * 70, flush=True)
    print("Exp0E-4: Calibration Half-life", flush=True)
    print("=" * 70, flush=True)

    confs = [e for e in episodes if e["confidence"] is not None]
    sorted_confs = sorted(confs)
    q1_threshold = sorted_confs[len(sorted_confs) // 5]
    q4_threshold = sorted_confs[4 * len(sorted_confs) // 5]

    # Rolling: use each year as test, prior years as train
    by_year = defaultdict(list)
    for e in episodes:
        by_year[e["year"]].append(e)

    years = sorted(by_year.keys())
    print(f"\n  {'test_year':10s} {'train_n':>8} {'test_n':>7} {'delta':>8}", flush=True)

    deltas = []
    for i, test_yr in enumerate(years):
        if i == 0:
            continue  # no training data
        train = []
        for j in range(i):
            train.extend(by_year[years[j]])
        test = by_year[test_yr]

        if len(train) < 10 or len(test) < 5:
            print(f"  {test_yr:10s} {'(insufficient)':>8}", flush=True)
            continue

        # Train thresholds
        train_confs = sorted([e["confidence"] for e in train])
        train_q1 = train_confs[len(train_confs) // 5]
        train_q4 = train_confs[4 * len(train_confs) // 5]

        # Test with train thresholds
        h = [e for e in test if e["confidence"] >= train_q4]
        l = [e for e in test if e["confidence"] < train_q1]
        if len(h) < 2 or len(l) < 2:
            print(
                f"  {test_yr:10s} {len(train):8d} {len(test):7d}  (insufficient split)", flush=True
            )
            continue
        hwr = 100.0 * sum(1 for e in h if e["outcome"] > 0) / len(h)
        lwr = 100.0 * sum(1 for e in l if e["outcome"] > 0) / len(l)
        d = hwr - lwr
        deltas.append({"year": test_yr, "delta": float(d)})
        print(f"  {test_yr:10s} {len(train):8d} {len(test):7d} {d:+7.1f}pp", flush=True)

    if len(deltas) >= 2:
        delta_vals = [d["delta"] for d in deltas]
        trend = "DECAYING" if delta_vals[-1] < delta_vals[0] - 5 else "STABLE"
        print(f"\n  Trend: {trend}", flush=True)
        print(
            f"  First delta: {delta_vals[0]:+.1f}pp, Last delta: {delta_vals[-1]:+.1f}pp",
            flush=True,
        )
        if trend == "DECAYING":
            print("  WARNING: calibration may be decaying. Monitor closely.", flush=True)
    else:
        trend = "INSUFFICIENT"
        print("\n  Insufficient data for half-life estimation.", flush=True)

    return {"rolling_deltas": deltas, "trend": trend}


def run_gate1():
    episodes = load_decision_ledger()
    print("=" * 70, flush=True)
    print("v4.0 Gate 1: Self-Knowledge Gate (6-S.20.1)", flush=True)
    print("Does Stock Sieve have the right to manage its own errors?", flush=True)
    print("=" * 70, flush=True)
    print(f"\nDecision ledger: N={len(episodes)}", flush=True)

    # Gate 0
    g0 = gate_0_data_integrity(episodes)
    if g0["verdict"] != "PASS":
        print("\n  GATE 0 FAILED. Cannot proceed.", flush=True)
        return

    # Gate 0.5
    g05 = gate_0_5_decision_semantics(episodes)

    # Exp0E-1 (most important)
    e1 = exp0e_1_stability(episodes)
    if e1["criteria"]["verdict"] != "PASS":
        print("\n  Exp0E-1 FAILED. Confidence is not stable.", flush=True)
        print(
            "  Stopping. No point testing curve shape or half-life of unstable signal.", flush=True
        )
        evidence_ladder = {
            "gate_0": g0["verdict"],
            "gate_0_5": g05["verdict"],
            "exp0e_1": e1["criteria"]["verdict"],
            "exp0e_1_evidence": e1["criteria"]["evidence_level"],
            "exp0e_3": "SKIPPED (Exp0E-1 failed)",
            "exp0e_2": "SKIPPED",
            "exp0e_4": "SKIPPED",
            "architecture_permission": {"exposure_controller": "BLOCKED"},
        }
    else:
        # Exp0E-3
        e3 = exp0e_3_regime(episodes)
        # Exp0E-2
        e2 = exp0e_2_curve(episodes)
        # Exp0E-4
        e4 = exp0e_4_half_life(episodes)

        all_pass = e1["criteria"]["verdict"] == "PASS" and e3["verdict"] == "PASS"
        evidence_ladder = {
            "gate_0": g0["verdict"],
            "gate_0_5": g05["verdict"],
            "exp0e_1": e1["criteria"]["verdict"],
            "exp0e_1_evidence": e1["criteria"]["evidence_level"],
            "exp0e_3": e3["verdict"],
            "exp0e_3_boundary_map": e3["regimes"],
            "exp0e_2": e2["shape"],
            "exp0e_4": e4["trend"],
            "architecture_permission": {
                "exposure_controller": "ALLOWED (Gate 1 passed, Gate 2 next)"
                if all_pass
                else "BLOCKED",
            },
        }

    # Summary
    print("\n" + "=" * 70, flush=True)
    print("EVIDENCE LADDER (Gate 1 Summary)", flush=True)
    print("=" * 70, flush=True)
    for k, v in evidence_ladder.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                print(f"  {k}.{k2}: {v2}", flush=True)
        else:
            print(f"  {k}: {v}", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.20.1",
        "experiment": "gate1_self_knowledge",
        "date": str(date.today()),
        "evidence_ladder": evidence_ladder,
        "gate_0": g0,
        "gate_0_5": g05,
        "exp0e_1": e1 if "e1" in dir() else None,
        "exp0e_3": e3 if "e3" in dir() else None,
        "exp0e_2": e2 if "e2" in dir() else None,
        "exp0e_4": e4 if "e4" in dir() else None,
    }
    report_path = os.path.join(REPORT_DIR, f"gate1_self_knowledge_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_gate1()
