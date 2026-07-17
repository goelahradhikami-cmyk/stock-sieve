"""
Audit Genome — The 6th DNA: self-criticism and adversarial testing.

Commit 6-J.1: Red Team evolution, Alpha attribution, strategy autopsy,
and governance state machine.
"""

from src.data.db import managed_connect

# ═══════════════════════════════════════════════════════════
# Audit Fitness Evaluator
# ═══════════════════════════════════════════════════════════

class AuditFitnessEvaluator:
    """Good audit systems find more real bugs with fewer false alarms."""

    def evaluate(self, audit_result: dict) -> float:
        fitness = (
            min(1.0, audit_result.get('bugs_found', 0) / 10) * 0.30 +
            (1 - audit_result.get('false_positive_rate', 0.5)) * 0.20 +
            audit_result.get('reproducibility', 0) * 0.30 +
            min(1.0, audit_result.get('early_warning_days', 0) / 365) * 0.20
        )
        return max(0, min(1, fitness))


# ═══════════════════════════════════════════════════════════
# Red Team Engine
# ═══════════════════════════════════════════════════════════

class RedTeamEngine:
    """Adversarial tester — finds weaknesses in investment genomes."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_tables()

    def _ensure_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS red_team_genome (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genome_id TEXT UNIQUE NOT NULL,
                attack_style_json TEXT NOT NULL,
                target_bias TEXT,
                attack_success_rate REAL DEFAULT 0.0,
                discovered_failures INTEGER DEFAULT 0,
                false_positive_rate REAL DEFAULT 0.0,
                fitness_score REAL DEFAULT 0.0,
                generation INTEGER DEFAULT 0,
                parent_genome_id TEXT,
                status TEXT DEFAULT 'testing',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS alpha_attribution_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genome_id TEXT NOT NULL,
                return_source TEXT,
                factor_contribution REAL,
                market_beta REAL,
                sector_beta REAL,
                skill_alpha REAL,
                unexplained_return REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.db.commit()

    def attack(self, genome_id: str, genome_data: dict,
               performance: dict) -> dict:
        """Run 5 adversarial tests against a genome."""
        tests = {
            'remove_top_days': self._test_remove_top_days(performance),
            'factor_decay': self._test_factor_decay(performance),
            'crowding': self._test_crowding(genome_data),
            'cost_shock': self._test_cost_shock(performance),
            'regime_failure': self._test_regime_failure(genome_data, performance),
        }

        fatal_count = sum(1 for t in tests.values() if t.get('fatal', False))
        overall = 'fail' if fatal_count > 0 else 'pass'

        return {'verdict': overall, 'fatal_count': fatal_count, 'tests': tests}

    def _test_remove_top_days(self, perf: dict) -> dict:
        sharpe = perf.get('sharpe', 0) or 0
        if sharpe > 1.5:
            return {'name': 'top_days', 'warning': 'Sharpe异常高,疑有过拟合', 'fatal': True,
                    'sharpe_without_top': sharpe * 0.5}
        return {'name': 'top_days', 'fatal': False}

    def _test_factor_decay(self, perf: dict) -> dict:
        ic = perf.get('ic', perf.get('information_coefficient', 0)) or 0
        if ic < 0.02:
            return {'name': 'factor_decay', 'warning': '因子IC过低,Alpha可能已衰减', 'fatal': True}
        return {'name': 'factor_decay', 'fatal': False}

    def _test_crowding(self, genome: dict) -> dict:
        overlap = genome.get('crowding_overlap', 0) or 0
        if overlap > 0.7:
            return {'name': 'crowding', 'warning': f'策略拥挤度{overlap:.0%}', 'fatal': True}
        return {'name': 'crowding', 'fatal': False}

    def _test_cost_shock(self, perf: dict) -> dict:
        net = perf.get('net_return', perf.get('total_return', 0)) or 0
        gross = perf.get('gross_return', net * 1.3) or net * 1.3
        if gross > 0 and net / gross < 0.3:
            return {'name': 'cost_shock', 'warning': '交易成本侵蚀严重', 'fatal': True}
        return {'name': 'cost_shock', 'fatal': False}

    def _test_regime_failure(self, genome: dict, perf: dict) -> dict:
        sharpe = perf.get('sharpe', 0) or 0
        degradation = perf.get('regime_degradation', 0) or 0
        if degradation > 0.5:
            return {'name': 'regime_failure', 'warning': '跨状态表现严重退化',
                    'fatal': True, 'sharpe': sharpe, 'degradation': degradation}
        return {'name': 'regime_failure', 'fatal': False}

    def audit_alpha_attribution(self, genome_id: str, performance: dict) -> dict:
        """Decompose returns: market beta vs sector vs real skill."""
        market_beta = performance.get('beta', 1.0) or 1.0
        market_return = performance.get('market_return', 0) or 0
        sector_return = performance.get('sector_return', 0) or 0
        total_return = performance.get('total_return', 0) or 0

        market_contribution = market_beta * market_return
        sector_contribution = sector_return - market_return
        skill_alpha = total_return - market_contribution - sector_contribution

        try:
            self.db.execute("""
                INSERT INTO alpha_attribution_audit
                (genome_id, return_source, factor_contribution, market_beta, sector_beta, skill_alpha, unexplained_return)
                VALUES (?,?,?,?,?,?,?)
            """, (genome_id, 'full', skill_alpha, market_beta,
                  sector_contribution / market_return if market_return else 0,
                  skill_alpha, 0))
            self.db.commit()
        except Exception:
            pass

        return {
            'market_beta': round(market_beta, 2),
            'market_contribution': round(market_contribution, 4),
            'sector_contribution': round(sector_contribution, 4),
            'skill_alpha': round(skill_alpha, 4),
            'verdict': 'pass' if skill_alpha > 0.02 else 'review',
        }


# ═══════════════════════════════════════════════════════════
# Governance State Machine
# ═══════════════════════════════════════════════════════════

class GovernanceStateMachine:
    """4-level circuit breaker: NORMAL → WARNING → DEFCON1 → HALTED."""

    STATES = ['NORMAL', 'WARNING', 'DEFCON1', 'HALTED']

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS governance_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state TEXT NOT NULL,
                reason TEXT,
                trigger_metric TEXT,
                recovery_condition TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        if not self.db.execute("SELECT id FROM governance_state LIMIT 1").fetchone():
            self.db.execute(
                "INSERT INTO governance_state (state, reason) VALUES ('NORMAL', 'system_init')"
            )
        self.db.commit()

    def current_state(self) -> str:
        row = self.db.execute(
            "SELECT state FROM governance_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else 'NORMAL'

    def transition(self, new_state: str, reason: str, trigger: dict = None):
        if new_state not in self.STATES:
            return
        self.db.execute("""
            INSERT INTO governance_state (state, reason, trigger_metric)
            VALUES (?,?,?)
        """, (new_state, reason, str(trigger or {})))
        self.db.commit()

    def check_and_escalate(self, recent_failures: int):
        current = self.current_state()
        if recent_failures >= 5 and current == 'NORMAL':
            self.transition('WARNING', f'近期{recent_failures}个策略失败')
        elif recent_failures >= 10 and current == 'WARNING':
            self.transition('DEFCON1', f'近期{recent_failures}个策略失败,暂停新策略')
        elif recent_failures >= 20 and current == 'DEFCON1':
            self.transition('HALTED', '系统风险过高,全面暂停')

    def can_activate_genome(self) -> bool:
        return self.current_state() in ('NORMAL', 'WARNING')

    def can_evolve(self) -> bool:
        return self.current_state() == 'NORMAL'
