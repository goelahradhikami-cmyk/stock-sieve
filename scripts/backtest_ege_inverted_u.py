"""
Backtest EGE inverted-U transform - follow-up to v33_gate_removal_validation.

Context: the v3.3 no-gate pool (200 candidates, 10 episodes) shows EGE gap
(stored as recovery_score = gap_score*50+50, unclamped) is inverted-U:
mid-gap quintiles earn +2~3% residual alpha, extreme-high-gap quintile
earns -5.65%. Current production ranking is divergence_score only, which
ranks extreme-gap names at the top when they look "mispriced".

This script compares selection rules on the SAME candidate pool
(shadow_candidates_v3, top-20 per episode), picking top-5 per episode,
equal-weight, T+20 horizon:

  A baseline      : top-5 by divergence_score (current behavior)
  B drop_extreme  : exclude recovery_score > 100 (gap z > +1), then A's rule
  C drop_q5       : exclude episode-internal top EGE quintile, then A's rule
  D penalty       : divergence_score - max(0, recovery_score-100)/100 penalty
  E sweet_zone    : sort by -abs(recovery_score - 50) (pure mid-gap preference)
  F div_x_sweet   : divergence_score among recovery_score in [0, 100] only

Metrics: pooled and per-episode mean stock_return_t20, market_beta
(alpha vs HS300), residual_alpha, hit rate.

Usage:
    python scripts/backtest_ege_inverted_u.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SHADOW_DB = "data/shadow_trading.db"
TOP_N = 5


def load_pool() -> list[sqlite3.Row]:
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT episode_id, trade_date, security_id, funnel_rank,
                  divergence_score, recovery_score,
                  stock_return_t20, market_beta, residual_alpha
           FROM shadow_candidates_v3
           WHERE stock_return_t20 IS NOT NULL
           ORDER BY episode_id, funnel_rank"""
    ).fetchall()
    conn.close()
    return rows


def select(rows: list[sqlite3.Row], rule: str) -> list[sqlite3.Row]:
    """Pick top-N for ONE episode under a selection rule."""
    if rule == "A_baseline":
        key = lambda r: -(r["divergence_score"] or 0)
        pool = rows
    elif rule == "B_drop_extreme":
        pool = [r for r in rows if (r["recovery_score"] or 0) <= 100]
        key = lambda r: -(r["divergence_score"] or 0)
    elif rule == "C_drop_q5":
        eges = sorted((r["recovery_score"] or 0) for r in rows)
        cut = eges[int(len(eges) * 0.8)] if eges else 0
        pool = [r for r in rows if (r["recovery_score"] or 0) <= cut]
        key = lambda r: -(r["divergence_score"] or 0)
    elif rule == "D_penalty":
        pool = rows
        key = lambda r: -(
            (r["divergence_score"] or 0) - max(0.0, (r["recovery_score"] or 0) - 100) / 100.0
        )
    elif rule == "E_sweet_zone":
        pool = rows
        key = lambda r: abs((r["recovery_score"] or 0) - 50)
    elif rule == "F_div_x_sweet":
        pool = [r for r in rows if 0 <= (r["recovery_score"] or 0) <= 100]
        key = lambda r: -(r["divergence_score"] or 0)
    else:
        raise ValueError(rule)
    return sorted(pool, key=key)[:TOP_N]


def metrics(picks: list[sqlite3.Row]) -> dict:
    if not picks:
        return {"n": 0}
    ret = np.array([p["stock_return_t20"] for p in picks])
    mb = np.array([p["market_beta"] for p in picks if p["market_beta"] is not None])
    ra = np.array([p["residual_alpha"] for p in picks if p["residual_alpha"] is not None])
    return {
        "n": len(picks),
        "ret_mean": float(np.mean(ret)),
        "ret_median": float(np.median(ret)),
        "hit": float(np.mean(ret > 0)),
        "mb_mean": float(np.mean(mb)) if len(mb) else None,
        "ra_mean": float(np.mean(ra)) if len(ra) else None,
        "worst": float(np.min(ret)),
    }


def main() -> None:
    rows = load_pool()
    episodes: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        episodes.setdefault(r["episode_id"], []).append(r)
    print(f"pool: {len(rows)} candidates, {len(episodes)} episodes\n")

    rules = ["A_baseline", "B_drop_extreme", "C_drop_q5", "D_penalty", "E_sweet_zone", "F_div_x_sweet"]
    summary = {}
    per_ep = {rule: {} for rule in rules}
    for rule in rules:
        all_picks = []
        for ep, ep_rows in episodes.items():
            picks = select(ep_rows, rule)
            per_ep[rule][ep] = metrics(picks)
            all_picks.extend(picks)
        summary[rule] = metrics(all_picks)

    hdr = f"{'rule':16s} {'N':>4s} {'ret均值':>8s} {'ret中位':>8s} {'胜率':>6s} {'alpha vs 300':>12s} {'残差alpha':>9s} {'最差':>8s}"
    print(hdr)
    print("-" * len(hdr))
    for rule in rules:
        m = summary[rule]
        print(
            f"{rule:16s} {m['n']:>4d} {m['ret_mean']:>+8.2%} {m['ret_median']:>+8.2%} "
            f"{m['hit']:>6.0%} {m['mb_mean']:>+12.2%} {m['ra_mean']:>+9.2%} {m['worst']:>+8.2%}"
        )

    print("\n=== 每 episode 组合收益 (T+20 等权, 股票收益) ===")
    eps = sorted(episodes)
    print(f"{'episode':12s}" + "".join(f"{r.replace('_', ' '):>16s}" for r in rules))
    for ep in eps:
        line = f"{ep:12s}"
        for rule in rules:
            m = per_ep[rule][ep]
            line += f"{m['ret_mean']:>+16.2%}" if m["n"] else f"{'n/a':>16s}"
        print(line)

    print("\n=== 每 episode: 变体相对基线 A 的超额 (均值差) ===")
    print(f"{'episode':12s}" + "".join(f"{r.split('_')[0]:>8s}" for r in rules[1:]))
    wins = {rule: 0 for rule in rules[1:]}
    for ep in eps:
        base = per_ep["A_baseline"][ep]["ret_mean"]
        line = f"{ep:12s}"
        for rule in rules[1:]:
            diff = per_ep[rule][ep]["ret_mean"] - base
            if diff > 0:
                wins[rule] += 1
            line += f"{diff:>+8.2%}"
        print(line)
    print(f"\n{'胜出期数':12s}" + "".join(f"{wins[r]:>8d}" for r in rules[1:]))


if __name__ == "__main__":
    main()
