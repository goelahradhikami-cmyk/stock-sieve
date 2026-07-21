"""
Generate v3 Candidate Snapshot - Commit 6-S.13.6 Step 1.

Runs the v3 CandidateGenerator on all BUY episodes and persists the
candidates to shadow_candidates_v3. This creates the independent audit
trail needed for v3.2 attribution backfill and A/B comparison.

Three A/B groups are tagged:
  v3_recovery    - v3 candidates (Stage 1 + Stage 2 + Stage 3, full funnel)
  v3_recovery_rs - same as v3_recovery (RS is part of the funnel)
  v2_anomaly     - v1/v2 candidates from shadow_candidates (for comparison)

Note: v3_recovery and v3_recovery_rs are the same candidates in v3 (the
funnel includes RS by default). The distinction matters when we want to
isolate Stage 1-only vs Stage 1+2 effect, which we handle in the A/B
analysis script by filtering on rs_data_available.

Usage:
    python scripts/generate_v3_candidate_snapshot.py
    python scripts/generate_v3_candidate_snapshot.py --episode E20240829
"""

from __future__ import annotations

import os
import sys
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.candidate_generator import CandidateGenerator
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
V3_TOP_N = 20  # persist top 20 per episode (more than v2's 5, for analysis)


def generate(episode_filter: str | None = None):
    gen = CandidateGenerator()
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row

    if episode_filter:
        episodes = conn.execute(
            "SELECT episode_id, trade_date, market_state "
            "FROM shadow_episode WHERE episode_id = ? "
            "AND decision='BUY' AND status='evaluated'",
            (episode_filter,),
        ).fetchall()
    else:
        episodes = conn.execute(
            "SELECT episode_id, trade_date, market_state "
            "FROM shadow_episode "
            "WHERE decision='BUY' AND status='evaluated' "
            "ORDER BY trade_date"
        ).fetchall()

    print(f"BUY episodes to process: {len(episodes)}", flush=True)

    total_candidates = 0
    episodes_with_candidates = 0

    for i, ep in enumerate(episodes):
        try:
            candidates = gen.generate(
                ep["trade_date"], ep["market_state"],
                top_n=V3_TOP_N, episode_id=ep["episode_id"],
            )
        except Exception as e:
            logger.warning("v3 snapshot %s failed: %s", ep["episode_id"], e)
            continue

        if not candidates:
            continue

        episodes_with_candidates += 1
        for rank, c in enumerate(candidates, 1):
            v3 = c.v3_features
            if not v3:
                continue

            # Determine ab_group: all v3 candidates go through the full
            # funnel (recovery + RS + mispricing). We tag them as
            # 'v3_recovery_rs'. The A/B script will further split by
            # rs_data_available to isolate Stage 1-only effect.
            ab_group = "v3_recovery_rs"

            conn.execute(
                """INSERT INTO shadow_candidates_v3
                   (episode_id, trade_date, security_id, funnel_rank,
                    frm_direction, frm_score, earnings_acceleration,
                    recovery_score, liquidity_pass, volume_ratio,
                    relative_strength, sector_strength, rs_score, rs_data_available,
                    divergence_score, price_drawdown_12m, market_pessimism,
                    business_strength, ab_group)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ep["episode_id"], ep["trade_date"], c.code, rank,
                    v3.frm_direction, v3.frm_score, v3.earnings_acceleration,
                    v3.recovery_score,
                    1 if v3.liquidity_pass else 0,
                    None,  # volume_ratio not stored in features; could add
                    v3.relative_strength, v3.sector_strength,
                    v3.rs_score,
                    1 if v3.relative_strength is not None else 0,
                    c.divergence_score, c.price_drawdown_12m,
                    c.market_pessimism, c.business_strength,
                    ab_group,
                ),
            )
            total_candidates += 1

        conn.commit()
        if (i + 1) % 20 == 0:
            print(f"  ... {i+1}/{len(episodes)} processed "
                  f"({total_candidates} candidates so far)", flush=True)

    conn.commit()
    print(f"\nDone: {episodes_with_candidates} episodes, "
          f"{total_candidates} v3 candidates persisted", flush=True)

    # Summary
    print("\n=== v3 Candidate Summary ===", flush=True)
    for r in conn.execute(
        "SELECT frm_direction, COUNT(*) c, AVG(recovery_score) avg_rec "
        "FROM shadow_candidates_v3 GROUP BY frm_direction ORDER BY c DESC"
    ).fetchall():
        print(f"  {r['frm_direction']:14s}: {r['c']:5d}  "
              f"avg_recovery={r['avg_rec']:.1f}", flush=True)

    # RS data availability
    total = conn.execute(
        "SELECT COUNT(*) FROM shadow_candidates_v3").fetchone()[0]
    with_rs = conn.execute(
        "SELECT COUNT(*) FROM shadow_candidates_v3 "
        "WHERE rs_data_available=1").fetchone()[0]
    print(f"\n  RS data available: {with_rs}/{total} "
          f"({with_rs/max(1,total)*100:.1f}%)", flush=True)

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Generate v3 Candidate Snapshot (6-S.13.6 Step 1)")
    parser.add_argument("--episode", type=str, default=None)
    args = parser.parse_args()
    generate(episode_filter=args.episode)


if __name__ == "__main__":
    main()
