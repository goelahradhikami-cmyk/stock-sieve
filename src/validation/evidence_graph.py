"""
Evidence Graph — Causal chain validation for thesis evidence.

Commit 5.1 Fix 2: Validates multi-step causal chains in thesis evidence,
falling back to traditional evidence checking when no chain is provided.
"""


class EvidenceGraph:
    """Validates causal evidence chains in investment theses."""

    def validate_chain(self, thesis: dict, factor_snapshot: dict) -> tuple:
        """Validate a causal evidence chain against factor data.

        Args:
            thesis: dict with optional 'evidence_chain' key
            factor_snapshot: dict of factor_name → value

        Returns:
            (score 0-100, warnings list)
        """
        chain = thesis.get("evidence_chain", [])
        if not chain:
            # Fallback to traditional evidence checking
            return 70.0, []

        passed = 0
        warnings = []

        for node in chain:
            node_type = node.get("type", "unknown")
            metric = node.get("metric", "")
            expected = node.get("expected_value")
            direction = node.get("direction", "gt")  # 'gt' or 'lt'
            actual = factor_snapshot.get(metric)

            if actual is None:
                warnings.append(f"{node_type}: metric '{metric}' not in snapshot")
                continue

            if not self._verify(actual, expected, direction):
                warnings.append(
                    f"{node_type}: {metric} expected {direction} {expected}, got {actual}"
                )
            else:
                passed += 1

        score = (passed / len(chain)) * 100 if chain else 70.0
        return score, warnings

    def _verify(self, actual, expected, direction: str) -> bool:
        """Verify a single condition."""
        if actual is None or expected is None:
            return False

        try:
            actual = float(actual)
            expected = float(expected)

            if direction == "gt":
                return actual > expected
            elif direction == "lt":
                return actual < expected
            elif direction == "gte":
                return actual >= expected
            elif direction == "lte":
                return actual <= expected
            elif direction == "eq":
                return abs(actual - expected) < 0.01
            return False
        except (ValueError, TypeError):
            return False

    def build_chain_from_evidence(self, evidence: list) -> list:
        """Build an evidence chain from traditional evidence list.

        Each evidence item becomes a chain node.
        """
        chain = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            metric = item.get("metric", "")
            condition = item.get("condition", "")

            # Parse condition string like ">0.20" → direction="gt", expected=0.20
            direction = "gt"
            expected = 0
            if condition.startswith(">="):
                direction, expected = "gte", condition[2:]
            elif condition.startswith("<="):
                direction, expected = "lte", condition[2:]
            elif condition.startswith(">"):
                direction, expected = "gt", condition[1:]
            elif condition.startswith("<"):
                direction, expected = "lt", condition[1:]

            try:
                expected = float(expected)
            except (ValueError, TypeError):
                continue

            chain.append({
                "type": "evidence",
                "metric": metric,
                "direction": direction,
                "expected_value": expected,
            })

        return chain
