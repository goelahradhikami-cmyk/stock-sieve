"""
Shadow Trading Outcome Evaluator - Commit 6-S.10.1.

Evaluates recorded shadow episodes at T+20, computing:
  1. BUY episodes: portfolio return + alpha vs benchmarks
  2. BLOCK episodes: counterfactual return (what if we had bought?)
  3. Classification: SUCCESS_BUY / FAILED_BUY / SUCCESS_BLOCK / FALSE_BLOCK

This is the "reality check" layer. It connects predictions to outcomes,
forming the Prediction -> Reality -> Error -> Belief Update loop.

Usage:
    python scripts/shadow_outcome_evaluator.py
"""

from __future__ import annotations

import os
import sys
import sqlite3
import numpy as np
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.local_provider import LocalDataProvider
from src.data.index_provider import IndexDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"
HORIZON = 20


class OutcomeEvaluator:
    """Evaluates shadow episodes at T+20.

    For BUY episodes:
      - Compute portfolio return (equal-weight of selected stocks)
      - Compute market benchmarks (HS300, CSI All, equal-weight universe)
      - Alpha = portfolio - benchmark
      - Classify: SUCCESS_BUY (alpha>0) / FAILED_BUY (alpha<0)

    For BLOCK episodes:
      - Compute counterfactual return (what if we had bought candidates?)
      - avoided_loss = -counterfactual_return (positive = Brain was right)
      - Classify: SUCCESS_BLOCK (counterfactual<0) / FALSE_BLOCK (counterfactual>0)
    """

    def __init__(self):
        self.local = LocalDataProvider()
        self.idx = IndexDataProvider()

    def evaluate_all(self) -> dict:
        """Evaluate all pending episodes that have reached T+20.

        Returns: summary statistics
        """
        conn = sqlite3.connect(SHADOW_DB)
        conn.row_factory = sqlite3.Row

        # Get all pending episodes
        episodes = conn.execute(
            "SELECT * FROM shadow_episode WHERE status='pending' ORDER BY trade_date"
        ).fetchall()

        print(f"Evaluating {len(episodes)} pending episodes...", flush=True)

        stats = {
            "total": 0,
            "success_buy": 0,
            "failed_buy": 0,
            "success_block": 0,
            "false_block": 0,
            "buy_alphas": [],
            "block_counterfactuals": [],
            "avoided_losses": [],
            "missed_gains": [],
        }

        for ep in episodes:
            result = self._evaluate_one(conn, ep)
            if result is None:
                continue

            stats["total"] += 1
            outcome_type = result["outcome_type"]
            stats[outcome_type.lower()] = stats.get(outcome_type.lower(), 0) + 1

            if result.get("alpha") is not None:
                stats["buy_alphas"].append(result["alpha"])
            if result.get("counterfactual_return") is not None:
                stats["block_counterfactuals"].append(result["counterfactual_return"])
            if result.get("avoided_loss") is not None:
                stats["avoided_losses"].append(result["avoided_loss"])
            if result.get("missed_gain") is not None:
                stats["missed_gains"].append(result["missed_gain"])

        conn.commit()
        conn.close()

        # Summary
        print(f"\n=== Outcome Evaluation Summary ===")
        print(f"Total evaluated: {stats['total']}")
        print(f"  SUCCESS_BUY:  {stats['success_buy']}")
        print(f"  FAILED_BUY:   {stats['failed_buy']}")
        print(f"  SUCCESS_BLOCK:{stats['success_block']}")
        print(f"  FALSE_BLOCK:  {stats['false_block']}")

        if stats["buy_alphas"]:
            alphas = np.array(stats["buy_alphas"])
            print(f"\n  BUY Alpha: mean={np.mean(alphas):+.2%} median={np.median(alphas):+.2%} "
                  f"win_rate={np.mean(alphas > 0):.0%}")

        if stats["block_counterfactuals"]:
            cfs = np.array(stats["block_counterfactuals"])
            print(f"\n  BLOCK Counterfactual: mean={np.mean(cfs):+.2%} median={np.median(cfs):+.2%}")
            print(f"  Avoided Loss (avg): {np.mean(stats['avoided_losses']):+.2%}")
            print(f"  Missed Gain (avg): {np.mean(stats['missed_gains']):+.2%}")
            print(f"  Block Accuracy: {stats['success_block']}/{stats['success_block']+stats['false_block']} "
                  f"({stats['success_block']/max(1,stats['success_block']+stats['false_block'])*100:.0f}%)")

        return stats

    def _evaluate_one(self, conn: sqlite3.Connection, ep: sqlite3.Row) -> dict | None:
        """Evaluate one episode.

        Returns: outcome dict, or None if not yet evaluable.
        """
        trade_date = ep["trade_date"]
        episode_id = ep["episode_id"]
        decision = ep["decision"]

        # Find eval_date (T+HORIZON trading days)
        cache_conn = sqlite3.connect(CACHE_DB)
        eval_row = cache_conn.execute(
            "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
            "AND trade_date > ? ORDER BY trade_date LIMIT 1 OFFSET ?",
            (trade_date, HORIZON - 1),
        ).fetchone()
        cache_conn.close()

        if not eval_row:
            return None  # T+20 hasn't arrived yet

        eval_date = eval_row[0]

        # Get candidates for this episode
        candidates = conn.execute(
            "SELECT * FROM shadow_candidates WHERE episode_id=?",
            (episode_id,),
        ).fetchall()

        if decision == "BUY":
            return self._evaluate_buy(conn, ep, candidates, trade_date, eval_date)
        else:
            return self._evaluate_block(conn, ep, candidates, trade_date, eval_date)

    def _evaluate_buy(self, conn, ep, candidates, trade_date, eval_date):
        """Evaluate a BUY episode."""
        # Selected stocks (the ones we would have bought)
        selected = [c for c in candidates if c["selected"] == 1]
        if not selected:
            # No stocks selected despite BUY -> treat as no position
            self._write_outcome(conn, ep["episode_id"], eval_date, "FAILED_BUY",
                                portfolio_return=0.0, market_return=0.0, alpha=0.0,
                                counterfactual_return=None, avoided_loss=None, missed_gain=None)
            return {"outcome_type": "FAILED_BUY", "alpha": 0.0}

        # Compute portfolio return (equal-weight)
        returns = []
        for c in selected:
            ret = self._get_stock_return(c["stock_code"], trade_date, eval_date)
            if ret is not None:
                returns.append(ret)

        if not returns:
            return None

        portfolio_return = float(np.mean(returns))

        # Market benchmarks
        mkt_return = self.idx.get_return("000300", trade_date, eval_date)

        # Alpha
        alpha = portfolio_return - mkt_return

        # Classification
        outcome_type = "SUCCESS_BUY" if alpha > 0 else "FAILED_BUY"

        # Max drawdown (simplified: min return in portfolio)
        max_dd = float(min(returns)) if returns else 0.0

        self._write_outcome(conn, ep["episode_id"], eval_date, outcome_type,
                            portfolio_return=portfolio_return,
                            market_return=mkt_return,
                            alpha=alpha,
                            max_drawdown=max_dd,
                            counterfactual_return=None,
                            avoided_loss=None,
                            missed_gain=None)

        # Update per-stock returns
        for c in selected:
            ret = self._get_stock_return(c["stock_code"], trade_date, eval_date)
            if ret is not None:
                conn.execute(
                    "UPDATE shadow_candidates SET stock_return_t20=? WHERE id=?",
                    (ret, c["id"]),
                )

        return {"outcome_type": outcome_type, "alpha": alpha, "portfolio_return": portfolio_return}

    def _evaluate_block(self, conn, ep, candidates, trade_date, eval_date):
        """Evaluate a BLOCK episode with counterfactual."""
        if not candidates:
            # No candidates recorded -> can't evaluate counterfactual
            self._write_outcome(conn, ep["episode_id"], eval_date, "SUCCESS_BLOCK",
                                portfolio_return=0.0, market_return=0.0, alpha=0.0,
                                counterfactual_return=0.0, avoided_loss=0.0, missed_gain=0.0)
            return {"outcome_type": "SUCCESS_BLOCK", "counterfactual_return": 0.0,
                    "avoided_loss": 0.0, "missed_gain": 0.0}

        # Counterfactual: what if we had bought all candidates?
        returns = []
        for c in candidates[:20]:  # cap for speed
            ret = self._get_stock_return(c["stock_code"], trade_date, eval_date)
            if ret is not None:
                returns.append(ret)

        if not returns:
            return None

        counterfactual_return = float(np.mean(returns))
        mkt_return = self.idx.get_return("000300", trade_date, eval_date)

        # Classification
        if counterfactual_return < 0:
            outcome_type = "SUCCESS_BLOCK"
            avoided_loss = abs(counterfactual_return)
            missed_gain = 0.0
        else:
            outcome_type = "FALSE_BLOCK"
            avoided_loss = 0.0
            missed_gain = counterfactual_return

        self._write_outcome(conn, ep["episode_id"], eval_date, outcome_type,
                            portfolio_return=0.0,  # didn't buy
                            market_return=mkt_return,
                            alpha=-mkt_return,  # opportunity cost
                            counterfactual_return=counterfactual_return,
                            avoided_loss=avoided_loss,
                            missed_gain=missed_gain)

        return {
            "outcome_type": outcome_type,
            "counterfactual_return": counterfactual_return,
            "avoided_loss": avoided_loss,
            "missed_gain": missed_gain,
        }

    def _write_outcome(self, conn, episode_id, eval_date, outcome_type,
                        portfolio_return=0.0, market_return=0.0, alpha=0.0,
                        max_drawdown=0.0,
                        counterfactual_return=None,
                        avoided_loss=None,
                        missed_gain=None):
        """Write outcome to shadow_outcome and shadow_counterfactual tables."""
        # shadow_outcome
        conn.execute("""
            INSERT OR REPLACE INTO shadow_outcome
            (episode_id, portfolio_return_t20, market_return_t20,
             alpha_vs_hs300, win, alpha_positive, failure_type, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            episode_id, portfolio_return, market_return,
            alpha,
            1 if portfolio_return > 0 else 0,
            1 if alpha > 0 else 0,
            outcome_type,
            eval_date,
        ))

        # shadow_counterfactual
        if counterfactual_return is not None:
            block_quality = "CORRECT_BLOCK" if outcome_type == "SUCCESS_BLOCK" else "INCORRECT_BLOCK"
            conn.execute("""
                INSERT OR REPLACE INTO shadow_counterfactual
                (episode_id, counterfactual_return, counterfactual_alpha,
                 avoided_loss, missed_gain, block_quality, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                episode_id, counterfactual_return,
                counterfactual_return - market_return if market_return else None,
                avoided_loss, missed_gain, block_quality, eval_date,
            ))

        # Update episode status
        conn.execute(
            "UPDATE shadow_episode SET status='evaluated' WHERE episode_id=?",
            (episode_id,),
        )

    def _get_stock_return(self, code, start, end):
        """Get forward return for a stock."""
        try:
            kline = self.local.get_daily_kline(code, start, end)
            if kline is not None and not kline.empty and len(kline) >= 2:
                close = kline["close"].values
                return float((close[-1] - close[0]) / close[0])
        except Exception:
            pass
        return None


def main():
    evaluator = OutcomeEvaluator()
    stats = evaluator.evaluate_all()

    # Write rolling metrics
    if stats["total"] > 0:
        conn = sqlite3.connect(SHADOW_DB)
        today = date.today().isoformat()

        buy_count = stats["success_buy"] + stats["failed_buy"]
        block_count = stats["success_block"] + stats["false_block"]

        avg_avoided = float(np.mean(stats["avoided_losses"])) if stats["avoided_losses"] else 0
        avg_missed = float(np.mean(stats["missed_gains"])) if stats["missed_gains"] else 0
        block_acc = stats["success_block"] / max(1, block_count)

        buy_alpha_median = float(np.median(stats["buy_alphas"])) if stats["buy_alphas"] else 0

        conn.execute("""
            INSERT OR REPLACE INTO shadow_metrics
            (metric_date, total_episodes, buy_episodes, block_episodes,
             avg_avoided_loss, avg_missed_gain, block_accuracy)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (today, stats["total"], buy_count, block_count,
              avg_avoided, avg_missed, block_acc))
        conn.commit()
        conn.close()

        print(f"\n=== Shadow Metrics ({today}) ===")
        print(f"  Block Accuracy: {block_acc:.0%}")
        print(f"  Avg Avoided Loss: {avg_avoided:+.2%}")
        print(f"  Avg Missed Gain: {avg_missed:+.2%}")
        print(f"  BUY Alpha Median: {buy_alpha_median:+.2%}")


if __name__ == "__main__":
    main()
