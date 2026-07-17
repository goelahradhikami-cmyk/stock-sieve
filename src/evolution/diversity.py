"""
Diversity Evaluator — Prevents factor genome collapse toward a single elite.

Commit 6-H.1 Fix 1: Scores ecological niche uniqueness for each factor genome.
Integrated into fitness calculation as 10% weight.
"""

import json

import numpy as np

from src.data.db import managed_connect


class DiversityEvaluator:
    """Prevent all factor genomes from converging to one peak."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)

    def score_factor_diversity(self, factor_genome: dict) -> dict:
        """Calculate ecological niche uniqueness of a factor genome."""
        population = self._get_active_factor_genomes()
        if not population:
            return {'similarity_to_population': 0, 'unique_alpha_score': 1.0,
                    'regime_gap_score': 0, 'diversity_score': 1.0}

        target_vec = self._genome_to_vector(factor_genome)
        similarities = []
        for g in population:
            try:
                g_vec = self._genome_to_vector(g)
                sim = self._cosine_similarity(target_vec, g_vec)
                similarities.append(sim)
            except Exception:
                pass

        avg_similarity = float(np.mean(similarities)) if similarities else 0
        unique_alpha = 1 - avg_similarity

        # Regime gap: what regimes does this cover that others don't?
        target_regimes = set(factor_genome.get('preferred_regimes', []))
        population_regimes = set()
        for g in population:
            population_regimes.update(g.get('preferred_regimes', []))
        regime_gap = len(target_regimes - population_regimes) / max(1, len(target_regimes))

        diversity_score = unique_alpha * 0.6 + regime_gap * 0.4

        try:
            self.db.execute("""
                INSERT OR REPLACE INTO genome_diversity_score
                (genome_id, similarity_to_population, unique_alpha_score, regime_gap_score, diversity_score)
                VALUES (?,?,?,?,?)
            """, (factor_genome.get('genome_id', 'unknown'),
                  avg_similarity, unique_alpha, regime_gap, diversity_score))
            self.db.commit()
        except Exception:
            pass

        return {
            'similarity_to_population': round(avg_similarity, 3),
            'unique_alpha_score': round(unique_alpha, 3),
            'regime_gap_score': round(regime_gap, 3),
            'diversity_score': round(diversity_score, 3),
        }

    def _genome_to_vector(self, genome: dict) -> np.ndarray:
        """Factor genome → weight vector."""
        factors = genome.get('factors', [])
        vec = np.array([float(f.get('weight', 0) or 0) for f in factors])
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        if len(a) != len(b):
            m = min(len(a), len(b))
            a, b = a[:m], b[:m]
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        return float(np.dot(a, b) / (na * nb + 1e-9))

    def _get_active_factor_genomes(self) -> list[dict]:
        try:
            rows = self.db.execute(
                "SELECT factors_json, preferred_regimes FROM factor_genome WHERE status='active'"
            ).fetchall()
            return [{'factors': json.loads(r[0]) if r[0] else [],
                     'preferred_regimes': json.loads(r[1]) if r[1] else []}
                    for r in rows]
        except Exception:
            return []
