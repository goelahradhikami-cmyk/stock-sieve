"""
v4.0 Phase 0 Guardian Autopsy v2 - Commit 6-S.19.5.

Corrected from 6-S.19.2: the original autopsy only looked at BUY episodes
with non-zero portfolio_return (N=8). It MISSED that BLOCK episodes
have counterfactual market_return (N=103). The full decision ledger is:
  - 103 BLOCK episodes with counterfactual (Guardian said don't buy -> market did X)
  - 8 BUY episodes with actual portfolio return
  - Total usable: 111 (meets Data Readiness Contract N>=100)

This re-run uses the FULL decision ledger, not just BUY actuals. The
BLOCK counterfactuals are equally important - they tell us when Guardian
was RIGHT to not act (avoided loss) vs WRONG (missed gain).

Key insight: Guardian's value has TWO dimensions:
  1. BUY alpha: when Guardian says buy, does portfolio beat market?
  2. BLOCK accuracy: when Guardian says block, does market actually fall?
Both must be measured for Exposure Controller.

Usage:
    python scripts/run_guardian_autopsy_v2.py
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


def load_decision_ledger():
    """Load the FULL decision ledger: BLOCK counterfactuals + BUY actuals."""
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
    return [dict(r) for r in rows]


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
        "std_pct": float(np.std(arr) * 100),
        "p5_pct": float(np.percentile(arr, 5) * 100),
        "p95_pct": float(np.percentile(arr, 95) * 100),
    }


def exp_block_accuracy(episodes):
    """BLOCK counterfactual: when Guardian says don't buy, is market actually negative?"""
    print("\n" + "=" * 70, flush=True)
    print("BLOCK Counterfactual Analysis (Guardian said don't buy)", flush=True)
    print("=" * 70, flush=True)

    blocks = [e for e in episodes if e["decision"] == "BLOCK"]
    print(f"\n  BLOCK episodes with counterfactual: {len(blocks)}", flush=True)

    mkts = [e["market_return_t20"] for e in blocks]
    s = _stats(mkts, "BLOCK market return")
    print(f"  Market return: mean={s['mean_pct']:+.2f}%, win_rate(mkt>0)={s['win_rate_pct']:.1f}%", flush=True)
    print(f"  P5={s['p5_pct']:+.2f}%, P95={s['p95_pct']:+.2f}%", flush=True)

    correct = sum(1 for m in mkts if m < 0)
    wrong = sum(1 for m in mkts if m > 0)
    print(f"\n  Guardian BLOCK CORRECT (market fell): {correct}/{len(mkts)} = {100*correct/len(mkts):.1f}%", flush=True)
    print(f"  Guardian BLOCK WRONG (market rose):   {wrong}/{len(mkts)} = {100*wrong/len(mkts):.1f}%", flush=True)

    avoided = [m for m in mkts if m < 0]
    missed = [m for m in mkts if m > 0]
    if avoided:
        print(f"  Avg avoided loss (when correct): {np.mean(avoided)*100:+.2f}%", flush=True)
    if missed:
        print(f"  Avg missed gain (when wrong):    {np.mean(missed)*100:+.2f}%", flush=True)

    # By market_state
    print(f"\n  BLOCK accuracy by market_state:", flush=True)
    print(f"  {'state':25s} {'n':>4} {'mean_mkt':>9} {'correct%':>9}", flush=True)
    by_state = defaultdict(list)
    for e in blocks:
        by_state[e["market_state"]].append(e["market_return_t20"])
    for state in sorted(by_state.keys()):
        vals = by_state[state]
        c = sum(1 for v in vals if v < 0)
        print(f"  {state:25s} {len(vals):4d} {np.mean(vals)*100:+7.2f}% {100*c/len(vals):7.1f}%", flush=True)

    return {
        "n": len(blocks), "correct_rate": float(100*correct/len(mkts)),
        "mean_market": s["mean_pct"],
        "avg_avoided_loss": float(np.mean(avoided)*100) if avoided else None,
        "avg_missed_gain": float(np.mean(missed)*100) if missed else None,
        "by_state": {st: {"n": len(v), "mean": float(np.mean(v)*100),
                          "correct_pct": float(100*sum(1 for x in v if x<0)/len(v))}
                     for st, v in by_state.items()},
    }


def exp_buy_alpha(episodes):
    """BUY actual: when Guardian says buy, does portfolio beat market?"""
    print("\n" + "=" * 70, flush=True)
    print("BUY Actual Analysis (Guardian said buy)", flush=True)
    print("=" * 70, flush=True)

    buys = [e for e in episodes if e["decision"] == "BUY"]
    print(f"\n  BUY episodes with portfolio return: {len(buys)}", flush=True)

    if len(buys) < 3:
        print("  INSUFFICIENT N for BUY analysis", flush=True)
        return {"n": len(buys), "skipped": True}

    rets = [e["portfolio_return_t20"] for e in buys]
    alphas = [e["alpha_vs_hs300"] for e in buys if e["alpha_vs_hs300"] is not None]
    mkts = [e["market_return_t20"] for e in buys if e["market_return_t20"] is not None]

    sr = _stats(rets, "Portfolio return")
    sa = _stats(alphas, "Alpha vs HS300")
    sm = _stats(mkts, "Market return")

    print(f"  Portfolio: mean={sr['mean_pct']:+.2f}%, win={sr['win_rate_pct']:.1f}%", flush=True)
    print(f"  Market:    mean={sm['mean_pct']:+.2f}%, win={sm['win_rate_pct']:.1f}%", flush=True)
    print(f"  Alpha:     mean={sa['mean_pct']:+.2f}%, win={sa['win_rate_pct']:.1f}%", flush=True)
    print(f"\n  Beta attribution:", flush=True)
    print(f"    Portfolio: {sr['mean_pct']:+.2f}%", flush=True)
    print(f"    Beta (market): {sm['mean_pct']:+.2f}%", flush=True)
    print(f"    Alpha: {sa['mean_pct']:+.2f}%", flush=True)
    alpha_share = sa["mean_pct"] / sr["mean_pct"] if abs(sr["mean_pct"]) > 0.01 else 0
    print(f"    Alpha share: {alpha_share*100:.1f}%", flush=True)

    return {
        "n": len(buys),
        "portfolio_return": sr["mean_pct"],
        "market_return": sm["mean_pct"],
        "alpha": sa["mean_pct"],
        "alpha_share": float(alpha_share),
        "win_rate": sr["win_rate_pct"],
    }


def exp_combined_decision_quality(blocks_result, buys_result):
    """Combined: Guardian's overall decision quality (BUY + BLOCK)."""
    print("\n" + "=" * 70, flush=True)
    print("Combined Decision Quality (BUY + BLOCK)", flush=True)
    print("=" * 70, flush=True)

    n_block = blocks_result.get("n", 0)
    n_buy = buys_result.get("n", 0)
    total = n_block + n_buy

    block_correct = blocks_result.get("correct_rate", 0)
    buy_win = buys_result.get("win_rate", 0) if not buys_result.get("skipped") else 0

    print(f"\n  Total decisions: {total} (BLOCK {n_block} + BUY {n_buy})", flush=True)
    print(f"  BLOCK accuracy: {block_correct:.1f}% (market fell when Guardian said block)", flush=True)
    if not buys_result.get("skipped"):
        print(f"  BUY win rate: {buy_win:.1f}% (portfolio positive when Guardian said buy)", flush=True)
        # Overall: weighted average of correct decisions
        block_correct_n = int(n_block * block_correct / 100)
        buy_correct_n = int(n_buy * buy_win / 100)
        overall = 100 * (block_correct_n + buy_correct_n) / total
        print(f"  Overall decision accuracy: {overall:.1f}%", flush=True)
        print(f"    (BLOCK correct: {block_correct_n}/{n_block}, BUY correct: {buy_correct_n}/{n_buy})", flush=True)
    else:
        print(f"  BUY win rate: SKIPPED (N={n_buy} too small)", flush=True)
        overall = block_correct
        print(f"  Overall (BLOCK only): {overall:.1f}%", flush=True)

    return {"total": total, "block_accuracy": block_correct, "buy_win_rate": buy_win,
            "overall": float(overall) if not buys_result.get("skipped") else float(block_correct)}


def exp_confidence_calibration(episodes):
    """Confidence calibration using FULL ledger (BLOCK + BUY)."""
    print("\n" + "=" * 70, flush=True)
    print("Confidence Calibration (FULL ledger)", flush=True)
    print("=" * 70, flush=True)

    # For BLOCK: outcome = -market_return (positive = Guardian was right to block)
    # For BUY: outcome = portfolio_return (positive = Guardian was right to buy)
    calibrated = []
    for e in episodes:
        if e["confidence"] is None:
            continue
        if e["decision"] == "BLOCK":
            outcome = -(e["market_return_t20"] or 0)  # negative market = positive outcome for BLOCK
        else:
            outcome = e["portfolio_return_t20"] or 0
        calibrated.append({"confidence": e["confidence"], "outcome": outcome,
                           "decision": e["decision"]})

    print(f"  N with confidence + outcome: {len(calibrated)}", flush=True)

    if len(calibrated) < 20:
        print("  INSUFFICIENT N for calibration curve", flush=True)
        return {"n": len(calibrated), "skipped": True}

    # Quintile calibration
    calibrated.sort(key=lambda x: x["confidence"])
    n = len(calibrated)
    q = max(n // 5, 1)
    print(f"\n  {'quintile':10s} {'conf_range':>16} {'n':>4} {'mean_outcome':>13} {'win_rate':>9}", flush=True)
    calibration = []
    for qi in range(5):
        start = qi * q
        end = (qi + 1) * q if qi < 4 else n
        sub = calibrated[start:end]
        if not sub:
            continue
        confs = [x["confidence"] for x in sub]
        outs = [x["outcome"] for x in sub]
        wr = 100.0 * sum(1 for o in outs if o > 0) / len(outs)
        print(f"  Q{qi+1:1d}         [{min(confs):5.1f},{max(confs):5.1f}] "
              f"{len(sub):4d} {np.mean(outs)*100:+10.2f}% {wr:7.1f}%", flush=True)
        calibration.append({
            "quintile": qi+1, "conf_range": [float(min(confs)), float(max(confs))],
            "n": len(sub), "mean_outcome_pct": float(np.mean(outs)*100), "win_rate_pct": float(wr),
        })

    if len(calibration) >= 2:
        delta = calibration[-1]["win_rate_pct"] - calibration[0]["win_rate_pct"]
        print(f"\n  Calibration delta (Q5-Q1 win rate): {delta:+.1f}pp", flush=True)
        if delta > 10:
            print(f"  -> CALIBRATED: confidence predicts decision quality", flush=True)
        elif delta > 0:
            print(f"  -> WEAKLY CALIBRATED", flush=True)
        else:
            print(f"  -> NOT CALIBRATED", flush=True)

    return {"calibration": calibration, "n": len(calibrated)}


def run_autopsy_v2():
    episodes = load_decision_ledger()
    print("=" * 70, flush=True)
    print("v4.0 Phase 0 Guardian Autopsy v2 (6-S.19.5)", flush=True)
    print("CORRECTED: uses FULL decision ledger (BLOCK counterfactual + BUY actual)", flush=True)
    print("=" * 70, flush=True)
    print(f"\nTotal usable episodes: {len(episodes)}", flush=True)
    blocks = [e for e in episodes if e["decision"] == "BLOCK"]
    buys = [e for e in episodes if e["decision"] == "BUY"]
    print(f"  BLOCK with counterfactual: {len(blocks)}", flush=True)
    print(f"  BUY with portfolio return: {len(buys)}", flush=True)

    block_result = exp_block_accuracy(episodes)
    buy_result = exp_buy_alpha(episodes)
    combined = exp_combined_decision_quality(block_result, buy_result)
    calibration = exp_confidence_calibration(episodes)

    # Summary
    print("\n" + "=" * 70, flush=True)
    print("AUTOPSY v2 SUMMARY", flush=True)
    print("=" * 70, flush=True)
    print(f"\n  BLOCK accuracy: {block_result['correct_rate']:.1f}% (N={block_result['n']})", flush=True)
    if not buy_result.get("skipped"):
        print(f"  BUY alpha: {buy_result['alpha']:+.2f}% (alpha share {buy_result['alpha_share']*100:.1f}%, N={buy_result['n']})", flush=True)
    print(f"  Combined decisions: {combined['total']}", flush=True)
    if calibration.get("calibration"):
        c = calibration["calibration"]
        if len(c) >= 2:
            print(f"  Confidence calibration: Q5-Q1 delta = {c[-1]['win_rate_pct']-c[0]['win_rate_pct']:+.1f}pp", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "commit": "6-S.19.5",
        "experiment": "guardian_autopsy_v2",
        "date": str(date.today()),
        "n_total": len(episodes),
        "n_block": len(blocks), "n_buy": len(buys),
        "block_counterfactual": block_result,
        "buy_alpha": buy_result,
        "combined_decision_quality": combined,
        "confidence_calibration": calibration,
    }
    report_path = os.path.join(REPORT_DIR, f"guardian_autopsy_v2_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)
    return report


if __name__ == "__main__":
    run_autopsy_v2()
