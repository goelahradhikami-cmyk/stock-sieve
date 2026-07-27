"""
Stock Sieve Evolution Engine v1.0 — Production Ready

Naming: this is the PRODUCTION daily-cycle engine (EvolutionEngineV1,
data-driven selection + sandbox validation). The spec-level quarterly
machinery (EvolutionEngine) lives in spec_engine.py; genome data classes
live in genome.py.

Features:
  - Fitness from evaluation_results (real data)
  - Cosine distance diversity preservation
  - Sandbox backtest validation (child > parent +5%)
  - Evolution event logging (auditable)
  - Dry-Run mode (evaluate only, no modifications)
"""

import json
from dataclasses import dataclass, field

import numpy as np

from src.data.db import managed_connect
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AgentFitness:
    agent_id: str
    genome_hash: str
    strategy_genus: str
    fitness: float
    avg_alpha: float
    win_rate: float
    avg_drawdown: float
    sample_count: int
    identity_vector: np.ndarray = field(default_factory=lambda: np.zeros(6))


# ── Post-mortem → genome translation ─────────────────────
# Maps a post-mortem mutation_candidate's semantic `target` to a small nudge of
# one of the 8 investment_identity dimensions (clamped to [0, 100]). Targets
# without a known mapping are ignored by _apply_post_mortem_mutations (no-op),
# so unknown subsystems never corrupt a genome.
POST_MORTEM_DIMENSION_MAP = {
    "valuation_gate": ("valuation", 8),
    "market_regime_adapter": ("macro", 8),
    "thesis_engine.thesis_scoring": ("quality", -5),
    "thesis_engine.evidence_rules": ("quality", 5),
    "position_sizing.single_position": ("concentration", -8),
    "portfolio_constraints": ("concentration", -8),
}
# decision_graph targets are refined by their `filter` field.
DECISION_GRAPH_FILTER_MAP = {
    "momentum_confirmation": ("momentum", 8),
    "trailing_stop": ("patience", 8),
}


class EvolutionEngineV1:
    """Production evolution engine — data-driven selection + sandbox validation."""

    MIN_SAMPLES = 10  # Minimum evaluations to calculate fitness
    ELITE_FRACTION = 0.25  # Top 25%
    BOTTOM_FRACTION = 0.20  # Bottom 20%
    DIVERSITY_THRESHOLD = 0.3  # Cosine distance minimum
    SANDBOX_IMPROVEMENT = 0.05  # 5% improvement required
    SANDBOX_DAYS = 90  # 3 months backtest

    def __init__(self, db_path: str = "data/evaluation.db", dry_run: bool = False):
        self.db = managed_connect(self, db_path)
        self.dry_run = dry_run
        self.cycle_id = self._get_next_cycle_id()
        from .sandbox import SandboxValidator

        self.sandbox = SandboxValidator(db_path)

    def _get_next_cycle_id(self) -> int:
        row = self.db.execute("SELECT MAX(cycle_id) FROM evolution_events").fetchone()
        return (row[0] or 0) + 1

    def _log_event(
        self,
        event_type: str,
        agent_id: str = None,
        parent_id: str = None,
        description: str = "",
        details: dict = None,
    ):
        self.db.execute(
            """
            INSERT INTO evolution_events
            (cycle_id, event_type, agent_id, parent_id, description, details_json)
            VALUES (?,?,?,?,?,?)
        """,
            (
                self.cycle_id,
                event_type,
                agent_id,
                parent_id,
                description,
                json.dumps(details or {}),
            ),
        )

    # ═══════════════════════════════════════════════════════
    # 1. Fitness from evaluation_results
    # ═══════════════════════════════════════════════════════

    def _calculate_fitness(self, agent_id: str) -> AgentFitness | None:
        evals = self.db.execute(
            """
            SELECT
                AVG(alpha_vs_market) as avg_alpha,
                AVG(CASE WHEN alpha_vs_market > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                AVG(max_drawdown_during) as avg_dd,
                COUNT(*) as sample_count
            FROM evaluation_results
            WHERE research_decision_id IN (
                SELECT id FROM research_decisions WHERE agent_id = ?
            )
              AND eval_date >= date('now', '-1 year')
        """,
            (agent_id,),
        ).fetchone()

        if not evals or evals[3] < self.MIN_SAMPLES:
            return None

        avg_alpha = evals[0] or 0.0
        win_rate = evals[1] or 0.0
        avg_dd = evals[2] or 0.0
        sample_count = evals[3]

        # Calibration error (use 0.5 if column missing)
        cal_err = 0.5
        try:
            cal_row = self.db.execute(
                """
                SELECT AVG(ABS(alpha_error)) FROM evaluation_results
                WHERE research_decision_id IN (
                    SELECT id FROM research_decisions WHERE agent_id = ?
                ) AND alpha_error IS NOT NULL
            """,
                (agent_id,),
            ).fetchone()
            if cal_row and cal_row[0]:
                cal_err = cal_row[0]
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)

        fitness = (
            avg_alpha * 0.30
            + win_rate * 0.25
            + (-abs(avg_dd)) * 0.20
            + (1 - min(1.0, cal_err)) * 0.15
            + 0.10  # volatility weight (placeholder)
        )

        # Regime robustness (Commit 6-F.2): lower std across regimes = higher robustness
        try:
            regime_rows = self.db.execute(
                """
                SELECT AVG(alpha_vs_market) FROM evaluation_results
                WHERE research_decision_id IN (SELECT id FROM research_decisions WHERE agent_id = ?)
                GROUP BY market_regime
            """,
                (agent_id,),
            ).fetchall()
            if len(regime_rows) >= 2:
                alphas = [r[0] for r in regime_rows if r[0] is not None]
                if alphas and np.mean(alphas) != 0:
                    robustness = max(
                        0, min(1, 1 - (np.std(alphas) / (abs(np.mean(alphas)) + 0.01)))
                    )
                else:
                    robustness = 0.5
            else:
                robustness = 0.5
        except Exception:
            robustness = 0.5
        fitness += robustness * 0.10

        return AgentFitness(
            agent_id=agent_id,
            genome_hash=self._get_genome_hash(agent_id),
            strategy_genus=self._get_genus(agent_id),
            fitness=fitness,
            avg_alpha=avg_alpha,
            win_rate=win_rate,
            avg_drawdown=avg_dd,
            sample_count=sample_count,
            identity_vector=self._get_identity(agent_id),
        )

    def _get_genome_hash(self, agent_id: str) -> str:
        row = self.db.execute(
            "SELECT genome_hash FROM agent_genome_snapshots WHERE agent_id=? AND status='active' ORDER BY birth_date DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        return row[0] if row else "unknown"

    def _get_genus(self, agent_id: str) -> str:
        row = self.db.execute(
            "SELECT strategy_genus FROM agent_genome_snapshots WHERE agent_id=? LIMIT 1",
            (agent_id,),
        ).fetchone()
        return row[0] if row else "unknown"

    def _get_identity(self, agent_id: str) -> np.ndarray:
        row = self.db.execute(
            "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id=? AND status='active' ORDER BY birth_date DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if not row or not row[0]:
            return np.zeros(6)
        try:
            import yaml

            genome = yaml.safe_load(row[0]) or {}
        except Exception:
            return np.zeros(6)
        dims = genome.get("investment_identity", {}).get("dimensions", {})
        return np.array(
            [
                dims.get("valuation", 0),
                dims.get("quality", 0),
                dims.get("growth", 0),
                dims.get("momentum", 0),
                dims.get("contrarian", 0),
                dims.get("patience", 0),
            ]
        )

    # ═══════════════════════════════════════════════════════
    # 2. Cosine distance diversity
    # ═══════════════════════════════════════════════════════

    def _cosine_distance(self, a: np.ndarray, b: np.ndarray) -> float:
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 1.0
        return float(1 - np.dot(a, b) / (na * nb))

    def _is_diverse(self, agent: AgentFitness, population: list[AgentFitness]) -> bool:
        """True if agent is sufficiently different from all others in same genus."""
        min_dist = 1.0
        for other in population:
            if other.agent_id != agent.agent_id:
                d = self._cosine_distance(agent.identity_vector, other.identity_vector)
                min_dist = min(min_dist, d)
        return min_dist > self.DIVERSITY_THRESHOLD

    # ═══════════════════════════════════════════════════════
    # 3. Main evolution cycle
    # ═══════════════════════════════════════════════════════

    def run_cycle(self, pending_mutations=None) -> dict:
        logger.info(f"=== Evolution Cycle {self.cycle_id} (Dry-Run: {self.dry_run}) ===")

        # 3.1 Compute fitness for all active agents
        # Cold-start handling: newly evolved/founded agents with insufficient
        # T+N evaluations (sample_count < MIN_SAMPLES) cannot be scored yet.
        # Instead of skipping the whole cycle — which keeps the evolution loop
        # broken until enough history accrues — give them a neutral prior
        # (fitness=0.5) and protect them from elimination/elite selection so
        # they can build a track record. Only scoreless agents with real
        # evaluations participate in selection.
        agents = self._get_active_agents()
        fitness_list = []  # agents with real fitness (>= MIN_SAMPLES evals)
        cold_start = []  # active agents still in grace period
        for aid in agents:
            f = self._calculate_fitness(aid)
            if f:
                fitness_list.append(f)
            else:
                cs = AgentFitness(
                    agent_id=aid,
                    genome_hash=self._get_genome_hash(aid),
                    strategy_genus=self._get_genus(aid),
                    fitness=0.5,
                    avg_alpha=0.0,
                    win_rate=0.0,
                    avg_drawdown=0.0,
                    sample_count=0,
                    identity_vector=self._get_identity(aid),
                )
                cold_start.append(cs)
                self._log_event(
                    "COLD_START",
                    agent_id=aid,
                    description=f"Insufficient evaluations (<{self.MIN_SAMPLES}); grace period, neutral prior fitness",
                )

        total_active = len(fitness_list) + len(cold_start)
        if total_active < 3:
            logger.info(f"  Insufficient agents ({total_active}), skipping cycle")
            self._log_event("CYCLE_SKIPPED", description=f"Only {total_active} active agents total")
            return {"cycle_id": self.cycle_id, "status": "skipped", "reason": "insufficient_agents"}

        # Cold-start agents join the population (preserving diversity & size)
        # but are never selected as elites or eliminated — protected passengers
        # until they accumulate enough evaluations.
        population = fitness_list + cold_start
        population.sort(key=lambda x: x.fitness, reverse=True)

        # 3.2 Selection — only scored agents are eligible for elite/bottom.
        if len(fitness_list) >= 3:
            n = len(fitness_list)
            elite_n = max(1, int(n * self.ELITE_FRACTION))
            bottom_n = max(1, int(n * self.BOTTOM_FRACTION))
            scored_sorted = sorted(fitness_list, key=lambda x: x.fitness, reverse=True)
            elites = scored_sorted[:elite_n]
            bottom = scored_sorted[-bottom_n:]
        else:
            # Warmup: too few scored agents to prune meaningfully. Keep all
            # scored agents as elite candidates; defer elimination until the
            # track record grows. Crossover still runs if >=2 elites exist,
            # so the evolution loop stays functional during cold start.
            elites = sorted(fitness_list, key=lambda x: x.fitness, reverse=True)
            bottom = []
            self._log_event(
                "WARMUP",
                description=(
                    f"{len(fitness_list)} scored agent(s) (<3); elimination deferred, "
                    f"{len(cold_start)} in cold-start grace"
                ),
            )
            logger.info(
                f"  Warmup: {len(fitness_list)} scored | {len(cold_start)} cold-start grace | no elimination"
            )

        # Diversity exemption (compared against the whole population)
        survivors = []
        for agent in bottom:
            if self._is_diverse(agent, population):
                survivors.append(agent)
                self._log_event(
                    "DIVERSITY_EXEMPTION",
                    agent.agent_id,
                    description=f"Preserved: unique identity (fitness={agent.fitness:.4f})",
                )

        eliminated = [a for a in bottom if a not in survivors]
        elites.extend(survivors)

        logger.info(
            f"  Elites: {len(elites)} | Eliminated: {len(eliminated)} | Diversity-saved: {len(survivors)}"
        )

        if self.dry_run:
            self._log_event(
                "DRY_RUN", description=f"Would eliminate: {[a.agent_id for a in eliminated]}"
            )
            self.db.commit()
            return {
                "cycle_id": self.cycle_id,
                "mode": "dry_run",
                "eliminated_candidates": [a.agent_id for a in eliminated],
                "elites": [a.agent_id for a in elites],
            }

        # 3.3 Execute elimination
        for agent in eliminated:
            self._freeze_agent(agent.agent_id)
            self._log_event(
                "AGENT_FROZEN",
                agent.agent_id,
                description=f"Eliminated (fitness={agent.fitness:.4f})",
            )

        # 3.4 Crossover + mutation → sandbox → activate
        new_agents = []
        for i in range(0, len(elites) - 1, 2):
            parent_a, parent_b = elites[i], elites[i + 1]
            child_genome = self._crossover(parent_a, parent_b)
            child_genome = self._mutate(child_genome, pending_mutations)
            new_agents.append(child_genome)

        activated = []
        for child in new_agents:
            parent_id = child["identity"].get("parent_agent_id", "")
            parent_score = self._get_parent_fitness(parent_id)
            if self._sandbox_validate(child, parent_score):
                self._activate_agent(child)
                activated.append(child)
                self._log_event(
                    "AGENT_BORN",
                    child["identity"]["agent_id"],
                    parent_id=parent_id,
                    description="Sandbox passed, activated",
                )
            else:
                self._log_event(
                    "AGENT_REJECTED",
                    child["identity"]["agent_id"],
                    description="Sandbox validation failed",
                )

        self.db.commit()
        logger.info(f"  New agents: {len(activated)} activated")

        return {
            "cycle_id": self.cycle_id,
            "status": "warmup" if not bottom and cold_start else "ok",
            "eliminated": [a.agent_id for a in eliminated],
            "new_agents": [a["identity"]["agent_id"] for a in activated],
            "elite_fitness": [f"{a.agent_id}={a.fitness:.4f}" for a in elites[:5]],
            "cold_start": [a.agent_id for a in cold_start],
        }

    # ═══════════════════════════════════════════════════════
    # 4. Helpers
    # ═══════════════════════════════════════════════════════

    def _get_active_agents(self) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT agent_id FROM agent_genome_snapshots WHERE status='active'"
        ).fetchall()
        return [r[0] for r in rows]

    def _freeze_agent(self, agent_id: str):
        self.db.execute(
            "UPDATE agent_genome_snapshots SET status='frozen', frozen_date=date('now') WHERE agent_id=? AND status='active'",
            (agent_id,),
        )
        # Commit 6-H.1: failure propagation → penalize factor genomes
        try:
            from .failure_propagation import FailurePropagation

            FailurePropagation().propagate(agent_id)
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)

    def _get_parent_fitness(self, parent_id: str) -> float:
        if not parent_id:
            return 0.0
        f = self._calculate_fitness(parent_id)
        return f.fitness if f else 0.0

    def _crossover(self, a: AgentFitness, b: AgentFitness) -> dict:
        """Interpolate factor weights, inherit doctrine from parent A."""
        import yaml

        ga = (
            yaml.safe_load(
                self.db.execute(
                    "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id=? AND status='active'",
                    (a.agent_id,),
                ).fetchone()[0]
            )
            or {}
        )
        gb = (
            yaml.safe_load(
                self.db.execute(
                    "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id=? AND status='active'",
                    (b.agent_id,),
                ).fetchone()[0]
            )
            or {}
        )

        alpha = np.random.uniform(0.3, 0.7)
        child = ga.copy()
        child["identity"] = child.get("identity", {})
        child["identity"]["agent_id"] = (
            f"{a.strategy_genus}_gen{self.cycle_id}_{np.random.randint(1000, 9999)}"
        )
        child["identity"]["generation"] = (
            max(
                ga.get("identity", {}).get("generation", 1),
                gb.get("identity", {}).get("generation", 1),
            )
            + 1
        )
        child["identity"]["parent_agent_id"] = a.agent_id

        # Interpolate identity
        for dim in [
            "valuation",
            "quality",
            "growth",
            "momentum",
            "macro",
            "contrarian",
            "patience",
            "concentration",
        ]:
            va = ga.get("investment_identity", {}).get("dimensions", {}).get(dim, 50)
            vb = gb.get("investment_identity", {}).get("dimensions", {}).get(dim, 50)
            child.setdefault("investment_identity", {}).setdefault("dimensions", {})[dim] = int(
                va * alpha + vb * (1 - alpha)
            )

        return child

    def _apply_post_mortem_mutations(self, genome: dict, mutations: list) -> dict:
        """Bias a child genome toward fixing past failures.

        Translates post-mortem mutation_candidates (semantic targets like
        ``valuation_gate`` / ``position_sizing.single_position``) into small
        nudges of the ``investment_identity`` dimensions. Targets without a known
        mapping are skipped (no-op) so the genome always stays valid.
        """
        dims = genome.setdefault("investment_identity", {}).setdefault("dimensions", {})
        for m in mutations or []:
            if not isinstance(m, dict):
                continue
            target = m.get("target", "")
            if target == "decision_graph":
                sub = DECISION_GRAPH_FILTER_MAP.get(m.get("filter"))
            else:
                sub = POST_MORTEM_DIMENSION_MAP.get(target)
            if not sub:
                continue
            dim, d = sub
            current = dims.get(dim, 50)
            dims[dim] = max(0, min(100, current + d))
        return genome

    def _mutate(self, genome: dict, pending_mutations=None) -> dict:
        """Random ±2% factor weight adjustment, then post-mortem-guided nudges.

        ``pending_mutations`` (from PostMortemAnalyzer via PostMortemEngine) bias
        the child genome's identity dimensions toward fixing recurring failures.
        When ``None``/empty, behavior is identical to before.
        """
        for _family, config in genome.get("factor_model", {}).items():
            if isinstance(config, dict) and "weight" in config:
                delta = np.random.uniform(-0.02, 0.02)
                config["weight"] = max(0.0, min(0.50, config["weight"] + delta))
        if pending_mutations:
            self._apply_post_mortem_mutations(genome, pending_mutations)
        return genome

    def _sandbox_validate(self, child_genome: dict, parent_score: float) -> bool:
        """Delegates to SandboxValidator for three-layer validation."""
        parent_id = child_genome["identity"].get("parent_agent_id", "")
        self.db.commit()  # Release lock before sandbox opens its own connection
        result = self.sandbox.validate(child_genome, parent_id, self.cycle_id)

        if result.status == "rejected":
            self._log_event(
                "SANDBOX_REJECTED",
                child_genome["identity"]["agent_id"],
                parent_id=parent_id,
                description=f"Improvement: {result.improvement:.3f}",
                details={"reasons": result.reject_reasons},
            )

        return result.status == "approved"

    def _activate_agent(self, genome: dict):
        import hashlib
        import json

        gh = hashlib.sha256(json.dumps(genome).encode()).hexdigest()[:16]
        self.db.execute(
            "INSERT INTO agent_genome_snapshots (agent_id, strategy_genus, strategy_species, generation, parent_agent_id, genome_hash, genome_yaml, birth_date, status) "
            "VALUES (?,?,?,?,?,?,?,date('now'),'active')",
            (
                genome["identity"]["agent_id"],
                genome["identity"].get("strategy_genus", "unknown"),
                genome["identity"].get("strategy_species", "unknown"),
                genome["identity"].get("generation", 1),
                genome["identity"].get("parent_agent_id"),
                gh,
                json.dumps(genome),
            ),
        )
