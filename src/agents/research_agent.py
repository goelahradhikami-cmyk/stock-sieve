"""
Research Agent — Produces investment hypotheses from market data.

Implements agent_contract_v1.1 §2 Research Agent protocol:
  - Input: MarketSnapshot, StockSnapshot, FactorSnapshot, MemoryContext
  - Output: SecurityAnalysis (thesis, factor_profile, risk_assessment, decision_fingerprint)
  - Deterministic: same inputs → same outputs
  - LLM NOT involved in scoring (contract §2.3, §13.2)

Also implements personality_genome_schema_v3.2:
  - Load genome YAML → identity, doctrine, factor_weights, decision_graph
  - Thesis engine: pattern matching + evidence checking
  - Factor scoring: weighted composite per family
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from ..data import MarketSnapshot, StockSnapshot
from ..evolution.engine import AgentGenome
from ..factors.engine import CompositeResult, FactorEngine


@dataclass
class ThesisObject:
    """Structured, verifiable investment thesis (agent_contract §8)."""
    thesis_id: str
    family: str                    # growth / value / cycle / turnaround / special_situation / macro
    pattern: str                   # quality_compound / technology_substitution / deep_value ...
    claim: str                     # Core assertion
    evidence: list[dict] = field(default_factory=list)
    catalyst: str = ""
    invalidation: list[dict] = field(default_factory=list)
    horizon: str = "12_months"
    confidence_contribution: float = 0.0


@dataclass
class SecurityAnalysis:
    """Complete output of a Research Agent (agent_contract §3)."""
    agent_id: str
    stock_code: str
    timestamp: str
    alpha_score: float             # 0-10
    confidence: float              # 0-10
    thesis: ThesisObject | None = None
    factor_profile: dict = field(default_factory=dict)
    risk_assessment: dict = field(default_factory=dict)
    decision_fingerprint: dict = field(default_factory=dict)


class ResearchAgent:
    """AI fund manager — Research role.

    Loads a genome, analyzes stocks, produces SecurityAnalysis.
    """

    THESIS_PATTERNS = {
        "quality_compound": {
            "family": "value",
            "required_factors": {"roe": (0.15, None), "gross_margin": (0.30, None)},
            "claim_template": "{name} 的品牌护城河与定价权将在长期持续创造超额回报",
            "horizon": "36_months",
        },
        "deep_value": {
            "family": "value",
            "required_factors": {"pe_ttm": (None, 15), "pb": (None, 1.5)},
            "claim_template": "{name} 当前估值显著低于其内在价值，市场悲观情绪提供了安全边际",
            "horizon": "24_months",
        },
        "growth_at_reasonable_price": {
            "family": "growth",
            "required_factors": {"revenue_growth_1y": (0.15, None), "pe_ttm": (None, 30)},
            "claim_template": "{name} 的高速增长尚未被市场充分定价",
            "horizon": "18_months",
        },
        "dividend_compound": {
            "family": "value",
            "required_factors": {"dividend_yield": (0.03, None), "roe": (0.10, None)},
            "claim_template": "{name} 的稳定分红提供了可预期的现金回报",
            "horizon": "36_months",
        },
        "momentum_breakout": {
            "family": "cycle",
            "required_factors": {"momentum_3m": (0.15, None), "volume_ratio": (1.2, None)},
            "claim_template": "{name} 的强势突破信号预示趋势延续",
            "horizon": "3_months",
        },
        "turnaround_opportunity": {
            "family": "turnaround",
            "required_factors": {"momentum_1m": (None, -0.10), "roe": (0.0, None)},
            "claim_template": "{name} 正处于困境反转的关键节点",
            "horizon": "12_months",
        },
    }

    def __init__(self, genome_yaml: str, name: str = ""):
        self.genome = self._parse_genome(genome_yaml)
        self.name = name or self.genome.agent_id
        self.factor_engine = FactorEngine()

        # Agent memory (simplified — in production, loaded from evaluation_db)
        self.memory: dict = {
            "successful_patterns": [],
            "failure_patterns": [],
            "broken_thesis": [],
            "market_lessons": [],
        }

    def _parse_genome(self, yaml_str: str) -> AgentGenome:
        """Parse genome YAML into AgentGenome."""
        data = yaml.safe_load(yaml_str) or {}
        identity = data.get("identity", {})
        invest_id = data.get("investment_identity", {}).get("dimensions", {})
        doctrine = data.get("doctrine", {})

        # Extract factor weights
        factor_weights = {}
        factor_model = data.get("factor_model", {})
        for family, config in factor_model.items():
            if isinstance(config, dict):
                sub_factors = config.get("sub_factors", [])
                if isinstance(sub_factors, list):
                    for sf in sub_factors:
                        if isinstance(sf, dict):
                            factor_weights[sf.get("name", "")] = sf.get("weight", 0.0)
                        elif isinstance(sf, (int, float)):
                            factor_weights[str(sf)] = sf
                factor_weights[family + "_weight"] = config.get("weight", 0.0)

        # Extract thesis scoring
        thesis_scoring = {}
        thesis_engine = data.get("thesis_engine", {})
        if isinstance(thesis_engine, dict):
            thesis_scoring = thesis_engine.get("pattern_weights", {})

        # Extract decision graph
        decision_graph = data.get("decision_graph", {})

        return AgentGenome(
            agent_id=identity.get("agent_id", "unknown"),
            strategy_genus=identity.get("strategy_genus", "value"),
            strategy_species=identity.get("strategy_species", "unknown"),
            generation=identity.get("generation", 1),
            parent_agent_id=identity.get("parent_agent_id"),
            yaml_content=yaml_str,
            raw=data,
            identity_vector=invest_id,
            doctrine=doctrine,
            factor_weights=factor_weights,
            thesis_scoring=thesis_scoring,
            decision_graph=decision_graph,
        )

    def analyze(self, market: MarketSnapshot, stock: StockSnapshot,
                 factors: CompositeResult) -> SecurityAnalysis:
        """Core analyze() method (agent_contract §2.2).

        Deterministic: same input → same output.
        LLM NOT involved in scoring.
        """
        now = datetime.now().isoformat()

        # ── 1. Generate thesis ─────────────────────────────
        thesis = self._generate_thesis(stock, factors)

        # ── 2. Compute alpha_score (0-10) ──────────────────
        alpha_score = self._compute_alpha(factors)

        # ── 3. Compute confidence (0-10) ───────────────────
        confidence = self._compute_confidence(thesis, factors)

        # ── 4. Risk assessment ─────────────────────────────
        risk = self._assess_risk(stock, factors, market)

        # ── 5. Decision fingerprint ────────────────────────
        fingerprint = self._compute_fingerprint(
            stock.code, now, alpha_score, thesis
        )

        return SecurityAnalysis(
            agent_id=self.genome.agent_id,
            stock_code=stock.code,
            timestamp=now,
            alpha_score=round(alpha_score, 1),
            confidence=round(confidence, 1),
            thesis=thesis,
            factor_profile={
                "quality_score": factors.quality_score,
                "value_score": factors.value_score,
                "growth_score": factors.growth_score,
                "momentum_score": factors.momentum_score,
            },
            risk_assessment=risk,
            decision_fingerprint=fingerprint,
        )

    def _generate_thesis(self, stock: StockSnapshot,
                          factors: CompositeResult) -> ThesisObject | None:
        """Match stock+factors against thesis patterns.

        Uses genome's thesis_scoring weights to rank patterns.
        """
        best_thesis = None
        best_score = -1

        for pattern_name, pattern_def in self.THESIS_PATTERNS.items():
            # Check all required factors
            evidence = []
            all_met = True

            for factor_name, (min_val, max_val) in pattern_def["required_factors"].items():
                # Find factor value from factors
                factor = next(
                    (f for f in factors.factors if f.name == factor_name),
                    None
                )
                if factor is None or factor.raw_value is None:
                    all_met = False
                    break

                val = factor.raw_value
                if min_val is not None and val < min_val:
                    all_met = False
                    break
                if max_val is not None and val > max_val:
                    all_met = False
                    break

                evidence.append({
                    "metric": factor_name,
                    "value": round(val, 4),
                    "condition": f"{'>' if min_val else '<'}{min_val or max_val}",
                })

            if not all_met:
                continue

            # Score this pattern (weighted by genome's thesis preferences)
            genome_weight = self.genome.thesis_scoring.get(pattern_name, 0.5)
            pattern_score = len(evidence) * genome_weight

            if pattern_score > best_score:
                best_score = pattern_score

                # Build thesis claim
                name = stock.name or stock.code
                claim = pattern_def["claim_template"].format(name=name)

                best_thesis = ThesisObject(
                    thesis_id=f"{pattern_name}_{stock.code}_{datetime.now().strftime('%Y%m%d')}",
                    family=pattern_def["family"],
                    pattern=pattern_name,
                    claim=claim,
                    evidence=evidence,
                    catalyst="",  # TODO: derive from market/stock context
                    invalidation=self._derive_invalidation(pattern_name, factors),
                    horizon=pattern_def["horizon"],
                    confidence_contribution=round(best_score / 10, 2),
                )

        return best_thesis

    def _derive_invalidation(self, pattern: str,
                              factors: CompositeResult) -> list[dict]:
        """Derive invalidation conditions from thesis pattern."""
        invalidations = {
            "quality_compound": [
                {"condition": "roe_ttm < 0.15", "grace_period": "2_quarters"},
                {"condition": "gross_margin < 0.25", "grace_period": "2_quarters"},
            ],
            "deep_value": [
                {"condition": "pb > 2.0", "grace_period": "1_quarter"},
                {"condition": "momentum_6m < -0.30", "grace_period": "1_quarter"},
            ],
            "growth_at_reasonable_price": [
                {"condition": "revenue_growth_1y < 0.10", "grace_period": "2_quarters"},
                {"condition": "pe_ttm > 40", "grace_period": "1_quarter"},
            ],
            "dividend_compound": [
                {"condition": "dividend_yield < 0.02", "grace_period": "2_quarters"},
            ],
            "momentum_breakout": [
                {"condition": "momentum_1m < 0.0", "grace_period": "1_week"},
            ],
            "turnaround_opportunity": [
                {"condition": "roe < 0.0", "grace_period": "2_quarters"},
            ],
        }
        return invalidations.get(pattern, [
            {"condition": "alpha_score < 3.0", "grace_period": "1_quarter"},
        ])

    def _compute_alpha(self, factors: CompositeResult) -> float:
        """Compute alpha_score (0-10) from factor composite scores.

        Uses genome's factor_weights. Deterministic.
        """
        w = self.genome.factor_weights

        quality_w = w.get("quality_weight", 0.30)
        value_w = w.get("value_weight", 0.30)
        growth_w = w.get("growth_weight", 0.15)
        momentum_w = w.get("momentum_weight", 0.10)
        risk_w = w.get("risk_weight", 0.10)
        sentiment_w = w.get("sentiment_weight", 0.05)

        # Weighted composite, scaled to 0-10
        raw = (
            factors.quality_score * quality_w +
            factors.value_score * value_w +
            factors.growth_score * growth_w +
            factors.momentum_score * momentum_w +
            factors.risk_score * risk_w +
            factors.sentiment_score * sentiment_w
        ) / 100.0 * 10.0

        return min(10.0, max(0.0, raw))

    def _compute_confidence(self, thesis: ThesisObject | None,
                             factors: CompositeResult) -> float:
        """Compute confidence (0-10).

        Based on thesis strength + factor consistency.
        """
        if thesis is None:
            return 2.0

        # Base: 5.0 (neutral)
        confidence = 5.0

        # Thesis evidence completeness
        confidence += min(3.0, len(thesis.evidence) * 0.8)

        # Factor consistency: all families above 30 = higher confidence
        family_scores = [
            factors.quality_score, factors.value_score,
            factors.growth_score, factors.momentum_score,
        ]
        consistent_count = sum(1 for s in family_scores if s > 30)
        confidence += consistent_count * 0.5

        return min(10.0, max(0.0, confidence))

    def _assess_risk(self, stock: StockSnapshot, factors: CompositeResult,
                      market: MarketSnapshot) -> dict:
        """Assess idiosyncratic + market risk."""
        risks = []

        # Idiosyncratic risk from factors
        if factors.risk_score < 30:
            risks.append("高风险：因子风险评分偏低")

        # Liquidity risk
        if stock.float_mcap and stock.float_mcap < 50:  # < 50亿
            risks.append("流动性风险：流通市值偏小")
        if stock.turnover_pct and stock.turnover_pct < 0.5:
            risks.append("流动性风险：换手率极低")

        # Market regime risk
        if market.regime_type == "crisis":
            risks.append("市场风险：当前处于危机状态")

        # Drawdown expectation
        expected_dd = 0.15
        dd_factor = self._get_factor(factors, "max_drawdown_1y")
        if dd_factor is not None:
            expected_dd = max(0.10, abs(dd_factor))

        return {
            "idiosyncratic_risk": "high" if factors.risk_score < 30 else "medium" if factors.risk_score < 60 else "low",
            "liquidity_risk": "low" if (stock.float_mcap and stock.float_mcap > 100) else "medium",
            "key_risks": risks if risks else ["无明显特殊风险"],
            "expected_drawdown_12m": round(expected_dd, 2),
        }

    def _compute_fingerprint(self, stock_code: str, timestamp: str,
                              alpha_score: float,
                              thesis: ThesisObject | None) -> dict:
        """Compute decision fingerprint (agent_contract §7)."""
        thesis_id = thesis.thesis_id if thesis else "none"

        raw = f"{self.genome.agent_id}|{stock_code}|{timestamp}|{alpha_score}|{thesis_id}"
        decision_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]

        input_hash = hashlib.sha256(
            f"{stock_code}|{timestamp}".encode()
        ).hexdigest()[:16]

        return {
            "decision_hash": decision_hash,
            "input_hash": input_hash,
            "factor_snapshot_id": f"fs_{timestamp[:10]}",
            "model_version": self.genome.agent_id,
            "genome_hash": self.genome.genome_hash(),
        }

    def _get_factor(self, factors, name: str) -> float | None:
        """Look up a factor value from CompositeResult.factors list."""
        for f in factors.factors:
            if f.name == name:
                return f.raw_value
        return None

    def self_examine(self, decision: SecurityAnalysis,
                      outcome: dict) -> dict:
        """Self-review after T+N evaluation (agent_contract §11.3)."""
        was_correct = outcome.get("alpha_positive", False)
        error_type = None
        lesson = None

        if not was_correct:
            if outcome.get("alpha_vs_sector", 0) < 0:
                error_type = "THESIS_ERROR"
                lesson = "行业配置判断失误，需加强行业轮动分析"
            elif outcome.get("max_drawdown_during", 0) < -0.25:
                error_type = "RISK_ERROR"
                lesson = "风险敞口过大，需降低置信度较高时的仓位上限"
            else:
                error_type = "UNKNOWN"
                lesson = "失败原因待进一步分析"

        return {
            "decision_id": decision.decision_fingerprint.get("decision_hash"),
            "was_thesis_correct": was_correct,
            "error_type": error_type,
            "lesson_learned": lesson,
            "suggested_action": None,
        }
