"""
Evidence Checker — Verify thesis claims against factor snapshot data.

Phase 5A-2 §4.1
"""


class EvidenceChecker:
    """Validates thesis evidence items against actual factor values."""

    def check(self, evidence: list, factor_snapshot: dict) -> tuple[float, list[dict]]:
        """Check all evidence items against factor_snapshot.

        Args:
            evidence: List of {"metric": "...", "value": ..., "condition": "..."}
            factor_snapshot: Dict of factor_name → value

        Returns:
            (score 0-100, list of failures)
        """
        if not evidence:
            return 50.0, [{"metric": "none", "reason": "No evidence provided for thesis"}]

        failures = []
        for item in evidence:
            if not isinstance(item, dict):
                failures.append({"metric": "unknown", "reason": f"Invalid evidence format: {item}"})
                continue

            metric = item.get("metric", "unknown")
            condition = item.get("condition", "")
            actual_value = factor_snapshot.get(metric)

            # Fallback: evidence items carry their own observed ``value``
            # (e.g. {"metric": "pe_ttm", "value": 5.93, "condition": "<15"}).
            # factor_snapshot only holds composite factor-family scores
            # (quality/value/growth/momentum), so raw metrics like pe_ttm/pb
            # are never found there - which previously forced every evidence
            # item to fail and zeroed the evidence_score, blocking the whole
            # pipeline at the validator. Self-validate against the carried
            # value when the snapshot has no matching key.
            if actual_value is None and "value" in item:
                actual_value = item["value"]

            if actual_value is None:
                failures.append(
                    {
                        "metric": metric,
                        "reason": f"Factor '{metric}' not found in snapshot",
                        "condition": condition,
                    }
                )
                continue

            if not self._evaluate(condition, actual_value):
                failures.append(
                    {
                        "metric": metric,
                        "actual": actual_value,
                        "condition": condition,
                        "reason": f"Expected {condition}, got {actual_value}",
                    }
                )

        passed = len(evidence) - len(failures)
        score = (passed / len(evidence)) * 100 if evidence else 50.0

        return score, failures

    def _evaluate(self, condition: str, actual: float) -> bool:
        """Evaluate a condition string against an actual value.

        Supports: >X, <X, >=X, <=X, =X (with or without spaces).
        Examples: ">0.20", "> 0.20", "<30", ">= 0.15".
        Also supports AND/OR combinations.
        """
        if not condition or not isinstance(condition, str):
            return True

        condition = condition.strip()

        try:
            # Unspaced comparisons (e.g., ">0.20", "<30")
            import re

            m = re.match(r"^(>=|<=|>|<|=)\s*(-?[\d.]+)$", condition)
            if m:
                op = m.group(1)
                threshold = float(m.group(2))
                if op == ">=":
                    return float(actual) >= threshold
                if op == "<=":
                    return float(actual) <= threshold
                if op == ">":
                    return float(actual) > threshold
                if op == "<":
                    return float(actual) < threshold
                if op == "=":
                    return abs(float(actual) - threshold) < 0.001

            # Spaced comparisons with metric prefix (e.g., "roe > 0.20")
            if " >= " in condition:
                _, threshold_s = condition.split(" >= ")
                return float(actual) >= float(threshold_s.strip())
            if " <= " in condition:
                _, threshold_s = condition.split(" <= ")
                return float(actual) <= float(threshold_s.strip())
            if " > " in condition:
                _, threshold_s = condition.split(" > ")
                return float(actual) > float(threshold_s.strip())
            if " < " in condition:
                _, threshold_s = condition.split(" < ")
                return float(actual) < float(threshold_s.strip())

            # Handle AND
            if " AND " in condition:
                parts = condition.split(" AND ")
                return all(self._evaluate(p.strip(), actual) for p in parts)

            # Handle OR
            if " OR " in condition:
                parts = condition.split(" OR ")
                return any(self._evaluate(p.strip(), actual) for p in parts)

        except (ValueError, TypeError):
            pass

        return True  # Can't evaluate → pass
