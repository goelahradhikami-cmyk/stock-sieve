"""
Risk Genome — The 4th DNA strand: "how do I survive?"

Commit 6-I.1: Risk genome evolution with drawdown response,
volatility control, exposure management, and kill switch.

Quad evolution: Agent × Factor × Portfolio × Risk
"""

from copy import deepcopy

import numpy as np

from src.data.db import managed_connect

# ═══════════════════════════════════════════════════════════
# Default Risk Genome
# ═══════════════════════════════════════════════════════════

DEFAULT_RISK_GENOME = {
    "drawdown_response": {
        "5%": {"action": "review_positions", "exposure": 1.0},
        "10%": {"action": "reduce_risk", "exposure": 0.80},
        "15%": {"action": "reduce_half", "exposure": 0.50},
        "25%": {"action": "emergency_exit", "exposure": 0.10},
    },
    "volatility_control": {
        "vol_spike_20pct": {"action": "hedge", "cash_buffer": 0.15},
        "vol_spike_50pct": {"action": "reduce_50pct", "cash_buffer": 0.30},
    },
    "exposure_control": {
        "max_single_stock": 0.15,
        "max_sector": 0.30,
        "max_factor_exposure": 0.45,
    },
    "liquidity_control": {
        "min_daily_volume_wan": 5000,
        "max_position_vs_volume": 0.05,
    },
    "model_failure_response": {
        "consecutive_losses_3": "reduce_exposure_20pct",
        "factor_failure": "freeze_factor",
        "regime_mismatch": "increase_cash_15pct",
    },
    "mutation_type": None,
    "generation": 0,
}


# ═══════════════════════════════════════════════════════════
# Risk Mutation Engine
# ═══════════════════════════════════════════════════════════


class RiskMutationEngine:
    """Mutate risk genomes with 6 mutation types."""

    MUTATION_TYPES = [
        "tighten_drawdown",
        "loosen_drawdown",
        "increase_cash",
        "reduce_turnover",
        "increase_reaction_speed",
        "risk_aversion_shift",
    ]

    def mutate(self, parent: dict, agent_identity: dict = None) -> dict:
        mutation_type = np.random.choice(self.MUTATION_TYPES)
        child = deepcopy(parent)

        if mutation_type == "tighten_drawdown":
            for threshold in ["5%", "10%", "15%", "25%"]:
                if threshold in child.get("drawdown_response", {}):
                    current = child["drawdown_response"][threshold].get("exposure", 1.0)
                    child["drawdown_response"][threshold]["exposure"] = max(0.3, current - 0.10)

        elif mutation_type == "loosen_drawdown":
            for threshold in ["5%", "10%"]:
                if threshold in child.get("drawdown_response", {}):
                    current = child["drawdown_response"][threshold].get("exposure", 1.0)
                    child["drawdown_response"][threshold]["exposure"] = min(1.0, current + 0.10)

        elif mutation_type == "increase_cash":
            for condition in child.get("volatility_control", {}):
                current = child["volatility_control"][condition].get("cash_buffer", 0.1)
                child["volatility_control"][condition]["cash_buffer"] = min(0.6, current + 0.05)

        elif mutation_type == "risk_aversion_shift":
            if agent_identity and agent_identity.get("dimensions", {}).get("patience", 50) > 70:
                child["drawdown_response"]["25%"]["action"] = "review_only"
            else:
                child["drawdown_response"]["25%"]["action"] = "emergency_exit"

        child["mutation_type"] = mutation_type
        child["generation"] = parent.get("generation", 0) + 1
        return child


# ═══════════════════════════════════════════════════════════
# Risk Fitness Evaluator
# ═══════════════════════════════════════════════════════════


class RiskFitnessEvaluator:
    """Score risk genome based on survival statistics."""

    def evaluate(self, risk_genome: dict, survival_stats: dict) -> float:
        fitness = (
            (survival_stats.get("crisis_survival_score", 0) or 0) * 0.40
            + (survival_stats.get("recovery_speed", 0) or 0) * 0.25
            + (survival_stats.get("prediction_accuracy", 0) or 0) * 0.20
            + (1 - (survival_stats.get("false_alarm_rate", 0) or 0)) * 0.15
        )
        return max(0, min(1, round(fitness, 3)))


# ═══════════════════════════════════════════════════════════
# Kill Switch
# ═══════════════════════════════════════════════════════════


class KillSwitch:
    """Emergency control gate — checked before any automatic action."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS emergency_control (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                system_status TEXT DEFAULT 'NORMAL',
                max_exposure REAL DEFAULT 0.95,
                allow_evolution INTEGER DEFAULT 1,
                allow_trading INTEGER DEFAULT 1,
                triggered_by TEXT,
                triggered_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Ensure at least one row exists
        if not self.db.execute("SELECT id FROM emergency_control LIMIT 1").fetchone():
            self.db.execute("INSERT INTO emergency_control (system_status) VALUES ('NORMAL')")
        self.db.commit()

    def can_auto_execute(self) -> bool:
        row = self.db.execute(
            "SELECT system_status, allow_evolution FROM emergency_control ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return bool(row and row[0] == "NORMAL" and row[1] == 1)

    def can_trade(self) -> bool:
        row = self.db.execute(
            "SELECT system_status, allow_trading FROM emergency_control ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return bool(row and row[0] in ("NORMAL", "CAUTION") and row[1] == 1)

    def trigger_emergency(self, reason: str, allow_evolution: bool = False):
        self.db.execute(
            """
            INSERT INTO emergency_control
            (system_status, max_exposure, allow_evolution, allow_trading, triggered_by, triggered_at)
            VALUES ('EMERGENCY', 0.30, ?, 0, ?, datetime('now'))
        """,
            (1 if allow_evolution else 0, reason),
        )
        self.db.commit()
        print(f"🚨 EMERGENCY: {reason}")

    def resume_normal(self):
        self.db.execute("""
            INSERT INTO emergency_control (system_status, max_exposure, allow_evolution, allow_trading)
            VALUES ('NORMAL', 0.95, 1, 1)
        """)
        self.db.commit()
        print("✅ System resumed NORMAL")

    def can_evolve(self) -> bool:
        """Gate for automatic evolution cycles (used by daily_run / cli evolve)."""
        return self.can_auto_execute()

    def current_state(self) -> str:
        """Return current system status (NORMAL / CAUTION / EMERGENCY)."""
        row = self.db.execute(
            "SELECT system_status FROM emergency_control ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else "NORMAL"
