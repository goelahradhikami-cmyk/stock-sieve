"""
Shadow Trading Episode Recorder - Commit 6-S.10.2.

Runs the frozen Investment Brain v1.0 Defensive Core daily, records every
decision (including BLOCK with counterfactual candidates), and prepares
outcome evaluation for T+20.

This is NOT a trading system. It is a Decision Recording System.
No real trades are executed. No parameters are tuned.

Usage:
    python scripts/shadow_recorder.py --date 2026-07-21
    python scripts/shadow_recorder.py --range 2026-07-01:2026-07-21
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.index_provider import IndexDataProvider
from src.data.local_provider import LocalDataProvider
from src.factors.snapshot_builder import FactorSnapshotBuilder
from src.thesis.confidence_overlay import RecoveryConfidence
from src.thesis.doctrine_underwriting import DoctrineUnderwriter
from src.thesis.market_anomaly import MarketAnomalyDetector
from src.thesis.state_transition import StateTransitionEngine
from src.thesis.thesis_ledger import KillCriteria
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
EVAL_DB = "data/evaluation.db"
CACHE_DB = "data/cache.db"
HORIZON = 20


class ShadowEpisodeRecorder:
    """Records Investment Brain decisions to shadow_trading.db.

    For each trade date:
      1. Run Market Guardian (state + confidence)
      2. Record decision (BUY/BLOCK)
      3. If BUY: record candidate stocks + doctrine explanations
      4. If BLOCK: STILL record candidates (for counterfactual)
      5. Schedule T+20 outcome evaluation
    """

    def __init__(self):
        self.ste = StateTransitionEngine(eval_db=EVAL_DB, cache_db=CACHE_DB)
        self.rc = RecoveryConfidence(eval_db=EVAL_DB, cache_db=CACHE_DB)
        self.detector = MarketAnomalyDetector(cache_db=CACHE_DB, eval_db=EVAL_DB)
        self.uw = DoctrineUnderwriter()
        self.kill = KillCriteria()
        self.builder = FactorSnapshotBuilder()
        self.local = LocalDataProvider()
        self.idx = IndexDataProvider()

        # Build state history (needed for state lookup)
        self.ste.run("2021-08-11", "2026-07-17")

    def record_episode(self, trade_date: str) -> dict:
        """Record one shadow trading episode.

        Returns: episode summary dict
        """
        episode_id = f"E{trade_date.replace('-', '')}"

        # 1. Market Guardian
        state_rec = self.ste.get_state(trade_date)
        market_state = state_rec.state if state_rec else "unknown"

        # 2. Confidence Overlay
        conf = self.rc.compute(trade_date)
        decision = "BUY" if conf.allows_anomaly else "BLOCK"
        position_target = conf.anomaly_weight

        # 3. Reason codes
        reasons = []
        if decision == "BLOCK":
            if conf.confidence < 55:
                reasons.append("LOW_CONFIDENCE")
            if conf.vol_repair < 40:
                reasons.append("VOL_NOT_REPAIRING")
            if conf.trend_confirm < 40:
                reasons.append("TREND_NOT_CONFIRMED")
        else:
            reasons.append("RECOVERY_CONFIRMED")

        # 4. Anomaly detection (run regardless of decision, for counterfactual)
        anomalies = self.detector.scan(trade_date, top_n=50, triage=False)
        significant = [
            a
            for a in anomalies
            if a.price_drawdown_12m < -0.15
            and 0.02 < a.roe < 0.50
            and a.margin_change is not None
            and abs(a.margin_change) < 0.50
        ]

        # 5. Evaluate candidates (kill criteria + doctrine + selection)
        selected_codes = []
        candidate_records = []
        for a in significant[:50]:  # cap for speed
            kill_result = self.kill.check(a)
            uw_results = self.uw.underwrite_all(a)
            consensus = self.uw.consensus(uw_results)

            is_selected = (
                decision == "BUY" and not kill_result.killed and consensus["consensus"] == "PASS"
            )
            if is_selected:
                selected_codes.append(a.code)

            candidate_records.append(
                {
                    "code": a.code,
                    "anomaly_type": a.divergence_type,
                    "price_drawdown_12m": a.price_drawdown_12m,
                    "roe": a.roe,
                    "margin_change": a.margin_change,
                    "market_pessimism": a.market_pessimism,
                    "business_strength": a.business_strength,
                    "divergence_score": a.divergence_score,
                    "confidence": a.confidence,
                    "killed": 1 if kill_result.killed else 0,
                    "kill_reason": kill_result.kill_reason if kill_result.killed else None,
                    "quality_verdict": uw_results.get("quality_compounder").verdict
                    if uw_results.get("quality_compounder")
                    else None,
                    "contrarian_verdict": uw_results.get("contrarian").verdict
                    if uw_results.get("contrarian")
                    else None,
                    "value_verdict": uw_results.get("value_purist").verdict
                    if uw_results.get("value_purist")
                    else None,
                    "selected": 1 if is_selected else 0,
                }
            )

        # 6. Doctrine explanations (for audit trail)
        q_explain = None
        c_explain = None
        v_explain = None
        if significant:
            first = significant[0]
            uw_all = self.uw.underwrite_all(first)
            q_explain = (
                uw_all.get("quality_compounder").verdict
                if uw_all.get("quality_compounder")
                else None
            )
            c_explain = uw_all.get("contrarian").verdict if uw_all.get("contrarian") else None
            v_explain = uw_all.get("value_purist").verdict if uw_all.get("value_purist") else None

        # 7. Find eval_date (T+HORIZON trading days)
        conn = sqlite3.connect(CACHE_DB)
        eval_row = conn.execute(
            "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
            "AND trade_date > ? ORDER BY trade_date LIMIT 1 OFFSET ?",
            (trade_date, HORIZON - 1),
        ).fetchone()
        conn.close()
        eval_date = eval_row[0] if eval_row else None

        # 8. Write to shadow_trading.db
        sconn = sqlite3.connect(SHADOW_DB)
        try:
            # Insert episode
            sconn.execute(
                """
                INSERT OR REPLACE INTO shadow_episode
                (episode_id, trade_date, market_state, confidence, confidence_band,
                 decision, position_target, vol_20d, vol_change, trend_ma60,
                 breadth, recovery_prob, reason_codes,
                 quality_explanation, contrarian_explanation, value_explanation,
                 brain_version, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    episode_id,
                    trade_date,
                    market_state,
                    conf.confidence,
                    conf.confidence_band,
                    decision,
                    position_target,
                    state_rec.vol_20d if state_rec else None,
                    state_rec.vol_change if state_rec else None,
                    state_rec.trend if state_rec else None,
                    conf.breadth_recovery / 100,  # store as 0-1
                    conf.confidence / 100,
                    json.dumps(reasons),
                    q_explain,
                    c_explain,
                    v_explain,
                    "1.0-defensive-core",
                    "pending" if eval_date else "no_eval_date",
                ),
            )

            # Insert candidates
            for c in candidate_records:
                sconn.execute(
                    """
                    INSERT INTO shadow_candidates
                    (episode_id, stock_code, anomaly_type, price_drawdown_12m,
                     roe, margin_change, market_pessimism, business_strength,
                     divergence_score, confidence, killed, kill_reason,
                     quality_verdict, contrarian_verdict, value_verdict, selected)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                    (
                        episode_id,
                        c["code"],
                        c["anomaly_type"],
                        c["price_drawdown_12m"],
                        c["roe"],
                        c["margin_change"],
                        c["market_pessimism"],
                        c["business_strength"],
                        c["divergence_score"],
                        c["confidence"],
                        c["killed"],
                        c["kill_reason"],
                        c["quality_verdict"],
                        c["contrarian_verdict"],
                        c["value_verdict"],
                        c["selected"],
                    ),
                )

            sconn.commit()
        finally:
            sconn.close()

        summary = {
            "episode_id": episode_id,
            "date": trade_date,
            "state": market_state,
            "confidence": conf.confidence,
            "decision": decision,
            "candidates": len(candidate_records),
            "selected": len(selected_codes),
            "reasons": reasons,
            "eval_date": eval_date,
        }
        return summary

    def record_range(self, start_date: str, end_date: str) -> list[dict]:
        """Record episodes for a range of dates (trading days only)."""
        conn = sqlite3.connect(CACHE_DB)
        try:
            rows = conn.execute(
                "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
                "AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
                (start_date, end_date),
            ).fetchall()
        finally:
            conn.close()

        results = []
        for (d,) in rows:
            try:
                # Check if episode already exists
                sconn = sqlite3.connect(SHADOW_DB)
                existing = sconn.execute(
                    "SELECT 1 FROM shadow_episode WHERE episode_id=?",
                    (f"E{d.replace('-', '')}",),
                ).fetchone()
                sconn.close()
                if existing:
                    continue

                summary = self.record_episode(d)
                results.append(summary)
                status = "BUY" if summary["decision"] == "BUY" else "BLOCK"
                print(
                    f"  {d}: {status} conf={summary['confidence']:.1f} "
                    f"candidates={summary['candidates']} selected={summary['selected']} "
                    f"[{', '.join(summary['reasons'])}]",
                    flush=True,
                )
            except Exception as e:
                logger.warning("shadow_recorder: %s failed: %s", d, e)

        return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Shadow Trading Episode Recorder")
    parser.add_argument("--date", type=str, help="Single date (YYYY-MM-DD)")
    parser.add_argument("--range", type=str, help="Date range start:end")
    args = parser.parse_args()

    recorder = ShadowEpisodeRecorder()

    if args.date:
        summary = recorder.record_episode(args.date)
        print(f"Episode: {summary}")
    elif args.range:
        start, end = args.range.split(":")
        print(f"Recording episodes from {start} to {end}...", flush=True)
        results = recorder.record_range(start, end)
        print(f"\nRecorded {len(results)} episodes")
        buy_count = sum(1 for r in results if r["decision"] == "BUY")
        block_count = sum(1 for r in results if r["decision"] == "BLOCK")
        print(f"  BUY: {buy_count}, BLOCK: {block_count}")
    else:
        # Default: today
        today = date.today().isoformat()
        summary = recorder.record_episode(today)
        print(f"Episode: {summary}")


if __name__ == "__main__":
    main()
