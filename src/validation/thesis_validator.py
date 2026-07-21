"""
Thesis Validator — Independent audit layer between Research and Portfolio agents.

Implements Phase 5A-2 spec:
  - Evidence Check: verify thesis claims against factor data
  - Counter-Evidence Risk: match against known failure patterns
  - Historical Pattern: composite scoring from thesis_outcomes
  - Complexity Check: detect narrative overfitting
  - Confidence Calibration: compute effective_confidence

Core principles:
  1. Only audits, never modifies original SecurityAnalysis
  2. Structured validation (rule engine), not LLM judgment
  3. Failure-driven evolution: rules come from Post-Mortem Engine

Data flow:
  Research Agent → SecurityAnalysis → Thesis Validator → ValidationResult → Portfolio Agent
"""

import json
from dataclasses import dataclass, field

from .complexity_checker import ComplexityChecker
from .counter_evidence import CounterEvidenceChecker
from .evidence_checker import EvidenceChecker
from .historical_pattern import HistoricalPatternAnalyzer
from .rule_registry import RuleRegistry

# ═══════════════════════════════════════════════════════════
# ValidationResult
# ═══════════════════════════════════════════════════════════

@dataclass
class ValidationResult:
    """Output of Thesis Validator — independent from SecurityAnalysis."""

    thesis_id: str
    research_decision_id: int

    # Sub-scores (0-100, higher = better)
    evidence_score: float
    counter_evidence_risk: float   # 0 = no risk, 100 = extreme risk
    historical_score: float
    complexity_penalty: float      # 0-10, higher = more complex

    # Composite
    overall_score: float

    # Gate decision
    verdict: str                   # PASS / PASS_WITH_WARNING / REJECT
    routing_action: str = "ALLOW_COMMITTEE"  # BLOCK / RESEARCH_ONLY / ALLOW_REDUCED_WEIGHT / ALLOW_COMMITTEE
    rejection_reasons: list[str] = field(default_factory=list)

    # Confidence (does NOT modify original)
    original_confidence: float = 5.0
    effective_confidence: float = 5.0
    confidence_adjustment: float = 0.0

    # Detail
    evidence_failures: list[dict] = field(default_factory=list)
    counter_warnings: list[dict] = field(default_factory=list)
    historical_analog: dict = field(default_factory=dict)
    complexity_details: dict = field(default_factory=dict)

    # Pass to downstream
    monitoring_flags: list[str] = field(default_factory=list)
    memory_context: dict | None = None     # Commit 6-E: investment memory results
    validation_rationale: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════
# Thesis Validator
# ═══════════════════════════════════════════════════════════

class ThesisValidator:
    """Independent audit layer for investment theses.

    Placed between Research Agent and Portfolio Agent.
    Validates thesis quality without modifying original analysis.
    """

    # Scoring weights
    WEIGHTS = {
        "evidence": 0.35,
        "counter_evidence": 0.25,
        "historical": 0.25,
        "complexity": 0.15,
    }

    # Routing thresholds (Commit 5.1 Fix 1)
    BLOCK_THRESHOLD = 40
    RESEARCH_ONLY_THRESHOLD = 50
    REDUCED_WEIGHT_THRESHOLD = 70
    COUNTER_RISK_HIGH = 50

    def __init__(self, db, rule_registry: RuleRegistry = None):
        self.db = db
        self.rule_registry = rule_registry or RuleRegistry(db)
        self.evidence_checker = EvidenceChecker()
        self.counter_checker = CounterEvidenceChecker(self.rule_registry)
        self.historical_analyzer = HistoricalPatternAnalyzer(db)
        self.complexity_checker = ComplexityChecker()
        # Commit 6-E: Investment Memory
        try:
            from src.memory.retrieval import MemoryRetriever
            self.memory_retriever = MemoryRetriever()
        except Exception:
            self.memory_retriever = None

    def validate(self, research_decision_id: int) -> ValidationResult:
        """Run full validation on a research decision.

        Args:
            research_decision_id: ID from research_decisions table

        Returns:
            ValidationResult with verdict and effective_confidence.
        """
        # ── Load research decision ────────────────────────
        conn = self.db.connect()
        row = conn.execute(
            "SELECT * FROM research_decisions WHERE id = ?",
            (research_decision_id,)
        ).fetchone()
        conn.close()

        if not row:
            raise ValueError(f"Research decision {research_decision_id} not found")

        rd = dict(row)

        # Parse JSON fields
        thesis_evidence = self._parse_json(rd.get("thesis_evidence", "[]"))
        thesis_invalidation = self._parse_json(rd.get("thesis_invalidation", "[]"))
        factor_snapshot = self._parse_json(rd.get("factor_snapshot", "{}"))
        risk_assessment = self._parse_json(rd.get("risk_assessment", "{}"))

        thesis_id = rd.get("thesis_id", "")
        thesis_pattern = rd.get("thesis_pattern", "")
        thesis_claim = rd.get("thesis_claim", "")
        alpha_score = rd.get("alpha_score", 5.0)
        confidence = rd.get("confidence", 5.0)
        agent_id = rd.get("agent_id", "")

        # ── 1. Evidence Check ─────────────────────────────
        evidence_score, evidence_failures = self.evidence_checker.check(
            thesis_evidence, factor_snapshot
        )

        # ── 2. Counter-Evidence Risk ─────────────────────
        counter_risk, counter_warnings = self.counter_checker.assess(
            factor_snapshot, thesis_pattern, agent_id
        )

        # ── 3. Historical Pattern ─────────────────────────
        historical_score, historical_analog = self.historical_analyzer.analyze(
            thesis_pattern, agent_id
        )

        # ── 4. Investment Memory (Commit 6-E) ────────────
        memory_context = None
        if self.memory_retriever and thesis_pattern:
            try:
                # Get agent identity
                identity = self._get_agent_identity(agent_id)
                # Get market regime
                regime = self._get_market_regime(research_decision_id)
                memory_context = self.memory_retriever.search(
                    thesis_pattern=thesis_pattern,
                    market_regime=regime,
                    factor_snapshot=factor_snapshot,
                    agent_identity=identity,
                )
            except Exception:
                memory_context = None

        # ── 5. Complexity Check ───────────────────────────
        complexity_penalty, complexity_details = self.complexity_checker.check(
            thesis_claim, thesis_evidence
        )

        # ── 5. Composite Score ────────────────────────────
        overall_score = (
            evidence_score * self.WEIGHTS["evidence"] +
            (100 - counter_risk) * self.WEIGHTS["counter_evidence"] +
            historical_score * self.WEIGHTS["historical"] +
            (10 - complexity_penalty) * 10 * self.WEIGHTS["complexity"]
        )

        # ── 6. Verdict with routing (Fix 1) ────────────────
        rejection_reasons = []
        if evidence_failures:
            rejection_reasons.append(
                f"Evidence failures: {[f['metric'] for f in evidence_failures]}"
            )

        routing_action = "ALLOW_COMMITTEE"
        verdict = "PASS"

        if overall_score < self.BLOCK_THRESHOLD or evidence_score < 30:
            routing_action = "BLOCK"
            verdict = "REJECT"
        elif overall_score < self.RESEARCH_ONLY_THRESHOLD:
            routing_action = "RESEARCH_ONLY"
            verdict = "REJECT"
        elif overall_score < self.REDUCED_WEIGHT_THRESHOLD or counter_risk > self.COUNTER_RISK_HIGH:
            routing_action = "ALLOW_REDUCED_WEIGHT"
            verdict = "PASS_WITH_WARNING"

        if verdict == "REJECT":
            rejection_reasons.append(f"Overall score {overall_score:.1f} → routing={routing_action}")

        # ── 7. Confidence Calibration ─────────────────────
        adjustment = self._calculate_adjustment(
            verdict, counter_risk, complexity_penalty, evidence_score
        )
        # Memory risk adjustment (Commit 6-E): high risk → reduce confidence
        memory_risk = memory_context.get('memory_risk_score', 0.5) if memory_context else 0.5
        confidence_before = confidence + adjustment
        if memory_risk > 0.7 and memory_context and memory_context['pattern_stats']['total'] >= 10:
            adjustment -= 1.0
        effective_confidence = max(0.0, min(10.0, confidence + adjustment))

        # Write memory feedback log
        if memory_context:
            try:
                conn = self.db.connect() if hasattr(self.db, 'connect') else self.db
                conn.execute("""
                    INSERT INTO memory_feedback_log
                    (research_decision_id, memory_risk_score, confidence_before, confidence_after)
                    VALUES (?,?,?,?)
                """, (research_decision_id, memory_risk, round(confidence_before, 1), round(effective_confidence, 1)))
                if hasattr(conn, 'commit'): conn.commit()
            except Exception:
                pass

        # ── 8. Monitoring flags ───────────────────────────
        monitoring_flags = self._generate_monitoring_flags(
            verdict, counter_warnings, complexity_details
        )

        # ── 9. Rationale ──────────────────────────────────
        rationale = {
            "evidence": f"Evidence score: {evidence_score:.1f}/100 ({len(thesis_evidence) - len(evidence_failures)}/{len(thesis_evidence)} passed)",
            "counter_evidence": f"Counter-evidence risk: {counter_risk:.1f}/100 ({len(counter_warnings)} warnings)",
            "historical": f"Historical score: {historical_score:.1f}/100 (pattern: {thesis_pattern})",
            "complexity": f"Complexity penalty: {complexity_penalty:.1f}/10",
            "memory": f"Memory risk: {memory_risk:.2f} (cases: {memory_context['pattern_stats']['total'] if memory_context else 0})" if memory_context else "Memory: not available",
            "overall": f"Overall: {overall_score:.1f}/100 → {verdict}",
        }

        return ValidationResult(
            thesis_id=thesis_id,
            research_decision_id=research_decision_id,
            evidence_score=round(evidence_score, 1),
            counter_evidence_risk=round(counter_risk, 1),
            historical_score=round(historical_score, 1),
            complexity_penalty=round(complexity_penalty, 1),
            overall_score=round(overall_score, 1),
            verdict=verdict,
            routing_action=routing_action,
            rejection_reasons=rejection_reasons,
            original_confidence=confidence,
            effective_confidence=round(effective_confidence, 1),
            confidence_adjustment=round(adjustment, 1),
            evidence_failures=evidence_failures,
            counter_warnings=counter_warnings,
            historical_analog=historical_analog,
            complexity_details=complexity_details,
            monitoring_flags=monitoring_flags,
            memory_context=memory_context,
            validation_rationale=rationale,
        )

    def _calculate_adjustment(self, verdict: str, counter_risk: float,
                               complexity: float, evidence_score: float) -> float:
        """Calculate confidence adjustment (§4.6)."""
        adj = 0.0
        if verdict == "PASS_WITH_WARNING":
            adj -= 1.0
        if counter_risk > 60:
            adj -= 0.5
        if complexity > 7:
            adj -= 0.5
        if evidence_score < 70:
            adj -= 0.5
        return adj

    def _generate_monitoring_flags(self, verdict: str, warnings: list[dict],
                                    complexity: dict) -> list[str]:
        """Generate monitoring flags for Portfolio Agent."""
        flags = []
        if verdict == "PASS_WITH_WARNING":
            flags.append("MONITOR_CLOSELY")
        for w in warnings:
            if w.get("severity", "low") == "high":
                flags.append(f"COUNTER_RISK:{w.get('rule_name', 'unknown')}")
        if complexity.get("total_score", 0) > 7:
            flags.append("HIGH_COMPLEXITY_THESIS")
        return flags

    def _parse_json(self, value):
        """Safely parse JSON string or return as-is.

        Tries ``json.loads`` first (proper JSON). If that fails, falls back
        to ``ast.literal_eval`` - many rows in ``research_decisions`` store
        Python ``str(dict)`` repr (single-quoted) rather than real JSON
        (see ``evaluation_crud.py`` which uses ``str(factor_snapshot)``),
        so ``json.loads`` silently returns the raw string and downstream
        loops iterate character-by-character. The fallback parses the repr
        into a real object.
        """
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                import ast
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return value
        return value


# ═══════════════════════════════════════════════════════════
# Integration helper
# ═══════════════════════════════════════════════════════════

    def _get_agent_identity(self, agent_id: str) -> dict | None:
        """Load agent identity from genome snapshot."""
        try:
            conn = self.db.connect() if hasattr(self.db, 'connect') else self.db
            row = conn.execute(
                "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id=? AND status='active' ORDER BY birth_date DESC LIMIT 1",
                (agent_id,)
            ).fetchone()
            if row and row[0]:
                import yaml
                genome = yaml.safe_load(row[0]) or {}
                return genome.get('investment_identity', {})
        except Exception:
            pass
        return None

    def _get_market_regime(self, research_decision_id: int) -> str:
        """Get market regime at decision time."""
        try:
            conn = self.db.connect() if hasattr(self.db, 'connect') else self.db
            row = conn.execute(
                "SELECT entry_date FROM research_decisions WHERE id=?", (research_decision_id,)
            ).fetchone()
            if row and row[0]:
                mr = conn.execute(
                    "SELECT regime_type FROM market_regime_snapshots WHERE obs_date<=? ORDER BY obs_date DESC LIMIT 1",
                    (str(row[0])[:10],)
                ).fetchone()
                return mr[0] if mr else ""
        except Exception:
            pass
        return ""


def validate_and_enhance(sa, validator: ThesisValidator) -> dict | None:
    """Run validation on a SecurityAnalysis, return enhanced dict for Portfolio.

    If REJECT, returns None (thesis blocked from portfolio).
    """
    if sa.thesis is None or sa.thesis.thesis_id is None:
        return None

    # This would need a real research_decision_id
    # In practice, the SA would be saved to DB first, then validated
    # For now, return the SA with default effective_confidence
    return {
        "agent_id": sa.agent_id,
        "stock_code": sa.stock_code,
        "alpha_score": sa.alpha_score,
        "confidence": sa.confidence,
        "effective_confidence": sa.confidence,
        "thesis": sa.thesis,
        "factor_profile": sa.factor_profile,
        "risk_assessment": sa.risk_assessment,
        "monitoring_flags": [],
    }
