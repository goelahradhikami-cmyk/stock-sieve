"""
Rule Registry — Manages counter-evidence rules lifecycle.

Phase 5A-2 §5

Rule lifecycle:
  failure_patterns (occurrence >= 5, confidence >= 0.7)
    → candidate_rules
    → validated_rules (backtest verified)
    → retired_rules (decayed or superseded)

Also loads static rules from validation_rules.yaml.
"""

import json


class RuleRegistry:
    """Manages validation rules for the Thesis Validator.

    Rules come from:
      1. Static YAML config (industry-specific, always-on rules)
      2. Dynamic Post-Mortem pipeline (failure_patterns → candidate → validated)
    """

    # Thresholds for promoting failure_pattern → candidate_rule
    MIN_OCCURRENCE = 5
    MIN_CONFIDENCE = 0.7

    # Static rules (industry-specific, always active)
    STATIC_RULES = [
        {
            "rule_id": "static_low_pe_manufacturing_trap",
            "name": "低PE制造业价值陷阱",
            "description": "PE<10且无品牌壁垒的制造业公司容易出现价值陷阱",
            "severity": 0.8,
            "common_thesis_types": ["deep_value"],
            "common_factors": {"pe_ttm": {"max": 10}, "gross_margin": {"max": 0.25}},
            "preventive_action": "增加经营稳定性过滤（ROE连续5年>10%）",
            "source": "static",
        },
        {
            "rule_id": "static_high_pe_cyclical_trap",
            "name": "高PE周期股陷阱",
            "description": "周期股在盈利顶点时PE极低（看似便宜），实际是卖出信号",
            "severity": 0.7,
            "common_thesis_types": ["deep_value", "turnaround_opportunity"],
            "common_factors": {"pe_ttm": {"max": 8}, "roe": {"max": 0.30}},
            "preventive_action": "检查行业周期位置，确认不是在周期顶部买低PE",
            "source": "static",
        },
        {
            "rule_id": "static_smallcap_liquidity_risk",
            "name": "小市值流动性风险",
            "description": "流通市值<50亿的股票容易出现流动性问题和价格操纵",
            "severity": 0.5,
            "common_thesis_types": ["momentum_breakout", "turnaround_opportunity"],
            "common_factors": {},
            "preventive_action": "小市值股票限制最大仓位为5%",
            "source": "static",
        },
    ]

    def __init__(self, db):
        self.db = db

    def get_validated_rules(self, agent_id: str = None) -> list[dict]:
        """Return all active rules (static + validated dynamic).

        Static rules are always included.
        Dynamic rules require pattern_confidence >= 0.7 and occurrence_count >= 5.
        """
        rules = list(self.STATIC_RULES)

        # Fetch dynamic rules from failure_patterns
        conn = self.db.connect()
        rows = conn.execute("""
            SELECT * FROM failure_patterns
            WHERE occurrence_count >= ?
              AND pattern_confidence >= ?
            ORDER BY occurrence_count DESC
        """, (self.MIN_OCCURRENCE, self.MIN_CONFIDENCE)).fetchall()
        conn.close()

        for row in rows:
            row_dict = dict(row)
            rules.append({
                "rule_id": f"dynamic_{row_dict['pattern_id']}",
                "name": row_dict.get("pattern_name", ""),
                "description": row_dict.get("preventive_action", ""),
                "severity": min(0.9, row_dict.get("pattern_confidence", 0.7)),
                "common_thesis_types": self._parse_field(row_dict.get("common_thesis_types")),
                "common_factors": self._parse_field(row_dict.get("common_factors")),
                "preventive_action": row_dict.get("preventive_action", ""),
                "occurrence_count": row_dict.get("occurrence_count", 0),
                "source": "dynamic",
            })

        return rules

    def promote_candidate(self, pattern_id: str):
        """Manually promote a failure pattern to validated status."""
        conn = self.db.connect()
        conn.execute("""
            UPDATE failure_patterns
            SET pattern_confidence = MAX(pattern_confidence, 0.7),
                validated_by = 'manual_promotion',
                updated_at = CURRENT_TIMESTAMP
            WHERE pattern_id = ?
        """, (pattern_id,))
        conn.commit()
        conn.close()

    def retire_rule(self, pattern_id: str):
        """Retire a rule (reduce confidence below threshold)."""
        conn = self.db.connect()
        conn.execute("""
            UPDATE failure_patterns
            SET pattern_confidence = 0.3,
                validated_by = 'retired',
                updated_at = CURRENT_TIMESTAMP
            WHERE pattern_id = ?
        """, (pattern_id,))
        conn.commit()
        conn.close()

    def check_and_promote(self):
        """Auto-promote failure patterns that meet thresholds.

        Called periodically (e.g., after Post-Mortem batch runs).
        """
        conn = self.db.connect()
        conn.execute("""
            UPDATE failure_patterns
            SET pattern_confidence = MAX(pattern_confidence, 0.7),
                validated_by = 'auto_promotion',
                updated_at = CURRENT_TIMESTAMP
            WHERE occurrence_count >= ?
              AND pattern_confidence < 0.7
        """, (self.MIN_OCCURRENCE,))
        count = conn.total_changes
        conn.commit()
        conn.close()
        return count

    def _parse_field(self, value):
        """Parse a JSON field from DB."""
        if value is None:
            return {}
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return {}
        return value
