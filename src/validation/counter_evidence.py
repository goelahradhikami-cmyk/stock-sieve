"""
Counter-Evidence Checker — Match thesis against known failure patterns.

Phase 5A-2 §4.2

Uses RuleRegistry to fetch validated rules (from failure_patterns with
pattern_confidence >= 0.7 and occurrence_count >= 5).
Each matched rule increases risk score by rule.severity × 25.
"""


from .rule_registry import RuleRegistry


class CounterEvidenceChecker:
    """Checks thesis factor snapshot against known failure patterns."""

    def __init__(self, rule_registry: RuleRegistry):
        self.registry = rule_registry

    def assess(self, factor_snapshot: dict, thesis_pattern: str,
               agent_id: str = None) -> tuple[float, list[dict]]:
        """Assess counter-evidence risk.

        Returns:
            (risk_score 0-100, list of warning dicts)
        """
        rules = self.registry.get_validated_rules(agent_id)

        if not rules:
            return 0.0, []

        risk_score = 0.0
        warnings = []

        for rule in rules:
            if self._rule_matches(rule, factor_snapshot, thesis_pattern):
                severity = rule.get("severity", 0.5)
                risk_score += severity * 25

                warnings.append({
                    "rule_name": rule.get("name", "unknown"),
                    "rule_id": rule.get("rule_id", ""),
                    "severity": "high" if severity > 0.6 else "medium" if severity > 0.3 else "low",
                    "description": rule.get("description", ""),
                    "preventive_action": rule.get("preventive_action", ""),
                })

        return min(100.0, risk_score), warnings

    def _rule_matches(self, rule: dict, factor_snapshot, thesis_pattern: str) -> bool:
        """Check if a rule matches the current thesis context."""
        # Handle string (JSON) factor_snapshot
        if isinstance(factor_snapshot, str):
            try:
                import json
                factor_snapshot = json.loads(factor_snapshot)
            except (json.JSONDecodeError, TypeError):
                factor_snapshot = {}
        if not isinstance(factor_snapshot, dict):
            return False
        # Check thesis pattern match
        common_thesis = rule.get("common_thesis_types", [])
        if isinstance(common_thesis, str):
            import json
            try:
                common_thesis = json.loads(common_thesis)
            except json.JSONDecodeError:
                common_thesis = []

        if common_thesis and thesis_pattern not in common_thesis:
            return False

        # Check factor conditions
        common_factors = rule.get("common_factors", {})
        if isinstance(common_factors, str):
            import json
            try:
                common_factors = json.loads(common_factors)
            except json.JSONDecodeError:
                common_factors = {}

        if not common_factors:
            return True  # No factor conditions → matches by thesis pattern alone

        for factor_name, condition in common_factors.items():
            actual = factor_snapshot.get(factor_name)
            if actual is None:
                continue

            if isinstance(condition, dict):
                min_val = condition.get("min")
                max_val = condition.get("max")
                if min_val is not None and actual < min_val:
                    return True
                if max_val is not None and actual > max_val:
                    return True

        return False
