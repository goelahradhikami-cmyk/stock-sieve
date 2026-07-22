"""
Monthly Belief Audit - Commit 6-S.24.0 (Phase 4 Operational Tool).

The discipline preservation system. Run monthly during Phase 4 paper trading.
Three sections (frozen 6-S.24.0):

  A. Signal Usage Audit: did MUS/ARS change any trading action?
  B. Decision Fingerprint: every allocation change records justification
  C. Constitution Drift Check: automated correlation(position_change, MUS)

This script audits the EXISTING shadow_trading.db as a baseline,
and is designed to be re-run monthly as new episodes accumulate.

Usage:
    python scripts/monthly_belief_audit.py                  # audit all
    python scripts/monthly_belief_audit.py --month 2026-07  # audit specific month
"""

from __future__ import annotations

import os
import sys
import sqlite3
import argparse
import json
from datetime import date, datetime
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
REPORT_DIR = "data/reports"


def load_episodes(month: str | None = None):
    """Load episodes for audit. If month specified (YYYY-MM), filter to that month."""
    con = sqlite3.connect(SHADOW_DB)
    con.row_factory = sqlite3.Row

    if month:
        query = """
            SELECT e.*, o.portfolio_return_t20, o.market_return_t20, o.alpha_vs_hs300
            FROM shadow_episode e
            LEFT JOIN shadow_outcome o ON o.episode_id = e.episode_id
            WHERE e.trade_date LIKE ?
            ORDER BY e.trade_date
        """
        rows = con.execute(query, (f"{month}%",)).fetchall()
    else:
        query = """
            SELECT e.*, o.portfolio_return_t20, o.market_return_t20, o.alpha_vs_hs300
            FROM shadow_episode e
            LEFT JOIN shadow_outcome o ON o.episode_id = e.episode_id
            ORDER BY e.trade_date
        """
        rows = con.execute(query).fetchall()

    con.close()
    return [dict(r) for r in rows]


def section_a_signal_usage_audit(episodes):
    """A. Did MUS (confidence score) change any trading action?

    In the current architecture, MUS should NOT influence the BUY/BLOCK decision.
    The decision comes from Guardian (market_state + confidence_band).
    MUS is a DIAGNOSTIC readout only.

    Check: is there evidence that confidence was used as a trading signal
    rather than just a state descriptor?
    """
    print("\n" + "=" * 70, flush=True)
    print("SECTION A: Signal Usage Audit", flush=True)
    print("  Did MUS (confidence) change any trading action?", flush=True)
    print("=" * 70, flush=True)

    total = len(episodes)
    buy = [e for e in episodes if e["decision"] == "BUY"]
    block = [e for e in episodes if e["decision"] == "BLOCK"]

    print(f"\n  Total episodes: {total}", flush=True)
    print(f"  BUY: {len(buy)}, BLOCK: {len(block)}", flush=True)

    # Check: confidence distribution in BUY vs BLOCK
    # If Guardian uses confidence_band to make decisions, that's BY DESIGN (v1.1).
    # The question is whether confidence is used BEYOND the band (as a continuous signal).
    buy_confs = [e["confidence"] for e in buy if e["confidence"] is not None]
    block_confs = [e["confidence"] for e in block if e["confidence"] is not None]

    if buy_confs:
        print(f"\n  BUY confidence: mean={np.mean(buy_confs):.1f}, range=[{min(buy_confs):.1f}, {max(buy_confs):.1f}]",
              flush=True)
    if block_confs:
        print(f"  BLOCK confidence: mean={np.mean(block_confs):.1f}, range=[{min(block_confs):.1f}, {max(block_confs):.1f}]",
              flush=True)

    # Check: confidence_band usage (this is Guardian's design, not MUS misuse)
    band_counts = defaultdict(lambda: {"BUY": 0, "BLOCK": 0})
    for e in episodes:
        band = e.get("confidence_band", "unknown")
        band_counts[band][e["decision"]] += 1

    print(f"\n  Confidence band -> decision (Guardian design, not MUS misuse):", flush=True)
    print(f"  {'band':10s} {'BUY':>6} {'BLOCK':>6} {'total':>6}", flush=True)
    for band in ["blocked", "small", "normal", "full"]:
        bc = band_counts.get(band, {"BUY": 0, "BLOCK": 0})
        total_band = bc["BUY"] + bc["BLOCK"]
        print(f"  {band:10s} {bc['BUY']:6d} {bc['BLOCK']:6d} {total_band:6d}", flush=True)

    # VIOLATION CHECK: is MUS being used beyond Guardian's band?
    # Guardian v1.1 maps confidence_band -> position_target (normal=0.6, full=1.0).
    # This creates cross-band correlation by DESIGN. The real check is WITHIN band:
    # if position_target varies WITH confidence inside the same band, MUS is leaking.
    violation_found = False
    within_band_results = {}
    for band in ["normal", "full"]:
        band_positions = [(e["confidence"], e["position_target"]) for e in episodes
                          if e["confidence"] is not None and e["position_target"] is not None
                          and e["position_target"] > 0 and e.get("confidence_band") == band]
        if len(band_positions) >= 10:
            confs = np.array([p[0] for p in band_positions])
            targets = np.array([p[1] for p in band_positions])
            if len(set(targets)) > 1:
                corr = float(np.corrcoef(confs, targets)[0, 1])
                within_band_results[band] = {"n": len(band_positions), "corr": corr}
                if abs(corr) > 0.3:
                    print(f"\n  WITHIN-BAND [{band}]: correlation(confidence, position) = {corr:.4f}", flush=True)
                    print(f"  WARNING: MUS is influencing position sizing within {band} band!", flush=True)
                    violation_found = True
                else:
                    print(f"\n  WITHIN-BAND [{band}]: correlation = {corr:.4f} (N={len(band_positions)})", flush=True)
            else:
                within_band_results[band] = {"n": len(band_positions), "corr": 0.0, "constant_target": float(targets[0])}
                print(f"\n  WITHIN-BAND [{band}]: N={len(band_positions)}, all target={targets[0]:.1f} (constant, no MUS influence)", flush=True)
        else:
            print(f"\n  WITHIN-BAND [{band}]: N={len(band_positions)} (insufficient)", flush=True)

    # Cross-band correlation is EXPECTED (Guardian design), report for context only
    all_positions = [(e["confidence"], e["position_target"]) for e in episodes
                     if e["confidence"] is not None and e["position_target"] is not None
                     and e["position_target"] > 0]
    if len(all_positions) >= 20:
        confs = np.array([p[0] for p in all_positions])
        targets = np.array([p[1] for p in all_positions])
        cross_corr = float(np.corrcoef(confs, targets)[0, 1])
        print(f"\n  Cross-band correlation (Guardian design, expected): {cross_corr:.4f}", flush=True)
        print(f"  This is BY DESIGN: band determines position_target (normal=0.6, full=1.0).", flush=True)

    # Summary
    violations = 0 if not violation_found else 1
    verdict = "PASS" if violations == 0 else "VIOLATION"
    print(f"\n  SECTION A VERDICT: {verdict}", flush=True)
    if violations == 0:
        print(f"  MUS did not change trading actions beyond Guardian's design.", flush=True)

    return {"violations": violations, "verdict": verdict,
            "buy_count": len(buy), "block_count": len(block),
            "within_band_results": within_band_results}


def section_b_decision_fingerprint(episodes):
    """B. Decision Fingerprint: what justified each allocation change?

    In the current system, decisions come from:
      - Guardian (market_state + confidence_band -> BUY/BLOCK)
      - FRM direction (improving/stable -> pass, deteriorating -> reject)
      - Portfolio policy (static Bayesian allocation)

    Forbidden justifications:
      - MUS/confidence used as continuous signal
      - Emotion / news / manual override
    """
    print("\n" + "=" * 70, flush=True)
    print("SECTION B: Decision Fingerprint", flush=True)
    print("  What justified each allocation decision?", flush=True)
    print("=" * 70, flush=True)

    # Check reason_codes in episodes
    reason_counts = defaultdict(int)
    for e in episodes:
        reasons = e.get("reason_codes", "[]")
        if reasons and reasons != "[]":
            try:
                rlist = json.loads(reasons)
                for r in rlist:
                    reason_counts[r] += 1
            except (json.JSONDecodeError, TypeError):
                reason_counts["unparseable"] += 1
        else:
            reason_counts["no_reason_code"] += 1

    print(f"\n  Reason code distribution:", flush=True)
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"    {reason:40s}: {count}", flush=True)

    # Check: are there any forbidden reason patterns?
    forbidden_patterns = ["mus", "emotion", "news", "manual", "override", "hunch", "feeling"]
    forbidden_found = []
    for reason in reason_counts:
        for pattern in forbidden_patterns:
            if pattern in reason.lower():
                forbidden_found.append(reason)
                break

    if forbidden_found:
        print(f"\n  FORBIDDEN REASONS FOUND: {forbidden_found}", flush=True)
        verdict = "VIOLATION"
    else:
        print(f"\n  No forbidden reason patterns detected.", flush=True)
        verdict = "PASS"

    # Check position_target distribution (should be from band, not MUS)
    buy_targets = [e["position_target"] for e in episodes
                   if e["decision"] == "BUY" and e["position_target"] is not None]
    if buy_targets:
        unique_targets = sorted(set(buy_targets))
        print(f"\n  BUY position_target values: {unique_targets[:10]}", flush=True)
        if len(unique_targets) <= 5:
            print(f"  OK: discrete targets (from confidence_band, not continuous MUS).", flush=True)
        else:
            print(f"  WARN: many unique targets. Check if continuous MUS is sizing positions.", flush=True)

    print(f"\n  SECTION B VERDICT: {verdict}", flush=True)

    return {"verdict": verdict, "forbidden_reasons": forbidden_found,
            "reason_counts": dict(reason_counts)}


def section_c_constitution_drift(episodes):
    """C. Constitution Drift Check: automated correlation scan.

    Check if position changes are correlated with MUS (confidence).
    If correlation > 0.3, ALERT: implicit learning may have occurred.
    """
    print("\n" + "=" * 70, flush=True)
    print("SECTION C: Constitution Drift Check", flush=True)
    print("  Automated scan: is position change correlated with MUS?", flush=True)
    print("=" * 70, flush=True)

    # For BUY episodes: check WITHIN-BAND correlation (cross-band is Guardian design)
    buy = [e for e in episodes if e["decision"] == "BUY"
           and e["confidence"] is not None and e["position_target"] is not None]

    if len(buy) < 20:
        print(f"\n  Insufficient BUY episodes ({len(buy)}) for drift check.", flush=True)
        return {"verdict": "INSUFFICIENT_DATA", "n": len(buy)}

    # Cross-band correlation (expected to be high - Guardian design)
    confs_all = np.array([e["confidence"] for e in buy])
    targets_all = np.array([e["position_target"] for e in buy])
    cross_corr = float(np.corrcoef(confs_all, targets_all)[0, 1])
    print(f"\n  N BUY episodes: {len(buy)}", flush=True)
    print(f"  Cross-band correlation (Guardian design): {cross_corr:.4f} (expected high)", flush=True)

    # Within-band correlation (the REAL drift check)
    max_within_corr = 0.0
    drift_found = False
    for band in ["normal", "full"]:
        band_buy = [e for e in buy if e.get("confidence_band") == band]
        if len(band_buy) >= 10:
            confs = np.array([e["confidence"] for e in band_buy])
            targets = np.array([e["position_target"] for e in band_buy])
            if len(set(targets)) > 1:
                corr = float(np.corrcoef(confs, targets)[0, 1])
                print(f"  Within-band [{band}]: corr={corr:.4f} (N={len(band_buy)})", flush=True)
                if abs(corr) > max_within_corr:
                    max_within_corr = abs(corr)
                if abs(corr) > 0.3:
                    drift_found = True
            else:
                print(f"  Within-band [{band}]: N={len(band_buy)}, constant target={targets[0]:.1f} (no drift)", flush=True)

    print(f"\n  Max within-band correlation: {max_within_corr:.4f}", flush=True)
    print(f"  Threshold for ALERT: > 0.3", flush=True)

    if drift_found:
        print(f"  ALERT: within-band correlation > 0.3. MUS may be influencing position sizing.", flush=True)
        verdict = "ALERT"
    elif max_within_corr > 0.1:
        print(f"  CAUTION: weak within-band correlation. Monitor.", flush=True)
        verdict = "CAUTION"
    else:
        print(f"  OK: no drift detected. Position comes from band, not continuous MUS.", flush=True)
        verdict = "PASS"

    # Monthly trend: within-band correlation over time
    by_month = defaultdict(list)
    for e in buy:
        month_key = e["trade_date"][:7]
        by_month[month_key].append((e["confidence"], e["position_target"], e.get("confidence_band", "")))

    print(f"\n  Monthly cross-band correlation (context):", flush=True)
    print(f"  {'month':8s} {'n':>4} {'cross_corr':>10}", flush=True)
    trend_data = []
    for month_key in sorted(by_month.keys()):
        pairs = by_month[month_key]
        if len(pairs) >= 5:
            c = np.array([p[0] for p in pairs])
            t = np.array([p[1] for p in pairs])
            if len(set(t)) > 1:
                m_corr = float(np.corrcoef(c, t)[0, 1])
            else:
                m_corr = 0.0
            print(f"  {month_key:8s} {len(pairs):4d} {m_corr:+9.4f}", flush=True)
            trend_data.append({"month": month_key, "n": len(pairs), "cross_corr": m_corr})

    print(f"\n  SECTION C VERDICT: {verdict}", flush=True)

    return {"verdict": verdict, "cross_band_corr": cross_corr,
            "max_within_band_corr": float(max_within_corr), "monthly_trend": trend_data}


def run_audit(month: str | None = None):
    episodes = load_episodes(month)
    period = month if month else "all-time"
    print("=" * 70, flush=True)
    print(f"Monthly Belief Audit - {period}", flush=True)
    print(f"Date: {date.today()}", flush=True)
    print("=" * 70, flush=True)
    print(f"\nEpisodes in scope: {len(episodes)}", flush=True)

    if len(episodes) == 0:
        print("No episodes to audit.", flush=True)
        return

    # Run three sections
    a = section_a_signal_usage_audit(episodes)
    b = section_b_decision_fingerprint(episodes)
    c = section_c_constitution_drift(episodes)

    # Summary
    print("\n" + "=" * 70, flush=True)
    print("BELIEF AUDIT SUMMARY", flush=True)
    print("=" * 70, flush=True)
    print(f"\n  Section A (Signal Usage):      {a['verdict']}", flush=True)
    print(f"  Section B (Decision Fingerprint): {b['verdict']}", flush=True)
    print(f"  Section C (Constitution Drift):    {c['verdict']}", flush=True)

    total_violations = a["violations"] + (1 if b["verdict"] == "VIOLATION" else 0) + (1 if c["verdict"] == "ALERT" else 0)
    overall = "PASS" if total_violations == 0 else "VIOLATION"

    print(f"\n  OVERALL: {overall}", flush=True)
    print(f"  Violations: {total_violations}", flush=True)

    if overall == "PASS":
        print(f"\n  Constitution held. MUS did not influence capital decisions.", flush=True)
        print(f"  Continue paper trading.", flush=True)
    else:
        print(f"\n  CONSTITUTION VIOLATION DETECTED.", flush=True)
        print(f"  STOP. Review why discipline broke.", flush=True)
        print(f"  Log incident. Reset before continuing.", flush=True)

    # Export
    os.makedirs(REPORT_DIR, exist_ok=True)
    report = {
        "audit_date": str(date.today()),
        "period": period,
        "n_episodes": len(episodes),
        "section_a": a,
        "section_b": b,
        "section_c": c,
        "overall": overall,
        "violations": total_violations,
    }
    report_path = os.path.join(REPORT_DIR, f"belief_audit_{period}_{date.today()}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Report: {report_path}", flush=True)

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monthly Belief Audit")
    parser.add_argument("--month", help="Specific month (YYYY-MM). Default: all-time.")
    args = parser.parse_args()
    run_audit(args.month)
