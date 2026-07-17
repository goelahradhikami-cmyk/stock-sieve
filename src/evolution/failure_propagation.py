"""
Failure Propagation — Agent death penalizes associated factor genomes.

Commit 6-H.1 Fix 3: When an agent is frozen, reduce fitness of its
associated factor genomes. After 3+ rejections, mark as 'weakening'.
"""

from src.data.db import managed_connect


class FailurePropagation:
    """Propagate agent elimination consequences to factor genome pool."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)

    def propagate(self, agent_genome_id: str):
        """After agent freeze, penalize its factor genomes."""
        rows = self.db.execute("""
            SELECT DISTINCT factor_genome_id, fitness_score
            FROM genome_compatibility
            WHERE agent_genome_id = ? AND status = 'active'
        """, (agent_genome_id,)).fetchall()

        for factor_id, current_fitness in rows:
            new_fitness = (current_fitness or 0.5) * 0.95

            try:
                self.db.execute(
                    "UPDATE factor_genome SET fitness_score=? WHERE genome_id=?",
                    (new_fitness, factor_id)
                )
            except Exception:
                pass

            penalty_count = self.db.execute("""
                SELECT COUNT(*) FROM genome_compatibility
                WHERE factor_genome_id = ? AND status = 'rejected'
            """, (factor_id,)).fetchone()

            if penalty_count and penalty_count[0] >= 3:
                try:
                    self.db.execute(
                        "UPDATE factor_genome SET status='weakening' WHERE genome_id=?",
                        (factor_id,)
                    )
                except Exception:
                    pass

        self.db.commit()
