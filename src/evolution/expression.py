"""
Expression Engine — Translate factor DNA into trading rules.

Commit 6-H.1 Fix 2: Same factor genome produces different expressions
depending on the agent's personality (patience, valuation, growth).

Examples:
  - ROE factor × value_purist (patience=95) → 5-year avg ROE, 180-day hold
  - ROE factor × momentum_chaser (patience=20) → quarterly change, 60-day hold
"""

import json

from src.data.db import managed_connect


class ExpressionEngine:
    """Translate factor genome into personality-specific trading rules."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)

    def translate(self, factor_genome: dict, agent_identity: dict) -> dict:
        """Generate expression rules based on agent personality dimensions."""
        dims = agent_identity.get('dimensions', {})
        valuation = dims.get('valuation', 50)
        growth = dims.get('growth', 50)
        patience = dims.get('patience', 50)
        momentum = dims.get('momentum', 50)

        expressions = {}
        for factor in factor_genome.get('factors', []):
            name = str(factor.get('name', factor.get('factor', ''))).lower()
            weight = factor.get('weight', 0.1)

            expr, holding, rebalance = self._build_expression(
                name, weight, patience, valuation, growth, momentum
            )

            expressions[name] = expr

            try:
                self.db.execute("""
                    INSERT OR REPLACE INTO factor_expression_genome
                    (factor_genome_id, factor_name, expression_json, holding_period,
                     rebalance_frequency, entry_rule, exit_rule)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    factor_genome.get('genome_id', 'unknown'), name,
                    json.dumps(expr), holding, rebalance,
                    f"rank_{expr['metric']}_top_30pct",
                    f"rank_{expr['metric']}_bottom_20pct OR invalidation_triggered",
                ))
            except Exception:
                pass

        self.db.commit()
        return expressions

    def _build_expression(self, name: str, weight: float,
                           patience: int, valuation: int,
                           growth: int, momentum: int) -> tuple:
        """Build expression rule from personality dimensions."""
        # Quality factors (ROE, ROIC, gross_margin)
        if any(k in name for k in ('roe', 'roic', 'quality', 'margin', 'gross')):
            if patience > 70:
                return (
                    {'metric': f'{name}_5y_avg', 'window': 60, 'threshold': 0.15},
                    180, 60
                )
            else:
                return (
                    {'metric': f'{name}_quarterly_change', 'window': 4, 'threshold': 0.02},
                    60, 30
                )

        # Momentum factors
        if any(k in name for k in ('momentum', 'trend', 'rsi')):
            if growth > 60:
                return (
                    {'metric': f'{name}_6m', 'window': 120, 'threshold': 0.10},
                    90, 30
                )
            else:
                return (
                    {'metric': f'{name}_3m', 'window': 60, 'threshold': 0.05},
                    30, 15
                )

        # Value factors (PE, PB, EV/EBITDA, dividend)
        if any(k in name for k in ('pe', 'pb', 'ev_ebitda', 'dividend', 'fcf', 'value')):
            if valuation > 70:
                return (
                    {'metric': f'{name}_vs_5y_median', 'window': 5, 'threshold': -0.20},
                    250, 90
                )
            else:
                return (
                    {'metric': f'{name}_vs_sector', 'window': 1, 'threshold': -0.10},
                    90, 30
                )

        # Growth factors
        if any(k in name for k in ('growth', 'revenue_growth', 'earnings_growth')):
            if growth > 70:
                return (
                    {'metric': f'{name}_3y_cagr', 'window': 12, 'threshold': 0.20},
                    120, 60
                )
            else:
                return (
                    {'metric': f'{name}_1y', 'window': 4, 'threshold': 0.10},
                    60, 30
                )

        # Default
        return (
            {'metric': name, 'window': 20, 'threshold': 0},
            60, 30
        )
