"""
Alpha Competition Engine - Commit 6-N.2.

Solves the 50-gen experiment's core problem: "投资人格分化了,但投资行为趋同"
(doctrines have different names but converge to similar stock picks, factor
exposure, and thesis patterns - crowding 0.7-0.84 sustained).

Three modules:
  6-N.2a Competition Matrix  - 4-dimensional similarity (stock + factor + thesis + identity)
  6-N.2b Survival Memory     - birth/death reason, alpha peak, competitors
  6-N.2c Half-life Engine    - exponential fit alpha_quality(t) = A*e^(-λt)

These feed into Fitness v4: Origin Alpha × Uniqueness × Survival Probability.

Key insight: crowding penalty (6-N.1) only punishes AFTER the fact. Competition
Index prevents it by measuring how similar doctrines REALLY are (not just stock
overlap - two doctrines can pick different stocks but have identical factor
exposure, making them the same species).

Usage:
    from src.evolution.competition import CompetitionEngine
    comp = CompetitionEngine()
    matrix = comp.compute_competition_matrix(population, trade_date)
    memory = comp.record_death(doctrine_id, gen, reason, ...)
    half_life = comp.estimate_half_life(doctrine_id)
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass

import numpy as np

from src.agents.doctrine_engine import IDENTITY_DIMS, DoctrineGenome
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CompetitionInfo:
    """How similar two doctrines are across 4 dimensions."""

    doctrine_a: str
    doctrine_b: str
    stock_overlap: float  # Jaccard of pick sets (0-1)
    factor_similarity: float  # cosine of factor_bias vectors (0-1)
    thesis_similarity: float  # ranking correlation of thesis_priority (0-1)
    identity_distance: float  # normalized Euclidean of identity (0-1, 0=same)
    strategy_distance: float  # composite (0=same species, 1=totally different)
    is_same_species: bool  # strategy_distance < 0.15


class CompetitionEngine:
    """4-dimensional competition analysis + survival memory + half-life.

    The 50-gen experiment showed crowding 0.7-0.84 sustained - doctrines
    converge in behavior despite diverging in name. This engine measures
    TRUE similarity (not just stock overlap) to detect "same species"
    doctrines that should compete more intensely.
    """

    # Species threshold: if strategy_distance < this, they're the same species
    SPECIES_THRESHOLD = 0.15

    # Half-life: minimum generations to fit exponential decay
    HALF_LIFE_MIN_GENS = 3

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db

    # ── 6-N.2a Competition Matrix ─────────────────────────

    def compute_competition_matrix(
        self, doctrines: list[DoctrineGenome], picks_map: dict[str, list[dict]], trade_date: str
    ) -> dict[str, dict[str, CompetitionInfo]]:
        """Compute pairwise competition between all doctrines.

        Args:
            doctrines: all doctrines in population
            picks_map: {doctrine_id: picks} from score_universe
            trade_date: for reference

        Returns: {doctrine_a_id: {doctrine_b_id: CompetitionInfo}}
        """
        matrix: dict[str, dict[str, CompetitionInfo]] = {}
        for i, a in enumerate(doctrines):
            matrix[a.doctrine_id] = {}
            picks_a = {p["security_id"] for p in picks_map.get(a.doctrine_id, [])}
            for j, b in enumerate(doctrines):
                if i == j:
                    continue
                picks_b = {p["security_id"] for p in picks_map.get(b.doctrine_id, [])}

                stock_overlap = self._jaccard(picks_a, picks_b)
                factor_sim = self._cosine(a.factor_bias, b.factor_bias)
                thesis_sim = self._thesis_similarity(a.thesis_priority, b.thesis_priority)
                identity_dist = self._identity_distance(a.identity_origin, b.identity_origin)

                # Composite: weighted (stock 0.4 + factor 0.3 + thesis 0.2 + identity 0.1)
                # strategy_distance = 1 - similarity (0=same, 1=different)
                similarity = (
                    0.4 * stock_overlap
                    + 0.3 * factor_sim
                    + 0.2 * thesis_sim
                    + 0.1 * (1.0 - identity_dist)
                )
                strategy_distance = 1.0 - similarity

                matrix[a.doctrine_id][b.doctrine_id] = CompetitionInfo(
                    doctrine_a=a.doctrine_id,
                    doctrine_b=b.doctrine_id,
                    stock_overlap=stock_overlap,
                    factor_similarity=factor_sim,
                    thesis_similarity=thesis_sim,
                    identity_distance=identity_dist,
                    strategy_distance=strategy_distance,
                    is_same_species=strategy_distance < self.SPECIES_THRESHOLD,
                )
        return matrix

    def count_competitors(
        self, matrix: dict[str, dict[str, CompetitionInfo]], doctrine_id: str
    ) -> int:
        """How many other doctrines are the same species as this one."""
        if doctrine_id not in matrix:
            return 0
        return sum(1 for info in matrix[doctrine_id].values() if info.is_same_species)

    def uniqueness_score(
        self, matrix: dict[str, dict[str, CompetitionInfo]], doctrine_id: str, population_size: int
    ) -> float:
        """How unique a doctrine is (0=crowded, 1=totally unique).

        = 1 - (same_species_count / population_size)
        """
        competitors = self.count_competitors(matrix, doctrine_id)
        if population_size <= 1:
            return 1.0
        return max(0.0, 1.0 - competitors / population_size)

    # ── 6-N.2b Survival Memory ────────────────────────────

    def record_birth(
        self,
        doctrine_id: str,
        family: str,
        generation: int,
        birth_reason: str = "crossover_mutation",
    ) -> None:
        """Record a doctrine's birth (called when a child is born)."""
        conn = sqlite3.connect(self.eval_db)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO doctrine_survival_memory
                (doctrine_id, family, birth_generation, birth_reason, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
                (doctrine_id, family, generation, birth_reason),
            )
            conn.commit()
        finally:
            conn.close()

    def record_death(
        self,
        doctrine_id: str,
        death_generation: int,
        death_reason: str,
        alpha_peak: float = None,
        alpha_final: float = None,
        alpha_half_life: int = None,
        avg_crowding: float = None,
        top_competitors: list[str] = None,
    ) -> None:
        """Record a doctrine's death (called when extinct)."""
        conn = sqlite3.connect(self.eval_db)
        try:
            # Get birth generation
            row = conn.execute(
                "SELECT birth_generation FROM doctrine_survival_memory WHERE doctrine_id=?",
                (doctrine_id,),
            ).fetchone()
            birth_gen = row[0] if row else 0
            lifespan = death_generation - birth_gen

            conn.execute(
                """
                UPDATE doctrine_survival_memory SET
                death_generation=?, death_reason=?, alpha_peak=?, alpha_final=?,
                alpha_half_life=?, avg_crowding=?, top_competitors=?, lifespan=?
                WHERE doctrine_id=?
            """,
                (
                    death_generation,
                    death_reason,
                    alpha_peak,
                    alpha_final,
                    alpha_half_life,
                    avg_crowding,
                    json.dumps(top_competitors or []),
                    lifespan,
                    doctrine_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_survival_stats(self) -> dict:
        """Aggregate survival statistics across all dead doctrines."""
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT family, death_reason, alpha_peak, lifespan, "
                "alpha_half_life, avg_crowding FROM doctrine_survival_memory "
                "WHERE death_generation IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"total_deaths": 0}

        # Aggregate by death reason
        reason_counts: dict[str, int] = {}
        family_lifespans: dict[str, list[int]] = {}
        for row in rows:
            reason = row["death_reason"] or "unknown"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            fam = row["family"] or "unknown"
            if fam not in family_lifespans:
                family_lifespans[fam] = []
            if row["lifespan"]:
                family_lifespans[fam].append(row["lifespan"])

        return {
            "total_deaths": len(rows),
            "death_reasons": reason_counts,
            "avg_lifespan": float(np.mean([r["lifespan"] or 0 for r in rows])),
            "family_lifespans": {
                fam: {"avg": float(np.mean(ls)), "max": max(ls), "count": len(ls)}
                for fam, ls in family_lifespans.items()
                if ls
            },
        }

    # ── 6-N.2c Half-life Engine ───────────────────────────

    def estimate_half_life(self, doctrine_id: str) -> int:
        """Estimate alpha half-life via exponential fit.

        Fits: alpha_quality(t) = A * e^(-λt)
        half_life = ln(2) / λ

        Returns half-life in generations, or 0 if insufficient data.
        """
        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT generation, selection_alpha FROM alpha_decay_history "
                "WHERE doctrine_id=? AND selection_alpha IS NOT NULL "
                "ORDER BY generation",
                (doctrine_id,),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < self.HALF_LIFE_MIN_GENS:
            return 0

        gens = np.array([r[0] for r in rows], dtype=float)
        alphas = np.array([r[1] for r in rows], dtype=float)

        # Need positive alphas for exponential fit (skip if all negative)
        if np.all(alphas <= 0):
            return 0

        # Shift alphas to be positive for log transform
        min_alpha = np.min(alphas)
        alphas_shifted = alphas - min_alpha + 0.001 if min_alpha <= 0 else alphas

        try:
            # Linear fit on log(alpha) = log(A) - λt
            log_alphas = np.log(alphas_shifted)
            slope, intercept = np.polyfit(gens, log_alphas, 1)

            # slope = -λ (negative if decaying)
            if slope < 0:
                lam = -slope
                half_life = math.log(2) / lam
                return max(1, int(half_life))
            else:
                return 0  # not decaying
        except (np.linalg.LinAlgError, ValueError):
            return 0

    def get_half_life_by_family(self) -> dict[str, int]:
        """Get median half-life per doctrine family."""
        conn = sqlite3.connect(self.eval_db)
        try:
            # Get all doctrine_ids and their families
            rows = conn.execute("SELECT DISTINCT doctrine_id FROM alpha_decay_history").fetchall()
        finally:
            conn.close()

        family_half_lives: dict[str, list[int]] = {}
        for (doctrine_id,) in rows:
            family = doctrine_id.split("_")[0] if "_" in doctrine_id else doctrine_id
            hl = self.estimate_half_life(doctrine_id)
            if hl > 0:
                if family not in family_half_lives:
                    family_half_lives[family] = []
                family_half_lives[family].append(hl)

        return {fam: int(np.median(hls)) for fam, hls in family_half_lives.items() if hls}

    # ── Helpers ───────────────────────────────────────────

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    @staticmethod
    def _cosine(a: dict, b: dict) -> float:
        keys = set(a.keys()) | set(b.keys())
        va = np.array([a.get(k, 0) for k in keys])
        vb = np.array([b.get(k, 0) for k in keys])
        norm = np.linalg.norm(va) * np.linalg.norm(vb)
        return float(np.dot(va, vb) / norm) if norm > 0 else 0.0

    @staticmethod
    def _thesis_similarity(a: list[str], b: list[str]) -> float:
        """Spearman-like ranking similarity of thesis priority lists."""
        if not a or not b:
            return 0.0
        # Build rank dicts
        rank_a = {p: i for i, p in enumerate(a)}
        rank_b = {p: i for i, p in enumerate(b)}
        common = set(a) & set(b)
        if not common:
            return 0.0
        # Normalized rank distance
        n = len(a)
        total_dist = sum(abs(rank_a[p] - rank_b[p]) for p in common)
        max_dist = n * len(common)
        return 1.0 - (total_dist / max_dist if max_dist > 0 else 0)

    @staticmethod
    def _identity_distance(a: dict, b: dict) -> float:
        """Normalized Euclidean distance of identity vectors (0=same, 1=opposite)."""
        sq_sum = 0.0
        for dim in IDENTITY_DIMS:
            va = a.get(dim, 50)
            vb = b.get(dim, 50)
            sq_sum += ((va - vb) / 100.0) ** 2
        return min(1.0, math.sqrt(sq_sum))
