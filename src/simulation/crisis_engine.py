"""
Crisis Simulation Engine — Extinction pressure for Risk Genomes.

Commit 6-I.2: Tests investment organisms against historical crises
and synthetic black swans. Only survivors reproduce.

5 preset scenarios: GFC 2008, COVID 2020, Rate Hike 2022, AI Bubble, Liquidity Crisis
"""

import numpy as np

from src.data.db import managed_connect

# ═══════════════════════════════════════════════════════════
# Preset crisis scenarios
# ═══════════════════════════════════════════════════════════

PRESET_SCENARIOS = [
    ("GFC_2008", "全球金融危机", "historical", -0.55, 3.5, 0.8, 0.9, 420, "crisis", 1.0),
    ("COVID_2020", "新冠疫情崩盘", "historical", -0.35, 4.0, 0.7, 0.85, 65, "crisis", 0.85),
    ("RATE_HIKE_2022", "加息杀估值", "historical", -0.20, 2.0, 0.4, 0.5, 180, "bear", 0.65),
    ("AI_BUBBLE", "AI泡沫破裂", "synthetic", -0.35, 2.5, 0.5, 0.6, 180, "rotation", 0.75),
    ("LIQUIDITY_CRISIS", "流动性危机", "synthetic", -0.25, 3.0, 0.95, 0.8, 90, "crisis", 0.90),
]


class CrisisSimulationEngine:
    """Test investment organisms against crisis scenarios."""

    EXTINCTION_THRESHOLD = 30.0   # NAV < 30 = dead
    SURVIVAL_THRESHOLD = 50.0    # NAV > 50 = survived

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_tables()
        self._seed_scenarios()

    def _ensure_tables(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS crisis_scenarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                category TEXT,
                equity_shock REAL,
                volatility_multiplier REAL,
                liquidity_shock REAL,
                correlation_shift REAL,
                growth_factor_shock REAL,
                valuation_compression REAL,
                duration_days INTEGER,
                regime TEXT,
                severity REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.commit()

    def _seed_scenarios(self):
        for s in PRESET_SCENARIOS:
            self.db.execute("""
                INSERT OR IGNORE INTO crisis_scenarios
                (scenario_id, name, category, equity_shock, volatility_multiplier,
                 liquidity_shock, correlation_shift, duration_days, regime, severity)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, s)
        self.db.commit()

    def test_organism(self, organism: dict, scenario: dict) -> dict:
        """Run a single crisis pressure test on an organism."""
        risk = organism.get('risk', {})
        portfolio = organism.get('portfolio', {})

        # Build shocked market state
        market_shock = {
            'equity_return': scenario.get('equity_shock', -0.3),
            'volatility': (scenario.get('volatility_multiplier', 2.0) or 2.0) * 0.15,
            'liquidity': 1 - (scenario.get('liquidity_shock', 0.5) or 0.5),
            'correlation_shift': scenario.get('correlation_shift', 0.6) or 0.6,
            'regime': scenario.get('regime', 'crisis'),
            'duration': scenario.get('duration_days', 90),
        }

        # Apply risk response
        defense_actions = self._apply_risk_response(risk, market_shock)
        portfolio_adj = self._adjust_portfolio(portfolio, defense_actions)

        # Simulate NAV path
        nav_path = [100.0]
        daily_vol = market_shock['volatility'] / np.sqrt(252)
        days = int(market_shock['duration'])

        for day in range(days):
            daily_ret = np.random.normal(
                market_shock['equity_return'] / max(1, days / 20),
                daily_vol
            )
            effective_exposure = portfolio_adj.get('effective_exposure', 0.7)
            nav_path.append(nav_path[-1] * (1 + daily_ret * effective_exposure))
            if nav_path[-1] < self.EXTINCTION_THRESHOLD:
                break

        final_nav = nav_path[-1]
        max_dd = min(
            (nav / max(nav_path[:i+1]) - 1)
            for i, nav in enumerate(nav_path) if max(nav_path[:i+1]) > 0
        ) if nav_path else 0

        survival = 1 if final_nav > self.SURVIVAL_THRESHOLD else 0
        min_idx = nav_path.index(min(nav_path)) if nav_path else 0
        recovery_days = len(nav_path) - min_idx if survival else days

        return {
            'survival': survival,
            'final_nav': round(final_nav, 1),
            'max_drawdown': round(max_dd, 3),
            'recovery_days': recovery_days,
            'defense_actions': defense_actions,
        }

    def _apply_risk_response(self, risk: dict, market: dict) -> list[dict]:
        """Generate defense actions from risk genome."""
        actions = []
        dd_response = risk.get('drawdown_response', {})

        for threshold, config in dd_response.items():
            pct = float(threshold.replace('%', ''))
            exposure = config.get('exposure', 1.0)
            action = config.get('action', 'hold')

            if pct >= 25 and market['regime'] == 'crisis':
                actions.append({
                    'type': action, 'target_exposure': min(0.3, exposure),
                    'trigger': f'{threshold} DD in crisis'
                })
            elif pct >= 15:
                actions.append({
                    'type': action, 'target_exposure': min(0.5, exposure),
                    'trigger': f'{threshold} DD'
                })

        return actions

    def _adjust_portfolio(self, portfolio: dict, actions: list) -> dict:
        adjusted = dict(portfolio) if portfolio else {}
        for a in actions:
            if a['type'] in ('reduce_exposure', 'emergency_exit', 'reduce_half',
                             'review_only', 'review_positions', 'reduce_risk'):
                adjusted['effective_exposure'] = a['target_exposure']
        if 'effective_exposure' not in adjusted:
            adjusted['effective_exposure'] = 0.75
        return adjusted

    def run_crisis_battery(self, organism: dict) -> dict:
        """Full crisis test suite: historical + synthetic + 1 black swan."""
        scenarios = self._load_scenarios()
        # Add a random black swan
        scenarios.append(self.generate_black_swan())

        results = []
        for scenario in scenarios:
            result = self.test_organism(organism, scenario)
            result['scenario_name'] = scenario.get('name', 'unknown')
            results.append(result)

        survival_rate = sum(r['survival'] for r in results) / len(results) if results else 0
        avg_dd = float(np.mean([r['max_drawdown'] for r in results])) if results else 0
        avg_recovery = float(np.mean([r['recovery_days'] for r in results])) if results else 999

        return {
            'survival_rate': round(survival_rate, 2),
            'avg_max_drawdown': round(avg_dd, 3),
            'avg_recovery_days': avg_recovery,
            'scenario_results': results,
        }

    def generate_black_swan(self) -> dict:
        """Generate a random synthetic black swan event."""
        return {
            'scenario_id': f'BSWAN_{np.random.randint(10000, 99999)}',
            'name': f'黑天鹅#{np.random.randint(1000,9999)}',
            'category': 'black_swan',
            'equity_shock': float(np.random.uniform(-0.7, -0.2)),
            'volatility_multiplier': float(np.random.uniform(2, 8)),
            'liquidity_shock': float(np.random.uniform(0.5, 1.0)),
            'correlation_shift': float(np.random.uniform(0.5, 1.0)),
            'duration_days': int(np.random.randint(30, 600)),
            'regime': 'crisis',
            'severity': float(np.random.uniform(0.5, 1.0)),
        }

    def _load_scenarios(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM crisis_scenarios").fetchall()
        cols = [d[1] for d in self.db.execute("PRAGMA table_info(crisis_scenarios)")]
        return [dict(zip(cols, r)) for r in rows]

    def survival_filter(self, organisms: list[dict], min_survival_rate: float = 0.5) -> list[dict]:
        """Filter organisms: only survivors pass to next generation."""
        survivors = []
        for org in organisms:
            crisis_result = self.run_crisis_battery(org)
            if crisis_result['survival_rate'] >= min_survival_rate:
                org['crisis_survival'] = crisis_result
                survivors.append(org)
            else:
                print(f"  💀 Extinct: survival_rate={crisis_result['survival_rate']:.0%}")
        return survivors
