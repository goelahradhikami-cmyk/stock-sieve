"""
Post-Mortem Analyzer — Structured failure analysis and mutation generation.

Implements Post-Mortem Engine Implementation Spec v1:
  - 5 primary error categories, 12 subtypes
  - Rule-based classification decision tree (LLM NOT involved in classification)
  - Auto-generates mutation_candidates per error type
  - Optional LLM deep analysis for detail_analysis field
  - Feeds mutation_candidates to Evolution Engine

Classification decision tree:
  profitable=false
    → invalidation triggered? → THESIS_ERROR
    → alpha_vs_sector<0 AND alpha_vs_market<0?
        → regime mismatch? → REGIME_ERROR
        → otherwise → REGIME_ERROR (factor_rotation)
    → valuation compression? → VALUATION_ERROR
    → had profit during? → TIMING_ERROR
    → oversize position? → RISK_ERROR (position_size)
    → default → RISK_ERROR (correlation_failure)
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
# Error Classification Enums
# ═══════════════════════════════════════════════════════════


class ErrorCategory(Enum):
    THESIS_ERROR = "THESIS_ERROR"
    VALUATION_ERROR = "VALUATION_ERROR"
    TIMING_ERROR = "TIMING_ERROR"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    REGIME_ERROR = "REGIME_ERROR"
    RISK_ERROR = "RISK_ERROR"


class ErrorSubtype(Enum):
    # THESIS_ERROR
    EVIDENCE_FAILURE = "evidence_failure"
    ASSUMPTION_FAILURE = "assumption_failure"
    CATALYST_FAILURE = "catalyst_failure"
    # VALUATION_ERROR
    MULTIPLE_COMPRESSION = "multiple_compression"
    EXPECTATION_OVERPRICED = "expectation_overpriced"
    # TIMING_ERROR
    EARLY_ENTRY = "early_entry"
    LATE_EXIT = "late_exit"
    CATALYST_DELAY = "catalyst_delay"
    # EXECUTION_ERROR
    SLIPPAGE_LOSS = "slippage_loss"
    # REGIME_ERROR
    MACRO_SHIFT = "macro_shift"
    FACTOR_ROTATION = "factor_rotation"
    # RISK_ERROR
    POSITION_SIZE_ERROR = "position_size_error"
    CORRELATION_FAILURE = "correlation_failure"


# ═══════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════


@dataclass
class PostMortemResult:
    """Complete post-mortem analysis of a failed investment decision."""

    decision_id: str
    agent_id: str
    stock_code: str
    error_category: ErrorCategory
    error_subtype: ErrorSubtype
    rule_trigger: dict  # Data that triggered the classification
    primary_cause: str  # Human-readable summary
    wrong_assumption: str | None = None
    missed_signal: str | None = None
    llm_analysis: str | None = None  # LLM-generated deep analysis (optional, deferred)
    lessons: dict = field(default_factory=dict)
    mutation_candidates: list = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# Lesson Templates
# ═══════════════════════════════════════════════════════════

LESSON_MAP = {
    (ErrorCategory.THESIS_ERROR, ErrorSubtype.EVIDENCE_FAILURE): {
        "lesson": "Thesis 证据被证伪，需加强证据验证",
        "action": "add_evidence_validation",
    },
    (ErrorCategory.THESIS_ERROR, ErrorSubtype.ASSUMPTION_FAILURE): {
        "lesson": "Thesis 核心假设被数据否定，需修改或移除该 thesis template",
        "action": "modify_thesis_template",
    },
    (ErrorCategory.THESIS_ERROR, ErrorSubtype.CATALYST_FAILURE): {
        "lesson": "Catalyst 过期未兑现，需增加时效性检查",
        "action": "add_catalyst_expiry_check",
    },
    (ErrorCategory.VALUATION_ERROR, ErrorSubtype.MULTIPLE_COMPRESSION): {
        "lesson": "估值压缩导致亏损，即使盈利增长符合预期",
        "action": "add_valuation_percentile_filter",
    },
    (ErrorCategory.VALUATION_ERROR, ErrorSubtype.EXPECTATION_OVERPRICED): {
        "lesson": "利好兑现但股价不涨反跌，需增加 sentiment divergence 检查",
        "action": "add_sentiment_divergence_check",
    },
    (ErrorCategory.TIMING_ERROR, ErrorSubtype.EARLY_ENTRY): {
        "lesson": "入场过早，持有期内先大跌再回升，需增加动量确认过滤",
        "action": "add_momentum_confirmation",
    },
    (ErrorCategory.TIMING_ERROR, ErrorSubtype.LATE_EXIT): {
        "lesson": "持有期过长，在盈利后未能及时止盈",
        "action": "add_trailing_stop",
    },
    (ErrorCategory.TIMING_ERROR, ErrorSubtype.CATALYST_DELAY): {
        "lesson": "Catalyst 延迟但最终兑现，需增加 thesis horizon 缓冲期",
        "action": "extend_thesis_horizon_buffer",
    },
    (ErrorCategory.REGIME_ERROR, ErrorSubtype.MACRO_SHIFT): {
        "lesson": "市场状态与策略不匹配，需增强环境识别",
        "action": "increase_regime_adapter_weight",
    },
    (ErrorCategory.REGIME_ERROR, ErrorSubtype.FACTOR_ROTATION): {
        "lesson": "因子 IC 剧烈变动，需触发因子记忆重新评估",
        "action": "trigger_factor_memory_reevaluation",
    },
    (ErrorCategory.RISK_ERROR, ErrorSubtype.POSITION_SIZE_ERROR): {
        "lesson": "仓位过大，超过 Kelly 建议上限",
        "action": "tighten_position_constraint",
    },
    (ErrorCategory.RISK_ERROR, ErrorSubtype.CORRELATION_FAILURE): {
        "lesson": "组合内多只股票同时下跌，分散化失效",
        "action": "add_effective_diversification_check",
    },
}


# ═══════════════════════════════════════════════════════════
# Mutation Generation Templates
# ═══════════════════════════════════════════════════════════


def generate_mutations(
    category: ErrorCategory, subtype: ErrorSubtype, trigger: dict, research: dict
) -> list[dict]:
    """Generate mutation candidates based on error classification."""

    mutations = []

    if category == ErrorCategory.THESIS_ERROR:
        thesis_pattern = research.get("thesis_pattern", "unknown")
        mutations.append(
            {
                "type": "reduce_thesis_scoring",
                "target": "thesis_engine.thesis_scoring",
                "pattern": thesis_pattern,
                "direction": "decrease",
                "delta_max": 0.05,
                "reason": f"连续 {subtype.value} 类型的 thesis 失败",
            }
        )
        # If evidence failure, suggest stricter evidence filters
        if subtype == ErrorSubtype.EVIDENCE_FAILURE:
            mutations.append(
                {
                    "type": "add_filter",
                    "target": "thesis_engine.evidence_rules",
                    "filter": "require_3rd_party_confirmation",
                    "threshold": True,
                    "reason": "证据被证伪，需增加第三方数据交叉验证",
                }
            )

    if category == ErrorCategory.VALUATION_ERROR:
        mutations.append(
            {
                "type": "add_filter",
                "target": "valuation_gate",
                "filter": "pe_percentile_max",
                "threshold": 0.70,
                "reason": "估值压缩导致损失，需增加估值分位数过滤",
            }
        )

    if category == ErrorCategory.TIMING_ERROR:
        if subtype == ErrorSubtype.EARLY_ENTRY:
            mutations.append(
                {
                    "type": "add_filter",
                    "target": "decision_graph",
                    "filter": "momentum_confirmation",
                    "threshold": "price_momentum_3m > 0",
                    "reason": "入场过早，需增加动量确认过滤",
                }
            )
        if subtype == ErrorSubtype.LATE_EXIT:
            mutations.append(
                {
                    "type": "add_filter",
                    "target": "decision_graph",
                    "filter": "trailing_stop",
                    "threshold": 0.15,  # 15% from peak
                    "reason": "未能及时止盈，需增加移动止损",
                }
            )

    if category == ErrorCategory.REGIME_ERROR:
        mutations.append(
            {
                "type": "adjust_regime_weight",
                "target": "market_regime_adapter",
                "direction": "increase_sensitivity",
                "reason": "市场状态与策略持续不匹配",
            }
        )

    if category == ErrorCategory.RISK_ERROR:
        if subtype == ErrorSubtype.POSITION_SIZE_ERROR:
            mutations.append(
                {
                    "type": "tighten_constraint",
                    "target": "position_sizing.single_position",
                    "constraint": "max_weight",
                    "current": research.get("final_weight", 0.10),
                    "suggested": max(0.03, research.get("final_weight", 0.10) * 0.7),
                    "reason": "仓位过大导致超出风险预算",
                }
            )
        if subtype == ErrorSubtype.CORRELATION_FAILURE:
            mutations.append(
                {
                    "type": "add_filter",
                    "target": "portfolio_constraints",
                    "filter": "effective_diversification",
                    "threshold": "effective_n >= 8",
                    "reason": "组合分散化不足，相关性危机中失效",
                }
            )

    return mutations


# ═══════════════════════════════════════════════════════════
# Post-Mortem Engine
# ═══════════════════════════════════════════════════════════


class PostMortemAnalyzer:
    """Rule-based failure classification and mutation generation.

    LLM is used ONLY for the optional detail_analysis text.
    Classification is 100% rule-engine driven.

    Note: distinct from ``postmortem.engine.PostMortemEngine`` (the daily
    batch orchestrator). This class analyzes a single decision and returns a
    ``PostMortemResult`` carrying ``mutation_candidates``.
    """

    def __init__(self, db, llm_client=None):
        self.db = db  # EvaluationDB instance
        self.llm = llm_client

    def run(self, evaluation_result_id: int) -> PostMortemResult:
        """Execute full post-mortem on a failed evaluation result.

        Steps:
          1. Load related data
          2. Rule-engine classify error
          3. Extract lessons
          4. Generate mutation candidates
          5. Optional LLM deep analysis
        """
        # ── 1. Load data ─────────────────────────────────
        eval_data = (
            self.db.connect()
            .execute("SELECT * FROM evaluation_results WHERE id = ?", (evaluation_result_id,))
            .fetchone()
        )
        if not eval_data:
            raise ValueError(f"Evaluation result {evaluation_result_id} not found")

        eval_data = dict(eval_data)

        research = (
            self.db.connect()
            .execute(
                "SELECT * FROM research_decisions WHERE id = ?",
                (eval_data["research_decision_id"],),
            )
            .fetchone()
        )
        research = dict(research) if research else {}

        # ── 2. Rule-engine classify ──────────────────────
        category, subtype, trigger = self._classify(eval_data, research)

        # ── 3. Extract lessons ───────────────────────────
        lesson_key = (category, subtype)
        lesson_data = LESSON_MAP.get(
            lesson_key,
            {
                "lesson": f"{category.value}/{subtype.value} 类型的损失",
                "action": "review_related_filters",
            },
        )
        lessons = {
            "lesson": lesson_data["lesson"],
            "action": lesson_data["action"],
            "detail": json.dumps(trigger, default=str),
        }

        # ── 4. Generate mutations ────────────────────────
        mutations = generate_mutations(category, subtype, trigger, research)

        # ── 5. Primary cause ────────────────────────────
        primary_cause = self._build_primary_cause(category, subtype, trigger, research)

        # ── 6. Optional LLM deep analysis ────────────────
        llm_text = None
        if self.llm:
            llm_text = self._llm_analyze(eval_data, research, category, subtype)

        return PostMortemResult(
            decision_id=str(research.get("id", eval_data["research_decision_id"])),
            agent_id=research.get("agent_id", "unknown"),
            stock_code=research.get("security_id", "unknown"),
            error_category=category,
            error_subtype=subtype,
            rule_trigger=trigger,
            primary_cause=primary_cause,
            wrong_assumption=self._extract_wrong_assumption(category, subtype, research),
            missed_signal=self._extract_missed_signal(category, subtype, trigger),
            llm_analysis=llm_text,
            lessons=lessons,
            mutation_candidates=mutations,
        )

    # ── Classification Decision Tree §1.1 ──────────────────

    def _classify(self, eval_data: dict, research: dict) -> tuple:
        """Rule-based classification decision tree.

        Returns (ErrorCategory, ErrorSubtype, trigger_dict).
        """
        # ── Check invalidation conditions ────────────────
        invalidation_raw = research.get("thesis_invalidation", "[]")
        try:
            if isinstance(invalidation_raw, str):
                invalidation = json.loads(invalidation_raw)
            else:
                invalidation = invalidation_raw
        except (json.JSONDecodeError, TypeError):
            invalidation = []

        if self._invalidation_triggered(invalidation, eval_data):
            subtype = self._classify_invalidation_subtype(invalidation, eval_data, research)
            return (
                ErrorCategory.THESIS_ERROR,
                subtype,
                {
                    "invalidation_triggered": True,
                    "triggered_conditions": self._get_triggered_invalidations(
                        invalidation, eval_data
                    ),
                },
            )

        # ── Check Alpha: both market and sector negative ──
        alpha_sector = eval_data.get("alpha_vs_sector") or 0
        alpha_market = eval_data.get("alpha_vs_market") or 0

        if alpha_sector < 0 and alpha_market < 0:
            regime = eval_data.get("market_regime", "rotation")
            agent_pref = self._get_agent_regime_preference(research.get("agent_id", ""))

            # Check regime mismatch
            if agent_pref:
                pref_score = agent_pref.get(regime, 0.5)
                if pref_score < 0.3:
                    return (
                        ErrorCategory.REGIME_ERROR,
                        ErrorSubtype.MACRO_SHIFT,
                        {
                            "regime": regime,
                            "agent_preference": agent_pref,
                            "pref_score": pref_score,
                        },
                    )
                else:
                    return (
                        ErrorCategory.REGIME_ERROR,
                        ErrorSubtype.FACTOR_ROTATION,
                        {"regime": regime, "factor_rotation_detected": True},
                    )

            return (
                ErrorCategory.REGIME_ERROR,
                ErrorSubtype.FACTOR_ROTATION,
                {"regime": regime, "factor_rotation_detected": True},
            )

        # ── Check valuation compression ──────────────────
        if self._valuation_compressed(eval_data, research):
            return (
                ErrorCategory.VALUATION_ERROR,
                ErrorSubtype.MULTIPLE_COMPRESSION,
                {
                    "pe_change": eval_data.get("pe_change", 0),
                    "pb_change": eval_data.get("pb_change", 0),
                },
            )

        # ── Check: had profit during holding period ──────
        max_profit = eval_data.get("max_profit_during") or 0
        max_drawdown = eval_data.get("max_drawdown_during") or 0
        stock_return = eval_data.get("stock_return") or 0

        if max_profit > 0.05 and stock_return < 0:
            return (
                ErrorCategory.TIMING_ERROR,
                ErrorSubtype.LATE_EXIT,
                {
                    "max_profit_during": max_profit,
                    "final_return": stock_return,
                },
            )

        # ── Check: early entry (deep drawdown then recovery) ──
        if max_drawdown < -0.15 and stock_return > -0.05:
            return (
                ErrorCategory.TIMING_ERROR,
                ErrorSubtype.EARLY_ENTRY,
                {
                    "max_drawdown_during": max_drawdown,
                    "final_return": stock_return,
                },
            )

        # ── Check position size ──────────────────────────
        portfolio_weight = research.get("final_weight", 0)
        if portfolio_weight > 0.10:
            return (
                ErrorCategory.RISK_ERROR,
                ErrorSubtype.POSITION_SIZE_ERROR,
                {"final_weight": portfolio_weight},
            )

        # ── Default: correlation failure ─────────────────
        return (
            ErrorCategory.RISK_ERROR,
            ErrorSubtype.CORRELATION_FAILURE,
            {"note": "default classification — probable correlation failure"},
        )

    def _invalidation_triggered(self, invalidation: list, eval_data: dict) -> bool:
        """Check if any invalidation condition was triggered."""
        if not invalidation:
            return False

        # Check simple invalidation conditions
        factor_snapshot = eval_data.get("factor_snapshot", "{}")
        if isinstance(factor_snapshot, str):
            try:
                factor_snapshot = json.loads(factor_snapshot)
            except json.JSONDecodeError:
                factor_snapshot = {}

        for cond in invalidation:
            if isinstance(cond, dict):
                condition_str = cond.get("condition", "")
                if self._evaluate_condition(condition_str, factor_snapshot):
                    return True
            elif isinstance(cond, str):
                if self._evaluate_condition(cond, factor_snapshot):
                    return True

        return False

    def _evaluate_condition(self, condition_str: str, factor_snapshot: dict) -> bool:
        """Evaluate simple conditions like 'roe_ttm < 0.20' against factor snapshot."""
        try:
            condition_str = condition_str.strip()
            if " < " in condition_str:
                metric, threshold = condition_str.split(" < ")
                value = factor_snapshot.get(metric.strip(), 0)
                return float(value) < float(threshold)
            elif " > " in condition_str:
                metric, threshold = condition_str.split(" > ")
                value = factor_snapshot.get(metric.strip(), 0)
                return float(value) > float(threshold)
        except (ValueError, TypeError):
            pass
        return False

    def _valuation_compressed(self, eval_data: dict, research: dict) -> bool:
        """Check if valuation multiple compression occurred."""
        pe_change = eval_data.get("pe_change", 0) or 0
        pb_change = eval_data.get("pb_change", 0) or 0
        return pe_change < -0.15 or pb_change < -0.15

    def _get_agent_regime_preference(self, agent_id: str) -> dict:
        """Load agent's market_regime_preference from genome."""
        if not agent_id:
            return {}

        row = (
            self.db.connect()
            .execute(
                "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id = ? AND status = 'active' ORDER BY birth_date DESC LIMIT 1",
                (agent_id,),
            )
            .fetchone()
        )

        if not row:
            return {}

        try:
            import yaml

            genome = yaml.safe_load(row[0]) or {}
            return genome.get("doctrine", {}).get("market_regime_preference", {})
        except Exception:
            return {}

    def _classify_invalidation_subtype(
        self, invalidation: list, eval_data: dict, research: dict
    ) -> ErrorSubtype:
        """Determine which subtype of THESIS_ERROR occurred."""
        # Check if catalyst was the issue
        thesis_catalyst = research.get("thesis_catalyst", "")
        if thesis_catalyst:
            return ErrorSubtype.CATALYST_FAILURE

        # Check if evidence failed
        evidence_raw = research.get("thesis_evidence", "[]")
        try:
            evidence = json.loads(evidence_raw) if isinstance(evidence_raw, str) else evidence_raw
            if len(evidence) > 0:
                return ErrorSubtype.EVIDENCE_FAILURE
        except (json.JSONDecodeError, TypeError):
            pass

        return ErrorSubtype.ASSUMPTION_FAILURE

    def _get_triggered_invalidations(self, invalidation: list, eval_data: dict) -> list:
        """Return list of triggered invalidation conditions."""
        factor_snapshot = eval_data.get("factor_snapshot", "{}")
        if isinstance(factor_snapshot, str):
            try:
                factor_snapshot = json.loads(factor_snapshot)
            except json.JSONDecodeError:
                factor_snapshot = {}

        triggered = []
        for cond in invalidation:
            if isinstance(cond, dict):
                condition_str = cond.get("condition", "")
                if self._evaluate_condition(condition_str, factor_snapshot):
                    triggered.append(cond)
            elif isinstance(cond, str):
                if self._evaluate_condition(cond, factor_snapshot):
                    triggered.append({"condition": cond})

        return triggered

    # ── Cause extraction ──────────────────────────────────

    def _build_primary_cause(
        self, category: ErrorCategory, subtype: ErrorSubtype, trigger: dict, research: dict
    ) -> str:
        """Build human-readable primary cause string."""
        stock = research.get("security_id", "unknown")
        pattern = research.get("thesis_pattern", "unknown")
        causes = {
            (
                ErrorCategory.THESIS_ERROR,
                ErrorSubtype.EVIDENCE_FAILURE,
            ): f"{stock} thesis '{pattern}' evidence invalidated by market data",
            (
                ErrorCategory.THESIS_ERROR,
                ErrorSubtype.ASSUMPTION_FAILURE,
            ): f"{stock} core assumption of thesis '{pattern}' disproven",
            (
                ErrorCategory.THESIS_ERROR,
                ErrorSubtype.CATALYST_FAILURE,
            ): f"{stock} catalyst for thesis '{pattern}' expired without effect",
            (
                ErrorCategory.VALUATION_ERROR,
                ErrorSubtype.MULTIPLE_COMPRESSION,
            ): f"{stock} PE/PB multiple compressed despite fundamentals intact",
            (
                ErrorCategory.VALUATION_ERROR,
                ErrorSubtype.EXPECTATION_OVERPRICED,
            ): f"{stock} positive news failed to lift price (expectation overpriced)",
            (
                ErrorCategory.TIMING_ERROR,
                ErrorSubtype.EARLY_ENTRY,
            ): f"{stock} entered too early — suffered {trigger.get('max_drawdown_during', 0):.1%} drawdown before recovery",
            (
                ErrorCategory.TIMING_ERROR,
                ErrorSubtype.LATE_EXIT,
            ): f"{stock} held too long — gave back {trigger.get('max_profit_during', 0):.1%} profit to {trigger.get('final_return', 0):.1%} loss",
            (
                ErrorCategory.TIMING_ERROR,
                ErrorSubtype.CATALYST_DELAY,
            ): f"{stock} catalyst delayed beyond thesis horizon",
            (
                ErrorCategory.REGIME_ERROR,
                ErrorSubtype.MACRO_SHIFT,
            ): f"Market regime '{trigger.get('regime', 'unknown')}' mismatched agent strategy",
            (
                ErrorCategory.REGIME_ERROR,
                ErrorSubtype.FACTOR_ROTATION,
            ): f"Factor rotation during regime '{trigger.get('regime', 'unknown')}'",
            (
                ErrorCategory.RISK_ERROR,
                ErrorSubtype.POSITION_SIZE_ERROR,
            ): f"{stock} position {trigger.get('final_weight', 0):.1%} exceeded risk budget",
            (
                ErrorCategory.RISK_ERROR,
                ErrorSubtype.CORRELATION_FAILURE,
            ): f"{stock} diversification failed — correlation breakdown in crisis",
        }
        return causes.get((category, subtype), f"{category.value}/{subtype.value} on {stock}")

    def _extract_wrong_assumption(
        self, category: ErrorCategory, subtype: ErrorSubtype, research: dict
    ) -> str | None:
        """Extract which assumption was wrong."""
        if category == ErrorCategory.THESIS_ERROR:
            return research.get("thesis_claim", "Unknown thesis claim")
        if category == ErrorCategory.VALUATION_ERROR:
            return "Assumed valuation multiple would remain stable or expand"
        if category == ErrorCategory.TIMING_ERROR:
            return "Assumed timing of entry/exit was optimal"
        if category == ErrorCategory.REGIME_ERROR:
            return "Assumed market regime would favor current strategy"
        if category == ErrorCategory.RISK_ERROR:
            return "Assumed portfolio diversification would protect against drawdown"
        return None

    def _extract_missed_signal(
        self, category: ErrorCategory, subtype: ErrorSubtype, trigger: dict
    ) -> str | None:
        """Identify what signal was missed."""
        signals = {
            (
                ErrorCategory.THESIS_ERROR,
                ErrorSubtype.EVIDENCE_FAILURE,
            ): "Early evidence contradicting thesis claim",
            (
                ErrorCategory.VALUATION_ERROR,
                ErrorSubtype.MULTIPLE_COMPRESSION,
            ): "Rising interest rates or sector de-rating signals",
            (
                ErrorCategory.TIMING_ERROR,
                ErrorSubtype.LATE_EXIT,
            ): f"Exit signal at {trigger.get('max_profit_during', 0):.1%} profit peak",
            (
                ErrorCategory.TIMING_ERROR,
                ErrorSubtype.EARLY_ENTRY,
            ): "Momentum confirmation before entry",
            (
                ErrorCategory.REGIME_ERROR,
                ErrorSubtype.MACRO_SHIFT,
            ): f"Early regime shift indicators for regime '{trigger.get('regime', 'unknown')}'",
            (
                ErrorCategory.RISK_ERROR,
                ErrorSubtype.POSITION_SIZE_ERROR,
            ): "Kelly formula suggesting lower position size",
            (
                ErrorCategory.RISK_ERROR,
                ErrorSubtype.CORRELATION_FAILURE,
            ): "Rising cross-asset correlation before crisis",
        }
        return signals.get((category, subtype))

    # ── LLM Analysis (optional, deferred) ─────────────────

    def _llm_analyze(
        self, eval_data: dict, research: dict, category: ErrorCategory, subtype: ErrorSubtype
    ) -> str | None:
        """Generate natural language post-mortem analysis via LLM.

        Per evolution_engine_spec §8: LLM only provides observation.
        Classification already done by rule engine.
        """
        if not self.llm:
            return None

        prompt = f"""[LLM] 投资尸检分析 — 仅用于解释复盘，不修改分类。

错误类型: {category.value}/{subtype.value}
股票: {research.get("security_id", "unknown")}
Thesis: {research.get("thesis_claim", "无")}
Alpha评分: {research.get("alpha_score", "N/A")}
确信度: {research.get("confidence", "N/A")}
实际收益: {eval_data.get("stock_return", "N/A")}
行业收益: {eval_data.get("sector_return", "N/A")}
市场收益: {eval_data.get("market_return", "N/A")}
持有期最大回撤: {eval_data.get("max_drawdown_during", "N/A")}

请分析（必须引用上面具体数据）：
1. 这次失败的根本原因是什么？
2. 当时忽略了什么关键信号？
3. 未来应如何避免此类错误？"""

        try:
            return self.llm.complete(prompt)
        except Exception:
            return None

    # ── Batch analysis ───────────────────────────────────

    def run_batch(self, evaluation_result_ids: list[int]) -> list[PostMortemResult]:
        """Run post-mortem on multiple evaluation results."""
        results = []
        for eid in evaluation_result_ids:
            try:
                result = self.run(eid)
                results.append(result)
            except Exception as e:
                logger.info(f"Post-mortem failed for eval_id={eid}: {e}")
        return results


# ═══════════════════════════════════════════════════════════
# Evolution Engine Integration
# ═══════════════════════════════════════════════════════════


def collect_post_mortem_mutations(db, agent_id: str, lookback_months: int = 6) -> list[dict]:
    """Collect mutation_candidates from recent post-mortems.

    Candidates appearing ≥3 times automatically enter sandbox queue.
    Intended for use by Evolution Engine.
    """
    conn = db.connect()
    six_months_ago = f"date('now', '-{lookback_months} months')"
    rows = conn.execute(
        f"""
        SELECT mutation_candidates
        FROM post_mortems
        WHERE agent_id = ?
          AND created_at > {six_months_ago}
          AND applied = 0
    """,
        (agent_id,),
    ).fetchall()
    conn.close()

    all_candidates = []
    for row in rows:
        if row[0]:
            try:
                candidates = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                all_candidates.extend(candidates)
            except (json.JSONDecodeError, TypeError):
                pass

    # Count frequency by (type, target)
    counter = Counter((c.get("type", ""), c.get("target", "")) for c in all_candidates)

    # Return candidates with frequency ≥ 3
    return [c for c in all_candidates if counter[(c.get("type", ""), c.get("target", ""))] >= 3]


def update_failure_patterns(db, post_mortem_results: list[PostMortemResult]):
    """Update failure_patterns table with new post-mortem data.

    Creates or increments failure pattern records for pattern-based learning.
    """
    conn = db.connect()

    for pm in post_mortem_results:
        pattern_id = f"{pm.error_category.value}_{pm.error_subtype.value}"

        # Check if pattern exists
        existing = conn.execute(
            "SELECT id, occurrence_count FROM failure_patterns WHERE pattern_id = ?", (pattern_id,)
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE failure_patterns
                SET occurrence_count = occurrence_count + 1,
                    last_occurrence = date('now'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE pattern_id = ?
            """,
                (pattern_id,),
            )
        else:
            conn.execute(
                """
                INSERT INTO failure_patterns
                (pattern_id, pattern_name, error_category, error_subtype,
                 occurrence_count, last_occurrence,
                 avg_loss_magnitude, pattern_confidence)
                VALUES (?, ?, ?, ?, 1, date('now'), 0.0, 0.0)
            """,
                (
                    pattern_id,
                    f"{pm.error_category.value}/{pm.error_subtype.value}",
                    pm.error_category.value,
                    pm.error_subtype.value,
                ),
            )

    conn.commit()
    conn.close()
