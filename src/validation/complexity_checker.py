"""
Complexity Checker — Detect narrative overfitting in investment theses.

Phase 5A-2 §4.4

Checks:
  - claims: number of independent claims
  - causal_depth: maximum depth of causal chain ("A→B→C" = depth 3)
  - dependencies: number of logical dependencies
  - assumptions: number of implicit assumptions

Penalty 0-10. When >7, additional penalties applied in overall score
and confidence adjustment.
"""



class ComplexityChecker:
    """Evaluates thesis structural complexity to prevent narrative overfitting."""

    MAX_CLAIMS = 5
    MAX_DEPTH = 3
    MAX_DEPENDENCIES = 5
    MAX_ASSUMPTIONS = 4

    def check(self, thesis_claim: str, evidence: list) -> tuple[float, dict]:
        """Analyze thesis complexity.

        Returns:
            (penalty 0-10, detail dict)
        """
        details = {}

        # ── 1. Count claims ──────────────────────────────
        claims_score = self._count_claims(thesis_claim)
        details["claims"] = {
            "count": claims_score,
            "max_recommended": self.MAX_CLAIMS,
            "penalty": min(3.0, max(0.0, (claims_score - self.MAX_CLAIMS) * 1.0)),
        }

        # ── 2. Causal depth ──────────────────────────────
        depth_score = self._causal_depth(thesis_claim)
        details["causal_depth"] = {
            "depth": depth_score,
            "max_recommended": self.MAX_DEPTH,
            "penalty": min(3.0, max(0.0, (depth_score - self.MAX_DEPTH) * 1.5)),
        }

        # ── 3. Dependencies ──────────────────────────────
        dep_score = self._count_dependencies(thesis_claim, evidence)
        details["dependencies"] = {
            "count": dep_score,
            "max_recommended": self.MAX_DEPENDENCIES,
            "penalty": min(2.0, max(0.0, (dep_score - self.MAX_DEPENDENCIES) * 0.5)),
        }

        # ── 4. Assumptions ───────────────────────────────
        assumption_score = self._count_assumptions(thesis_claim)
        details["assumptions"] = {
            "count": assumption_score,
            "max_recommended": self.MAX_ASSUMPTIONS,
            "penalty": min(2.0, max(0.0, (assumption_score - self.MAX_ASSUMPTIONS) * 0.5)),
        }

        # ── Total penalty ────────────────────────────────
        total = sum(
            details[k]["penalty"]
            for k in ["claims", "causal_depth", "dependencies", "assumptions"]
        )
        total = min(10.0, total)
        details["total_score"] = round(total, 1)

        return total, details

    def _count_claims(self, text: str) -> int:
        """Count independent claims in thesis text.

        Heuristic: count separators like '且', '同时', '并', '以及', semicolons.
        """
        if not text:
            return 1

        separators = ["且", "同时", "并", "以及", "；", ";", "。此外", "另外"]
        count = 1  # at least one claim
        for sep in separators:
            count += text.count(sep)
        return min(count, 10)  # cap

    def _causal_depth(self, text: str) -> int:
        """Estimate causal chain depth.

        Heuristic: look for causal connectors like '推动', '导致', '促使', '→'.
        """
        if not text:
            return 1

        causal_words = ["推动", "导致", "促使", "引发", "带动", "→", "->",
                        "所以", "因此", "从而", "进而"]
        depth = 1
        for word in causal_words:
            depth += text.count(word)
        return min(depth, 8)

    def _count_dependencies(self, text: str, evidence: list) -> int:
        """Count logical dependencies in thesis structure.

        Evidence items that depend on each other increase complexity.
        """
        if not text:
            return len(evidence)

        # Base: each evidence item is a dependency
        base = len(evidence) if evidence else 1

        # Additional: conditional language adds dependencies
        conditionals = ["如果", "若", "当", "取决于", "依赖于", "需要", "前提是"]
        extra = sum(text.count(w) for w in conditionals)

        return base + extra

    def _count_assumptions(self, text: str) -> int:
        """Count implicit assumptions in thesis.

        Heuristic: words indicating assumptions.
        """
        if not text:
            return 2

        assumption_words = ["假设", "预计", "预期", "有望", "可能", "或将",
                            "大概率", "应该", "将会", "预计将"]
        count = 1  # base assumption that the thesis is correct
        for word in assumption_words:
            count += text.count(word)
        return min(count, 10)
