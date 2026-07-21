"""
Backfill FRM Scores - Commit 6-S.12.2.

Populates the Fundamental Recovery Momentum fields added by migration v2:
  - frm_score
  - earnings_yoy_current / earnings_yoy_previous
  - earnings_revision_direction
  - frm_earnings_acceleration / frm_margin_change / frm_revenue_acceleration

Vintage-aware: each candidate is scored using only financial reports that
were publicly available on the episode's trade_date.

Usage:
    python scripts/backfill_frm_scores.py
    python scripts/backfill_frm_scores.py --episode E20250813
"""

from __future__ import annotations

import os
import sys
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.fundamental_recovery import FundamentalRecoveryScorer
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"


def backfill(episode_filter: str | None = None):
    scorer = FundamentalRecoveryScorer()
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row

    if episode_filter:
        candidates = conn.execute(
            "SELECT id, episode_id, stock_code FROM shadow_candidates "
            "WHERE episode_id = ? ORDER BY id",
            (episode_filter,),
        ).fetchall()
    else:
        candidates = conn.execute(
            "SELECT id, episode_id, stock_code FROM shadow_candidates "
            "ORDER BY id"
        ).fetchall()

    print(f"Candidates to score: {len(candidates)}", flush=True)

    # Pre-load episode -> (trade_date, market_state) mapping
    ep_map = {}
    for ep in conn.execute(
        "SELECT episode_id, trade_date, market_state FROM shadow_episode"
    ).fetchall():
        ep_map[ep["episode_id"]] = (ep["trade_date"], ep["market_state"])

    scored = 0
    failed = 0
    batch = []
    for i, cand in enumerate(candidates):
        ep_meta = ep_map.get(cand["episode_id"])
        if not ep_meta:
            failed += 1
            continue
        trade_date, market_state = ep_meta
        try:
            r = scorer.compute(cand["stock_code"], trade_date, market_state)
            batch.append((
                r.score,
                r.earnings_yoy_current,
                r.earnings_yoy_previous,
                r.revision_direction,
                r.earnings_acceleration,
                r.margin_stabilization,
                r.revenue_acceleration,
                cand["id"],
            ))
            scored += 1
        except Exception as e:
            failed += 1
            if failed <= 3:
                print(f"  WARN: {cand['stock_code']} @ {trade_date}: {e}",
                      flush=True)

        if len(batch) >= 200:
            conn.executemany(
                """UPDATE shadow_candidates
                   SET frm_score=?, earnings_yoy_current=?, earnings_yoy_previous=?,
                       earnings_revision_direction=?,
                       frm_earnings_acceleration=?, frm_margin_change=?,
                       frm_revenue_acceleration=?
                   WHERE id=?""",
                batch,
            )
            conn.commit()
            batch = []
            print(f"  ... {i+1}/{len(candidates)} scored ({scored} ok, {failed} fail)",
                  flush=True)

    if batch:
        conn.executemany(
            """UPDATE shadow_candidates
               SET frm_score=?, earnings_yoy_current=?, earnings_yoy_previous=?,
                   earnings_revision_direction=?,
                   frm_earnings_acceleration=?, frm_margin_change=?,
                   frm_revenue_acceleration=?
               WHERE id=?""",
            batch,
        )
        conn.commit()

    print(f"\nDone: {scored} scored, {failed} failed", flush=True)

    # Summary
    print("\n=== FRM Distribution ===", flush=True)
    for r in conn.execute(
        "SELECT earnings_revision_direction, COUNT(*) c, "
        "AVG(frm_score) avg_score FROM shadow_candidates "
        "WHERE frm_score IS NOT NULL "
        "GROUP BY earnings_revision_direction ORDER BY c DESC"
    ).fetchall():
        print(f"  {r['earnings_revision_direction']:14s}: {r['c']:5d}  "
              f"avg_frm={r['avg_score']:.1f}", flush=True)

    # Selected stocks FRM summary
    print("\n=== Selected stocks (BUY) FRM ===", flush=True)
    for r in conn.execute(
        "SELECT earnings_revision_direction, COUNT(*) c, "
        "AVG(frm_score) avg_score, AVG(residual_alpha) avg_resid "
        "FROM shadow_candidates c "
        "JOIN shadow_episode e ON e.episode_id = c.episode_id "
        "WHERE c.selected = 1 AND e.decision = 'BUY' "
        "AND c.frm_score IS NOT NULL "
        "GROUP BY earnings_revision_direction ORDER BY c DESC"
    ).fetchall():
        resid = f"{r['avg_resid']:+.4f}" if r['avg_resid'] is not None else "n/a"
        print(f"  {r['earnings_revision_direction']:14s}: {r['c']:3d}  "
              f"avg_frm={r['avg_score']:.1f}  avg_residual_alpha={resid}",
              flush=True)

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Backfill FRM Scores (6-S.12.2)")
    parser.add_argument("--episode", type=str, default=None,
                        help="Process only one episode")
    args = parser.parse_args()
    backfill(episode_filter=args.episode)


if __name__ == "__main__":
    main()
