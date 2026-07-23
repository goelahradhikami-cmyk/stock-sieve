"""
Multi-Agent Competitive Arena - Commit 6-L.6 (补丁6).

Runs multiple agents' doctrines simultaneously over the same stock_factor_snapshot
to compute competitive metrics:
  - Overlap rate: how many agents picked the same stock
  - Crowding score: how concentrated selection is (Herfindahl-like)
  - Alpha decay: do crowded picks underperform?

This connects 6-K.1 Market Ecology - the sandbox is no longer isolated per-agent
but models the reality that 100 agents compete for the same stocks.

Usage:
    from src.evolution.competitive_arena import CompetitiveArena
    arena = CompetitiveArena()
    result = arena.run_competition('2026-07-17', top_n=20)
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from src.agents.doctrine_engine import DoctrineEngine, DoctrineGenome, DoctrineRegistry
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CompetitionResult:
    """Result of a multi-agent selection competition."""

    trade_date: str
    num_agents: int
    top_n: int
    # Per-stock crowding: how many agents picked each stock
    stock_crowding: dict[str, int] = field(default_factory=dict)
    # Overlap matrix summary
    avg_overlap_rate: float = 0.0  # avg pairwise overlap
    max_crowding: int = 0  # max agents on one stock
    crowded_stocks: list[tuple[str, int]] = field(default_factory=list)  # (code, count)
    # Per-doctrine picks
    doctrine_picks: dict[str, list[str]] = field(default_factory=dict)
    # Alpha decay: forward return of crowded vs uncrowded (filled if K-line available)
    crowded_avg_return: float | None = None
    uncrowded_avg_return: float | None = None
    alpha_decay: float | None = None  # uncrowded - crowded (positive = crowding hurts)


class CompetitiveArena:
    """Multi-agent competitive selection arena (补丁6)."""

    def __init__(self, eval_db_path: str = "data/evaluation.db"):
        self.db_path = eval_db_path
        self.engine = DoctrineEngine()

    def run_competition(
        self, trade_date: str, top_n: int = 20, doctrines: list[DoctrineGenome] | None = None
    ) -> CompetitionResult:
        """Run a multi-agent selection competition on a given date.

        Args:
            trade_date: the snapshot date to score
            top_n: each agent picks this many stocks
            doctrines: list of DoctrineGenomes to compete. If None, uses all
                       active doctrines from the registry.

        Returns: CompetitionResult with crowding/overlap/decay metrics.
        """
        if doctrines is None:
            reg = DoctrineRegistry(self.db_path)
            doctrines = reg.get_active()
            if not doctrines:
                # Fallback: classify all 8 base identities
                doctrines = [self.engine.classify(iv) for iv in self._base_identities()]

        result = CompetitionResult(
            trade_date=trade_date,
            num_agents=len(doctrines),
            top_n=top_n,
        )

        # Each doctrine scores the universe and picks top_n
        pick_counter: Counter[str] = Counter()
        for doctrine in doctrines:
            picks = self._score_and_pick(trade_date, doctrine.factor_bias, top_n)
            result.doctrine_picks[doctrine.doctrine_id] = picks
            pick_counter.update(picks)

        # Crowding metrics
        result.stock_crowding = dict(pick_counter)
        result.max_crowding = max(pick_counter.values()) if pick_counter else 0
        # Stocks picked by multiple agents (crowded)
        result.crowded_stocks = [
            (code, cnt) for code, cnt in pick_counter.most_common(10) if cnt > 1
        ]

        # Average pairwise overlap rate
        result.avg_overlap_rate = self._avg_overlap(result.doctrine_picks)

        # Alpha decay: try to compute forward returns for crowded vs uncrowded
        self._compute_alpha_decay(result, trade_date)

        return result

    def _score_and_pick(self, trade_date: str, factor_bias: dict, top_n: int) -> list[str]:
        """Score universe with factor_bias, return top_n security_ids."""
        conn = sqlite3.connect(self.db_path)
        try:
            q = factor_bias.get("quality", 0)
            v = factor_bias.get("value", 0)
            g = factor_bias.get("growth", 0)
            m = factor_bias.get("momentum", 0)
            r = factor_bias.get("risk", 0)
            s = factor_bias.get("sentiment", 0)
            rows = conn.execute(
                """
                SELECT security_id FROM stock_factor_snapshot
                WHERE trade_date=?
                ORDER BY quality_score * ? + value_score * ? + growth_score * ?
                         + momentum_score * ? + risk_score * ? + sentiment_score * ? DESC
                LIMIT ?
            """,
                (trade_date, q, v, g, m, r, s, top_n),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def _avg_overlap(self, doctrine_picks: dict[str, list[str]]) -> float:
        """Average pairwise overlap rate between agents' picks."""
        agents = list(doctrine_picks.values())
        if len(agents) < 2:
            return 0.0
        total_overlap = 0.0
        pairs = 0
        for i in range(len(agents)):
            for j in range(i + 1, len(agents)):
                a, b = set(agents[i]), set(agents[j])
                if a and b:
                    overlap = len(a & b) / len(a | b)  # Jaccard
                    total_overlap += overlap
                    pairs += 1
        return total_overlap / pairs if pairs > 0 else 0.0

    def _compute_alpha_decay(self, result: CompetitionResult, trade_date: str) -> None:
        """Compute forward return difference: crowded vs uncrowded stocks.

        补丁6: alpha decay = do stocks picked by many agents underperform?
        Connects to 6-K.1 Market Ecology.
        """
        if not result.stock_crowding:
            return

        # Split into crowded (>=2 agents) vs uncrowded (==1 agent)
        crowded = [c for c, n in result.stock_crowding.items() if n >= 2]
        uncrowded = [c for c, n in result.stock_crowding.items() if n == 1]

        if not crowded or not uncrowded:
            return

        # Try to compute forward returns from K-line
        try:
            from datetime import date, timedelta

            from src.data.provider import MarketDataProvider

            provider = MarketDataProvider()
            start = trade_date
            end = (date.fromisoformat(trade_date) + timedelta(days=30)).isoformat()

            def avg_return(codes):
                rets = []
                for code in codes:
                    try:
                        bare = code.split(".")[0] if "." in code else code
                        kline = provider.get_daily_kline(bare, start, end)
                        if kline is not None and not kline.empty and len(kline) >= 2:
                            close = kline["close"].values
                            ret = (close[-1] - close[0]) / close[0]
                            rets.append(ret)
                    except Exception as exc:
                        logger.warning("operation failed (was silently ignored): %s", exc)
                return sum(rets) / len(rets) if rets else None

            result.crowded_avg_return = avg_return(crowded[:10])  # cap for speed
            result.uncrowded_avg_return = avg_return(uncrowded[:10])

            if result.crowded_avg_return is not None and result.uncrowded_avg_return is not None:
                result.alpha_decay = result.uncrowded_avg_return - result.crowded_avg_return
        except Exception as e:
            logger.debug("arena: alpha decay computation failed: %s", e)

    def _base_identities(self) -> list[dict]:
        """The 8 base personality identity vectors (fallback when DB empty)."""
        return [
            {
                "valuation": 90,
                "quality": 85,
                "growth": 40,
                "momentum": 15,
                "macro": 30,
                "contrarian": 80,
                "patience": 95,
                "concentration": 70,
            },
            {
                "valuation": 50,
                "quality": 70,
                "growth": 90,
                "momentum": 40,
                "macro": 45,
                "contrarian": 20,
                "patience": 50,
                "concentration": 60,
            },
            {
                "valuation": 10,
                "quality": 40,
                "growth": 40,
                "momentum": 95,
                "macro": 50,
                "contrarian": 5,
                "patience": 20,
                "concentration": 55,
            },
            {
                "valuation": 85,
                "quality": 50,
                "growth": 15,
                "momentum": 5,
                "macro": 60,
                "contrarian": 95,
                "patience": 85,
                "concentration": 65,
            },
            {
                "valuation": 75,
                "quality": 70,
                "growth": 25,
                "momentum": 10,
                "macro": 35,
                "contrarian": 55,
                "patience": 85,
                "concentration": 50,
            },
            {
                "valuation": 50,
                "quality": 95,
                "growth": 55,
                "momentum": 15,
                "macro": 25,
                "contrarian": 35,
                "patience": 90,
                "concentration": 80,
            },
            {
                "valuation": 40,
                "quality": 50,
                "growth": 50,
                "momentum": 55,
                "macro": 65,
                "contrarian": 30,
                "patience": 30,
                "concentration": 40,
            },
            {
                "valuation": 55,
                "quality": 55,
                "growth": 50,
                "momentum": 35,
                "macro": 40,
                "contrarian": 45,
                "patience": 60,
                "concentration": 65,
            },
        ]
