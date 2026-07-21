"""
Portfolio Construction Evaluator - Commit 6-S.12.4.

Populates the shadow_portfolio_construction table (migration v2) for every
BUY episode. Isolates Layer 3 (portfolio construction) failures from
Layer 2 (selection) failures.

For each BUY episode:
  - candidate_count: stocks passing the anomaly filter
  - selected_count: stocks with selected=1 (the equal-weight basket)
  - top1_weight: max single-stock weight (1/n for equal-weight)
  - herfindahl_index: sum(weight_i^2), 0-1, higher = more concentrated
  - sector_count: distinct industries among selected
  - max_sector_weight: largest sector's basket weight
  - min_positions_pass: selected_count >= 5
  - max_concentration_pass: top1_weight <= 0.25
  - max_sector_pass: max_sector_weight <= 0.40
  - diversification_pass: all three pass
  - failure_reason: which gate(s) failed

Expected: 2025-08-13 will be FAIL_CONCENTRATION (selected_count=1).

Usage:
    python scripts/evaluate_portfolio_construction.py
"""

from __future__ import annotations

import os
import sys
import sqlite3
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"

# Diversification gates
MIN_POSITIONS = 5
MAX_SINGLE_WEIGHT = 0.25
MAX_SECTOR_WEIGHT = 0.40


def evaluate_all():
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row
    cache = sqlite3.connect(CACHE_DB)
    cache.row_factory = sqlite3.Row

    # All BUY episodes
    episodes = conn.execute(
        "SELECT episode_id, trade_date, decision FROM shadow_episode "
        "WHERE decision = 'BUY' AND status = 'evaluated' ORDER BY trade_date"
    ).fetchall()

    print(f"BUY episodes to evaluate: {len(episodes)}", flush=True)

    stats = {
        "total": 0,
        "pass": 0,
        "fail_min_positions": 0,
        "fail_concentration": 0,
        "fail_sector": 0,
    }

    for ep in episodes:
        result = _evaluate_one(conn, cache, ep)
        if result is None:
            continue
        stats["total"] += 1
        if result["diversification_pass"]:
            stats["pass"] += 1
        else:
            if not result["min_positions_pass"]:
                stats["fail_min_positions"] += 1
            if not result["max_concentration_pass"]:
                stats["fail_concentration"] += 1
            if not result["max_sector_pass"]:
                stats["fail_sector"] += 1

        conn.execute(
            """INSERT OR REPLACE INTO shadow_portfolio_construction
               (episode_id, candidate_count, selected_count,
                top1_weight, herfindahl_index, sector_count, max_sector_weight,
                min_positions_pass, max_concentration_pass, max_sector_pass,
                diversification_pass, failure_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ep["episode_id"], result["candidate_count"],
             result["selected_count"], result["top1_weight"],
             result["herfindahl_index"], result["sector_count"],
             result["max_sector_weight"], result["min_positions_pass"],
             result["max_concentration_pass"], result["max_sector_pass"],
             result["diversification_pass"], result["failure_reason"]),
        )

    conn.commit()

    print(f"\n=== Portfolio Construction Summary ===", flush=True)
    print(f"  Total BUY episodes:        {stats['total']}", flush=True)
    print(f"  Diversification PASS:      {stats['pass']} "
          f"({stats['pass']/max(1,stats['total']):.1%})", flush=True)
    print(f"  FAIL_MIN_POSITIONS (<5):   {stats['fail_min_positions']}", flush=True)
    print(f"  FAIL_CONCENTRATION (>25%): {stats['fail_concentration']}", flush=True)
    print(f"  FAIL_SECTOR (>40%):        {stats['fail_sector']}", flush=True)

    # Show the known failure case
    print(f"\n=== Known failure: 2025-08-13 ===", flush=True)
    row = conn.execute(
        "SELECT * FROM shadow_portfolio_construction WHERE episode_id='E20250813'"
    ).fetchone()
    if row:
        print(f"  selected_count: {row['selected_count']}", flush=True)
        print(f"  top1_weight: {row['top1_weight']:.1%}", flush=True)
        print(f"  herfindahl: {row['herfindahl_index']:.3f}", flush=True)
        print(f"  failure_reason: {row['failure_reason']}", flush=True)

    # Distribution of selected_count
    print(f"\n=== selected_count distribution ===", flush=True)
    for r in conn.execute(
        "SELECT selected_count, COUNT(*) c FROM shadow_portfolio_construction "
        "GROUP BY selected_count ORDER BY selected_count"
    ).fetchall():
        print(f"  {r['selected_count']:3d} stocks: {r['c']} episodes", flush=True)

    conn.close()
    cache.close()


def _evaluate_one(conn: sqlite3.Connection, cache: sqlite3.Connection,
                  ep: sqlite3.Row) -> dict | None:
    """Evaluate portfolio construction for one episode."""
    candidates = conn.execute(
        "SELECT stock_code, selected, sector_code FROM shadow_candidates "
        "WHERE episode_id = ?",
        (ep["episode_id"],),
    ).fetchall()

    candidate_count = len(candidates)
    selected = [c for c in candidates if c["selected"] == 1]
    selected_count = len(selected)

    if selected_count == 0:
        return None

    # Equal-weight assumption
    weight = 1.0 / selected_count
    top1_weight = weight  # equal-weight: all same
    herfindahl = selected_count * (weight ** 2)

    # Sector concentration
    sector_weights = defaultdict(float)
    for c in selected:
        sector = c["sector_code"]
        if sector:
            sector_weights[sector] += weight
    sector_count = len(sector_weights) if sector_weights else 0
    max_sector_weight = max(sector_weights.values()) if sector_weights else 1.0

    # Gates
    min_positions_pass = 1 if selected_count >= MIN_POSITIONS else 0
    max_concentration_pass = 1 if top1_weight <= MAX_SINGLE_WEIGHT else 0
    max_sector_pass = 1 if max_sector_weight <= MAX_SECTOR_WEIGHT else 0
    diversification_pass = 1 if (min_positions_pass and max_concentration_pass
                                  and max_sector_pass) else 0

    # Failure reason
    reasons = []
    if not min_positions_pass:
        reasons.append("FAIL_MIN_POSITIONS")
    if not max_concentration_pass:
        reasons.append("FAIL_CONCENTRATION")
    if not max_sector_pass:
        reasons.append("FAIL_SECTOR")
    failure_reason = "+".join(reasons) if reasons else "PASS"

    return {
        "candidate_count": candidate_count,
        "selected_count": selected_count,
        "top1_weight": top1_weight,
        "herfindahl_index": herfindahl,
        "sector_count": sector_count,
        "max_sector_weight": max_sector_weight,
        "min_positions_pass": min_positions_pass,
        "max_concentration_pass": max_concentration_pass,
        "max_sector_pass": max_sector_pass,
        "diversification_pass": diversification_pass,
        "failure_reason": failure_reason,
    }


def main():
    evaluate_all()


if __name__ == "__main__":
    main()
