"""
Adversarial Evolution Runner — Crisis Genome vs Risk Genome co-evolution.

Commit 6-I.2.1: Attackers find weaknesses, defenders learn immunity.
Prevents "turtle gene" where risk genomes evolve to always stay empty.
"""

import numpy as np

from src.data.db import managed_connect
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
# Monte Carlo Crisis Test (Fix 2)
# ═══════════════════════════════════════════════════════════


class MonteCarloCrisisTester:
    """Multi-path crisis simulation for robust survival probability."""

    def __init__(self, crisis_engine):
        self.engine = crisis_engine

    def test(self, organism: dict, scenario: dict, iterations: int = 200) -> dict:
        """Run N random paths through a crisis, compute survival probability."""
        survivors = 0
        drawdowns = []
        recoveries = []

        for _ in range(iterations):
            result = self.engine.test_organism(organism, scenario)
            if result["survival"]:
                survivors += 1
            drawdowns.append(result["max_drawdown"])
            recoveries.append(result["recovery_days"])

        return {
            "survival_probability": survivors / iterations,
            "avg_max_drawdown": float(np.mean(drawdowns)),
            "worst_drawdown": float(min(drawdowns)),
            "avg_recovery": float(np.mean(recoveries)),
            "iterations": iterations,
        }


# ═══════════════════════════════════════════════════════════
# Risk Fitness with Capital Efficiency (Fix 1)
# ═══════════════════════════════════════════════════════════


class AdversarialRiskFitness:
    """Prevents turtle genes by penalizing excessive caution."""

    def evaluate(self, risk_genome: dict, survival_stats: dict) -> float:
        """Fitness with capital efficiency check."""
        # Extinction check
        worst_dd = survival_stats.get("worst_drawdown", 0)
        if abs(worst_dd) > 0.5:
            return 0.0

        # Anti-turtle: penalize if always at minimal exposure
        avg_exposure = survival_stats.get("avg_exposure", 0.5)

        fitness = (
            survival_stats.get("survival_rate", 0) * 0.35
            + survival_stats.get("normal_market_return", 0) * 0.20
            + survival_stats.get("recovery_speed", 0) * 0.15
            + survival_stats.get("defense_accuracy", 0) * 0.10
            + avg_exposure * 0.10  # Capital efficiency
            + survival_stats.get("capital_efficiency", 0) * 0.10
        )
        return max(0, min(1, fitness))

    def is_turtle_gene(self, risk_genome: dict) -> bool:
        """Detect overly conservative risk genomes."""
        dd_response = risk_genome.get("drawdown_response", {})
        # If 5% DD triggers emergency exit → turtle gene
        return dd_response.get("5%", {}).get("action") in ("emergency_exit", "reduce_half")


# ═══════════════════════════════════════════════════════════
# Immune Memory (Fix 4)
# ═══════════════════════════════════════════════════════════


class ImmuneMemory:
    """Inheritable crisis defense memory."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)

    def record(self, risk_genome_id: str, crisis_type: str, action: str, outcome: float):
        self.db.execute(
            """
            INSERT INTO risk_gene_memory
            (risk_genome_id, crisis_type, action_taken, outcome)
            VALUES (?,?,?,?)
        """,
            (risk_genome_id, crisis_type, action, outcome),
        )
        self.db.commit()

    def inherit(self, parent_id: str, child_id: str):
        """Child inherits successful defenses from parent."""
        memories = self.db.execute(
            """
            SELECT crisis_type, action_taken, outcome FROM risk_gene_memory
            WHERE risk_genome_id = ? AND outcome > 0.7
        """,
            (parent_id,),
        ).fetchall()

        for crisis_type, action, outcome in memories:
            self.db.execute(
                """
                INSERT INTO risk_gene_memory
                (risk_genome_id, crisis_type, action_taken, outcome, inherited_by)
                VALUES (?,?,?,?,?)
            """,
                (child_id, crisis_type, action, outcome, parent_id),
            )
        self.db.commit()
        return len(memories)


# ═══════════════════════════════════════════════════════════
# Adversarial Evolution Runner (Fix 5)
# ═══════════════════════════════════════════════════════════


class AdversarialEvolutionRunner:
    """Crisis Genome (attacker) vs Risk Genome (defender) co-evolution."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_tables()
        from src.simulation.crisis_engine import CrisisSimulationEngine

        self.crisis_engine = CrisisSimulationEngine(db_path)
        self.mc_tester = MonteCarloCrisisTester(self.crisis_engine)
        self.immune_memory = ImmuneMemory(db_path)
        self.fitness = AdversarialRiskFitness()

    def _ensure_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS crisis_factor_shock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_id TEXT NOT NULL, factor_name TEXT NOT NULL,
                shock REAL, recovery_days INTEGER,
                FOREIGN KEY (scenario_id) REFERENCES crisis_scenarios(scenario_id)
            );
            CREATE TABLE IF NOT EXISTS risk_gene_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                risk_genome_id TEXT NOT NULL, crisis_type TEXT NOT NULL,
                action_taken TEXT, outcome REAL,
                learning_weight REAL DEFAULT 1.0,
                inherited_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS crisis_genome (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genome_id TEXT UNIQUE NOT NULL, parent_genome_id TEXT,
                generation INTEGER DEFAULT 0,
                market_shock_json TEXT NOT NULL,
                factor_attack_json TEXT, liquidity_attack_json TEXT,
                correlation_attack_json TEXT,
                severity REAL DEFAULT 0.5, kill_rate REAL DEFAULT 0.0,
                fitness REAL DEFAULT 0.0, status TEXT DEFAULT 'testing',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._seed_factor_shocks()
        self.db.commit()

    def _seed_factor_shocks(self):
        shocks = [
            ("GFC_2008", "value", -0.25, 250),
            ("GFC_2008", "momentum", -0.40, 300),
            ("GFC_2008", "quality", 0.10, 180),
            ("COVID_2020", "momentum", -0.30, 60),
            ("COVID_2020", "growth", -0.20, 90),
        ]
        for s in shocks:
            self.db.execute(
                """
                INSERT OR IGNORE INTO crisis_factor_shock
                (scenario_id, factor_name, shock, recovery_days)
                VALUES (?,?,?,?)
            """,
                s,
            )
        self.db.commit()

    def run_cycle(self) -> dict:
        """One cycle of adversarial co-evolution."""
        # 1. Load attack crises
        attack_crises = self._get_crisis_scenarios()

        # 2. Test all risk genomes
        risk_genomes = self._get_risk_genomes()
        if not risk_genomes or not attack_crises:
            return {"survivors": 0, "crises": 0}

        survivors = []
        for risk in risk_genomes:
            total_survival = 0
            for crisis in attack_crises:
                mc_result = self.mc_tester.test({"risk": risk}, crisis, iterations=100)
                total_survival += mc_result["survival_probability"]
                # Record immune memory
                if mc_result["survival_probability"] > 0.7:
                    self.immune_memory.record(
                        risk.get("genome_id", "unknown"),
                        crisis.get("name", "unknown"),
                        "survived",
                        mc_result["survival_probability"],
                    )

            avg_survival = total_survival / len(attack_crises)
            if avg_survival >= 0.5 and not self.fitness.is_turtle_gene(risk):
                survivors.append(risk)

        # 3. Update crisis fitness (kill rate)
        total_risks = len(risk_genomes)
        for crisis in attack_crises:
            kill_count = sum(1 for r in risk_genomes if r not in survivors)
            kill_rate = kill_count / total_risks if total_risks > 0 else 0
            try:
                self.db.execute(
                    "UPDATE crisis_genome SET kill_rate=?, fitness=? WHERE genome_id=?",
                    (kill_rate, kill_count, crisis.get("scenario_id", "")),
                )
            except Exception as exc:
                logger.warning("operation failed (was silently ignored): %s", exc)

        self.db.commit()
        return {
            "survivors": len(survivors),
            "total_risks": total_risks,
            "crises_evolved": len(attack_crises),
            "kill_rate": round(1 - len(survivors) / total_risks, 2) if total_risks > 0 else 0,
        }

    def _get_crisis_scenarios(self) -> list[dict]:
        return self.crisis_engine._load_scenarios()

    def _get_risk_genomes(self) -> list[dict]:
        try:
            rows = self.db.execute(
                "SELECT * FROM risk_genome WHERE status='testing' LIMIT 20"
            ).fetchall()
            cols = [d[1] for d in self.db.execute("PRAGMA table_info(risk_genome)")]
            return [dict(zip(cols, r, strict=False)) for r in rows]
        except Exception:
            return []
