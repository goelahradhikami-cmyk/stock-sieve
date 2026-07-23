"""
Alpha Ecology & Decay Engine - Commit 6-N.

Answers: "How long does a doctrine's alpha survive, and does the system
evolve a successor before it dies?"

6-L produced alpha. 6-M explained where it comes from. 6-N explains why it
disappears - and feeds that back into evolution so the system doesn't breed
a winner that's already decaying.

Four modules:
  6-N.1 Alpha Decay Tracking  - alpha_decay_history table, decay_rate per gen
  6-N.2 Crowding Score        - stock/factor/sector overlap across doctrines
  6-N.3 Alpha Half-Life       - how fast each doctrine's alpha halves
  6-N.5 Doctrine Memory Bank  - which doctrine family works in which regime

6-N.4 (Ecology Penalty in Fitness v3) lives in fitness.py, calling this module.

Core insight: without 6-N, evolution breeds yesterday's winner into today's
loser. A momentum doctrine with alpha_half_life=45 days that won Gen 1 will
be cloned through Gen 3 - by which time crowding has killed its edge. The
system needs to detect decay and trigger mutation BEFORE the alpha dies.

Usage:
    from src.evolution.alpha_ecology import AlphaEcology
    ecology = AlphaEcology()
    ecology.record_generation(gen=3, doctrines=[...], population=[...])
    decay = ecology.get_decay_rate("momentum_trend_follower")
    crowding = ecology.compute_crowding(doctrine, population)
    half_life = ecology.estimate_half_life("deep_value_purist")
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.agents.doctrine_engine import DoctrineGenome
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DecayInfo:
    """Alpha decay info for a doctrine."""

    doctrine_id: str
    current_selection_alpha: float
    peak_selection_alpha: float
    decay_rate: float  # per-generation fractional decay (e.g. -0.15 = -15%/gen)
    alpha_half_life: int  # generations for alpha to halve (0 = unknown)
    generations_observed: int


@dataclass
class CrowdingInfo:
    """How crowded a doctrine is relative to the population."""

    doctrine_id: str
    stock_overlap: float  # 0-1, avg Jaccard with other doctrines' picks
    factor_overlap: float  # 0-1, cosine similarity of factor_bias
    sector_overlap: float  # 0-1, industry weight overlap
    crowding_score: float  # weighted composite (0=unique, 1=max crowded)


class AlphaEcology:
    """Tracks alpha decay, crowding, and half-life across generations.

    Called after each SurvivalArena cycle to record the state, and queried
    by FitnessCalculator v3 to apply ecology penalties.
    """

    # Crowding weights
    W_STOCK_OVERLAP = 0.4
    W_FACTOR_OVERLAP = 0.3
    W_SECTOR_OVERLAP = 0.3

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db

    # ── 6-N.1 Alpha Decay Tracking ───────────────────────

    def record_generation(self, generation: int, fitness_map: dict[str, dict]) -> int:
        """Record per-doctrine alpha state for this generation.

        Args:
            generation: generation number
            fitness_map: {doctrine_id: {selection_alpha, factor_alpha,
                         origin_quality, ...}} from current gen's backtest

        Returns: number of rows written to alpha_decay_history.
        """
        conn = sqlite3.connect(self.eval_db)
        written = 0
        try:
            for doctrine_id, info in fitness_map.items():
                # Compute crowding (needs population context, done separately)
                crowding = info.get("crowding_score", 0.0)
                # Compute decay rate (vs previous generation)
                decay_rate = self._compute_decay_rate(
                    conn, doctrine_id, info.get("selection_alpha", 0.0)
                )
                # Estimate half-life
                half_life = self._estimate_half_life_from_history(conn, doctrine_id)

                conn.execute(
                    """
                    INSERT INTO alpha_decay_history
                    (doctrine_id, generation, factor_alpha, selection_alpha,
                     origin_quality, crowding_score, decay_rate, alpha_half_life)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        doctrine_id,
                        generation,
                        info.get("factor_alpha"),
                        info.get("selection_alpha"),
                        info.get("origin_quality"),
                        crowding,
                        decay_rate,
                        half_life,
                    ),
                )
                written += 1
            conn.commit()
        finally:
            conn.close()
        return written

    def _compute_decay_rate(
        self, conn: sqlite3.Connection, doctrine_id: str, current_alpha: float
    ) -> float:
        """Decay rate = (current - previous) / |previous|.

        Positive = alpha growing, negative = decaying.
        Returns 0.0 if no previous record.
        """
        row = conn.execute(
            "SELECT selection_alpha FROM alpha_decay_history "
            "WHERE doctrine_id=? ORDER BY generation DESC LIMIT 1",
            (doctrine_id,),
        ).fetchone()
        if not row or row[0] is None or abs(row[0]) < 1e-6:
            return 0.0
        prev = row[0]
        return (current_alpha - prev) / abs(prev)

    def get_decay_info(self, doctrine_id: str) -> DecayInfo | None:
        """Get current decay state for a doctrine."""
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT generation, selection_alpha, decay_rate, alpha_half_life "
                "FROM alpha_decay_history WHERE doctrine_id=? ORDER BY generation",
                (doctrine_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return None

        alphas = [r[1] for r in rows if r[1] is not None]
        if not alphas:
            return None

        peak = max(alphas)
        current = alphas[-1]
        latest_decay = rows[-1][2] or 0.0
        half_life = rows[-1][3] or 0

        return DecayInfo(
            doctrine_id=doctrine_id,
            current_selection_alpha=current,
            peak_selection_alpha=peak,
            decay_rate=latest_decay,
            alpha_half_life=half_life,
            generations_observed=len(rows),
        )

    # ── 6-N.2 Crowding Score ──────────────────────────────

    def compute_crowding(
        self,
        doctrine: DoctrineGenome,
        picks: list[dict],
        population: list[DoctrineGenome],
        trade_date: str,
    ) -> CrowdingInfo:
        """How crowded is this doctrine relative to the population?

        Three overlap dimensions:
          - stock_overlap: Jaccard of pick sets (same stocks = crowded)
          - factor_overlap: cosine similarity of factor_bias vectors
          - sector_overlap: industry weight overlap

        Returns: CrowdingInfo with composite crowding_score (0=unique, 1=max).
        """
        if not population or len(population) < 2:
            return CrowdingInfo(doctrine.doctrine_id, 0, 0, 0, 0)

        # Get this doctrine's pick set
        my_picks = {p["security_id"] for p in picks}

        # Get other doctrines' pick sets (simplified: re-score with their factor_bias)
        from src.factors.snapshot_builder import FactorSnapshotBuilder

        builder = FactorSnapshotBuilder()

        stock_overlaps = []
        factor_overlaps = []
        sector_overlaps = []

        my_bias = doctrine.factor_bias
        my_industry_weights = self._industry_weights(picks)

        for other in population:
            if other.doctrine_id == doctrine.doctrine_id:
                continue
            # Stock overlap (Jaccard)
            other_picks = builder.score_universe(trade_date, other.factor_bias, top_n=20)
            other_set = {p["security_id"] for p in other_picks}
            if my_picks and other_set:
                jaccard = len(my_picks & other_set) / len(my_picks | other_set)
                stock_overlaps.append(jaccard)

            # Factor overlap (cosine)
            fo = self._cosine_similarity(my_bias, other.factor_bias)
            factor_overlaps.append(fo)

            # Sector overlap
            other_iw = self._industry_weights(other_picks)
            so = self._dict_overlap(my_industry_weights, other_iw)
            sector_overlaps.append(so)

        avg_stock = np.mean(stock_overlaps) if stock_overlaps else 0.0
        avg_factor = np.mean(factor_overlaps) if factor_overlaps else 0.0
        avg_sector = np.mean(sector_overlaps) if sector_overlaps else 0.0

        crowding = (
            self.W_STOCK_OVERLAP * avg_stock
            + self.W_FACTOR_OVERLAP * avg_factor
            + self.W_SECTOR_OVERLAP * avg_sector
        )

        return CrowdingInfo(
            doctrine_id=doctrine.doctrine_id,
            stock_overlap=float(avg_stock),
            factor_overlap=float(avg_factor),
            sector_overlap=float(avg_sector),
            crowding_score=float(crowding),
        )

    def _industry_weights(self, picks: list[dict]) -> dict[str, float]:
        """Compute industry weight distribution from picks."""
        if not picks:
            return {}
        conn = sqlite3.connect(self.cache_db)
        industry_mv: dict[str, float] = {}
        total = 0.0
        for pick in picks:
            code = pick.get("security_id", "")
            bare = code.split(".")[0] if "." in code else code
            row = conn.execute(
                "SELECT industry, total_mv FROM security_master WHERE code=?",
                (bare,),
            ).fetchone()
            if row and row[0]:
                mv = float(row[1]) if row[1] else 1.0
                industry_mv[row[0]] = industry_mv.get(row[0], 0) + mv
                total += mv
        conn.close()
        if total <= 0:
            return {}
        return {k: v / total for k, v in industry_mv.items()}

    @staticmethod
    def _cosine_similarity(a: dict, b: dict) -> float:
        """Cosine similarity of two factor_bias dicts."""
        keys = set(a.keys()) | set(b.keys())
        va = np.array([a.get(k, 0) for k in keys])
        vb = np.array([b.get(k, 0) for k in keys])
        norm = np.linalg.norm(va) * np.linalg.norm(vb)
        return float(np.dot(va, vb) / norm) if norm > 0 else 0.0

    @staticmethod
    def _dict_overlap(a: dict, b: dict) -> float:
        """Overlap coefficient (min sum) for two weight dicts."""
        if not a or not b:
            return 0.0
        keys = set(a.keys()) | set(b.keys())
        return float(sum(min(a.get(k, 0), b.get(k, 0)) for k in keys))

    # ── 6-N.3 Alpha Half-Life ─────────────────────────────

    def _estimate_half_life_from_history(self, conn: sqlite3.Connection, doctrine_id: str) -> int:
        """Estimate alpha half-life in generations.

        If alpha is decaying at rate r per generation, half-life = ln(2)/|r|.
        Returns 0 if no decay or insufficient data.
        """
        rows = conn.execute(
            "SELECT selection_alpha FROM alpha_decay_history "
            "WHERE doctrine_id=? AND selection_alpha IS NOT NULL "
            "ORDER BY generation",
            (doctrine_id,),
        ).fetchall()
        if len(rows) < 3:
            return 0

        alphas = [r[0] for r in rows]
        # Fit linear regression: alpha = a + b*gen
        gens = np.arange(len(alphas))
        slope, _ = np.polyfit(gens, alphas, 1)

        # If decaying (slope < 0) and started positive, estimate half-life
        first_alpha = alphas[0]
        if slope < 0 and first_alpha > 0:
            half_life = int(np.log(2) / abs(slope / first_alpha)) if abs(first_alpha) > 1e-6 else 0
            return max(1, min(100, half_life))
        return 0

    def estimate_half_life(self, doctrine_id: str) -> int:
        """Public API: get estimated alpha half-life (generations)."""
        conn = sqlite3.connect(self.eval_db)
        try:
            return self._estimate_half_life_from_history(conn, doctrine_id)
        finally:
            conn.close()

    # ── 6-N.5 Doctrine Memory Bank ────────────────────────

    def update_memory(self, doctrine_family: str, regime: str, selection_alpha: float) -> None:
        """Record that a doctrine family performed in a regime.

        Builds a memory of "which doctrine type works in which environment"
        so new doctrines can be evaluated against historical analogues.
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            # Upsert: incrementally update avg + count
            row = conn.execute(
                "SELECT avg_selection_alpha, sample_count FROM doctrine_memory "
                "WHERE doctrine_family=? AND market_regime=?",
                (doctrine_family, regime),
            ).fetchone()
            if row:
                old_avg, n = row
                new_n = n + 1
                new_avg = ((old_avg or 0) * n + selection_alpha) / new_n
                conn.execute(
                    "UPDATE doctrine_memory SET avg_selection_alpha=?, sample_count=?, "
                    "last_seen=date('now') WHERE doctrine_family=? AND market_regime=?",
                    (new_avg, new_n, doctrine_family, regime),
                )
            else:
                conn.execute(
                    "INSERT INTO doctrine_memory (doctrine_family, market_regime, "
                    "avg_selection_alpha, sample_count, last_seen) VALUES (?, ?, ?, 1, date('now'))",
                    (doctrine_family, regime, selection_alpha),
                )
            conn.commit()
        finally:
            conn.close()

    def recall_memory(self, doctrine_family: str) -> dict[str, dict]:
        """Recall how a doctrine family performed across regimes.

        Returns: {regime: {avg_alpha, sample_count, last_seen}}
        """
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT market_regime, avg_selection_alpha, sample_count, last_seen "
                "FROM doctrine_memory WHERE doctrine_family=?",
                (doctrine_family,),
            ).fetchall()
            return {r["market_regime"]: dict(r) for r in rows}
        finally:
            conn.close()

    # ── 6-N.4 Ecology Penalty (for Fitness v3) ────────────

    def ecology_penalty(self, doctrine_id: str) -> float:
        """Compute ecology penalty for fitness v3 (0=no penalty, 1=max).

        Combines:
          - decay_penalty: if alpha is decaying, penalize
          - crowding_penalty: if crowded, penalize

        Returns: penalty 0-1 (subtracted from fitness).
        """
        decay_info = self.get_decay_info(doctrine_id)
        # Get latest crowding from alpha_decay_history
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT crowding_score FROM alpha_decay_history "
                "WHERE doctrine_id=? ORDER BY generation DESC LIMIT 1",
                (doctrine_id,),
            ).fetchone()
        finally:
            conn.close()
        crowding = row[0] if row and row[0] is not None else 0.0

        # Decay penalty: if decay_rate < -0.2 (losing 20%+ of alpha per gen), penalize
        decay_penalty = 0.0
        if decay_info and decay_info.decay_rate < 0:
            # Scale: -0.2 decay -> 0.3 penalty, -0.5 decay -> 0.7 penalty
            decay_penalty = min(0.7, abs(decay_info.decay_rate) * 1.5)

        # Crowding penalty: crowding_score > 0.5 -> penalize
        crowding_penalty = max(0.0, (crowding - 0.3) * 1.0)  # 0.3 threshold, linear
        crowding_penalty = min(0.5, crowding_penalty)

        return min(1.0, decay_penalty + crowding_penalty)
