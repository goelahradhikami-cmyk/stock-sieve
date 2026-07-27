"""
Investment Committee Agent — Governance decision hub.

Phase 5A-3 Frozen.

Six roles review each SecurityAnalysis:
  - Valuation Reviewer, Industry Reviewer, Risk Controller
  - Quant Auditor, Devil's Advocate
  - Chairman (aggregates scores, issues verdict)

All scoring is rule-driven (pure Python). LLM only generates
natural language member_statements from scored attack_points.

Verdict flow:
  APPROVE → full position allowed
  APPROVE_WITH_CONDITIONS → capped position + monitoring flags
  RETURN_FOR_REVISION → revise and resubmit (max 1 revision)
  REJECT → blocked from portfolio

Key constraints:
  - confidence_modifier ≤ 0.0 (committee can only REDUCE confidence)
  - position_cap_modifier follows position_policy in config
  - max_revisions = 1 (dead-loop prevention)
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from .committee_roles import (
    devil_advocate,
    industry_reviewer,
    quant_auditor,
    risk_controller,
    valuation_reviewer,
)
from .portfolio_agent import PortfolioState

# ═══════════════════════════════════════════════════════════
# CommitteeDecision
# ═══════════════════════════════════════════════════════════


@dataclass
class CommitteeDecision:
    """Output of Investment Committee review."""

    committee_id: str
    research_decision_id: int

    # Role scores (0-100)
    valuation_score: float = 50.0
    industry_score: float = 50.0
    risk_score: float = 50.0
    quant_score: float = 50.0
    devil_advocate_score: float = 50.0

    # Chairman ruling
    weighted_score: float = 50.0
    verdict: str = "REJECT"  # APPROVE / APPROVE_WITH_CONDITIONS / REJECT / RETURN_FOR_REVISION
    verdict_reason: str = ""
    revision_count: int = 0

    # Portfolio Agent instructions
    position_cap_modifier: float = 1.0
    confidence_modifier: float = 0.0  # ≤ 0.0 — can only reduce
    monitoring_flags: list[str] = field(default_factory=list)
    required_conditions: list[str] = field(default_factory=list)

    # Debate records
    member_statements: dict = field(default_factory=dict)
    devil_advocate_attack_points: list[str] = field(default_factory=list)
    devil_advocate_attack: str = ""  # spec §4.1：致命攻击点描述
    debate_transcript: str = ""

    created_at: str = ""


# ═══════════════════════════════════════════════════════════
# Chairman Verdict
# ═══════════════════════════════════════════════════════════


def chairman_decision(scores: dict, revision_count: int = 0, config: dict | None = None) -> tuple:
    """Aggregate five role scores into final verdict.

    Args:
        scores: {valuation, industry, risk, quant, devil_advocate}
        revision_count: current revision number (dead-loop prevention)
        config: committee protocol config dict

    Returns:
        (verdict: str, weighted_score: float, reason: str)
    """
    if config is None:
        config = {}

    chairman_cfg = config.get("committee", {}).get("chairman", {}).get("verdict_rules", {})
    role_cfg = config.get("committee", {}).get("roles", {})

    # Weights from config
    weights = {
        "valuation": role_cfg.get("valuation_reviewer", {}).get("weight", 0.20),
        "industry": role_cfg.get("industry_reviewer", {}).get("weight", 0.20),
        "risk": role_cfg.get("risk_controller", {}).get("weight", 0.25),
        "quant": role_cfg.get("quant_auditor", {}).get("weight", 0.15),
        "devil_advocate": role_cfg.get("devil_advocate", {}).get("weight", 0.20),
    }

    # Normalize weights
    total_w = sum(weights.values())
    if total_w > 0:
        weights = {k: v / total_w for k, v in weights.items()}

    weighted = sum(scores.get(k, 50.0) * weights[k] for k in weights)

    fatal_threshold = chairman_cfg.get("fatal_reject_threshold", 30)
    return_threshold = chairman_cfg.get("return_threshold", 50)
    approve_threshold = chairman_cfg.get("approve_threshold", 70)
    conditional_threshold = chairman_cfg.get("conditional_threshold", 60)
    max_revisions = chairman_cfg.get("max_revisions", 1)

    # ── Fatal reject ────────────────────────────────────
    if scores.get("risk", 50.0) < fatal_threshold:
        return (
            "REJECT",
            weighted,
            f"风险评分{scores['risk']:.0f}低于致命阈值{fatal_threshold}，委员会否决",
        )
    if scores.get("devil_advocate", 50.0) < fatal_threshold:
        return (
            "REJECT",
            weighted,
            f"魔鬼代言人评分{scores['devil_advocate']:.0f}低于致命阈值{fatal_threshold}，核心假设存在致命缺陷",
        )

    # ── Return for revision (dead-loop prevention) ───────
    if revision_count < max_revisions:
        low_scores = [k for k, v in scores.items() if v < return_threshold]
        if len(low_scores) >= 2:
            return (
                "RETURN_FOR_REVISION",
                weighted,
                f"多个维度评分过低: {low_scores}，请修正后重新提交（第{revision_count + 1}次）",
            )

    # ── Approve with conditions (spec §6 rule 3，先于批准) ─
    # 加权综合良好(>=conditional_threshold)但存在弱维度(<60) → 有条件通过。
    # 必须放在 rule 4 批准之前，否则高分但带弱项会被误判为 APPROVE。
    if weighted >= conditional_threshold:
        weak_dimensions = [k for k, v in scores.items() if v < 60]
        if weak_dimensions:
            return (
                "APPROVE_WITH_CONDITIONS",
                weighted,
                f"加权综合评分{weighted:.1f}，附带监控条件通过（弱项: {weak_dimensions}）",
            )

    # ── Approve (spec §6 rule 4) ─────────────────────────
    if weighted >= approve_threshold:
        return "APPROVE", weighted, f"加权综合评分{weighted:.1f}，委员会批准"

    # ── Edge: too low → reject ───────────────────────────
    if weighted < 50:
        return "REJECT", weighted, f"加权综合评分{weighted:.1f}低于50，委员会否决"

    return (
        "APPROVE_WITH_CONDITIONS",
        weighted,
        f"加权综合评分{weighted:.1f}，附带严格监控条件通过",
    )


# ═══════════════════════════════════════════════════════════
# Committee Agent
# ═══════════════════════════════════════════════════════════


class CommitteeAgent:
    """Investment Committee — governance decision hub.

    Reviews SecurityAnalysis through six perspectives,
    issues binding verdict with position constraints.
    """

    def __init__(self, db=None, llm_client=None, config_path: str | None = None):
        self.db = db
        self.llm = llm_client

        # Load protocol config
        if config_path is None:
            import os

            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "config",
                "committee_protocol.yaml",
            )

        with open(config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def review(
        self,
        security_analysis,
        validation_result,
        market_snapshot: dict | None = None,
        portfolio_state: dict | None = None,
        revision_count: int = 0,
        factor_snapshot: dict | None = None,
        sector: str | None = None,
    ) -> CommitteeDecision:
        """Review a SecurityAnalysis through the committee.

        Args:
            security_analysis: SecurityAnalysis from Research Agent
            validation_result: ValidationResult from Thesis Validator
            market_snapshot: current MarketSnapshot as dict
            portfolio_state: current PortfolioState as dict
            revision_count: how many times this has been revised

        Returns:
            CommitteeDecision with verdict and position modifiers.
        """
        now = datetime.now().isoformat()
        committee_id = hashlib.sha256(
            f"{security_analysis.agent_id}|{security_analysis.stock_code}|{now}".encode()
        ).hexdigest()[:12]

        # Extract thesis and factor data
        thesis = self._thesis_to_dict(security_analysis)
        fs_dict = self._build_factor_snapshot(security_analysis, factor_snapshot, sector)

        # ── 1. Score each role ────────────────────────────
        val_score = valuation_reviewer.score_valuation(thesis, fs_dict, validation_result)
        ind_score = industry_reviewer.score_industry(thesis, market_snapshot, fs_dict)
        risk_score = risk_controller.score_risk(
            portfolio_state, security_analysis, validation_result, fs_dict
        )
        quant_score = quant_auditor.score_quant(
            security_analysis.agent_id,
            thesis.get("pattern", ""),
            self.db,
        )
        da_score, attack_points = devil_advocate.score_devil_advocate(
            thesis, validation_result, fs_dict
        )

        scores = {
            "valuation": val_score,
            "industry": ind_score,
            "risk": risk_score,
            "quant": quant_score,
            "devil_advocate": da_score,
        }

        # ── 2. Chairman verdict ───────────────────────────
        verdict, weighted_score, reason = chairman_decision(scores, revision_count, self.config)

        # ── 3. Position modifiers ─────────────────────────
        position_policy = (
            self.config.get("committee", {}).get("chairman", {}).get("position_policy", {})
        )
        cap_modifier = {
            "APPROVE": position_policy.get("approve_cap_modifier", 1.0),
            "APPROVE_WITH_CONDITIONS": position_policy.get(
                "approve_with_conditions_cap_modifier", 0.5
            ),
            "REJECT": position_policy.get("reject_cap_modifier", 0.0),
            "RETURN_FOR_REVISION": position_policy.get("return_for_revision_cap_modifier", 0.0),
        }.get(verdict, 0.0)

        # Confidence modifier: 委员会只可下调（≤0）。
        # 注意：该值在 apply_committee_decision 中作为权重乘子 (1+mod) 使用，
        # 必须是小比例（如 -0.10），否则会直接清零仓位（spec §7）。
        confidence_mod = 0.0
        if verdict == "APPROVE_WITH_CONDITIONS":
            confidence_mod -= 0.10
        if risk_score < 60:
            confidence_mod -= 0.05
        if quant_score < 60:
            confidence_mod -= 0.05
        confidence_mod = max(-0.30, round(confidence_mod, 2))

        # ── 4. Monitoring flags ───────────────────────────
        monitoring = (
            self.config.get("committee", {}).get("chairman", {}).get("monitoring_thresholds", {})
        )
        flags = []
        if val_score < monitoring.get("valuation", 60):
            flags.append("MONITOR_VALUATION")
        if risk_score < monitoring.get("risk", 50):
            flags.append("MONITOR_RISK_ELEVATED")
        if quant_score < monitoring.get("quant", 60):
            flags.append("MONITOR_ALPHA_QUALITY")
        if da_score < monitoring.get("devil_advocate", 50):
            flags.append("MONITOR_THESIS_INTEGRITY")
        if verdict == "APPROVE_WITH_CONDITIONS":
            flags.append("COMMITTEE_CONDITIONAL_APPROVAL")

        # ── 5. Required conditions ────────────────────────
        required = []
        if val_score < 60:
            required.append("入场前确认PE分位 < 70%")
        if risk_score < 60:
            required.append("持仓期间每周审查风险敞口")

        # ── 6. LLM member statements（spec §8 边界）────────
        # 默认使用 RuleOnlyLLMBridge 生成确定性陈述（标注 [LLM-vX]，仅扩写锚点，
        # 不引入新事实）；若配置了真实 LLM 且 enabled，则走 _generate_statements。
        statements = {}
        devil_attack = ""
        debate_transcript = ""
        llm_cfg = self.config.get("llm", {})
        role_genomes = self.config.get("committee", {}).get("roles", {})
        if self.llm is not None and llm_cfg.get("enabled", False):
            statements = self._generate_statements(scores, attack_points, thesis)
            devil_attack = "\n".join(f"- {a}" for a in attack_points) if attack_points else ""
            debate_transcript = self._generate_transcript(statements, verdict)
        else:
            bridge = RuleOnlyLLMBridge(version=llm_cfg.get("statement_version", "1.0"))
            statements = bridge.generate_statements(scores, role_genomes, thesis, verdict)
            devil_attack = bridge.generate_devil_attack(attack_points, thesis)
            debate_transcript = bridge.generate_transcript(
                statements, devil_attack, verdict, reason
            )

        return CommitteeDecision(
            committee_id=committee_id,
            research_decision_id=getattr(validation_result, "research_decision_id", 0),
            valuation_score=round(val_score, 1),
            industry_score=round(ind_score, 1),
            risk_score=round(risk_score, 1),
            quant_score=round(quant_score, 1),
            devil_advocate_score=round(da_score, 1),
            weighted_score=round(weighted_score, 1),
            verdict=verdict,
            verdict_reason=reason,
            revision_count=revision_count,
            position_cap_modifier=cap_modifier,
            confidence_modifier=round(confidence_mod, 1),
            monitoring_flags=flags,
            required_conditions=required,
            devil_advocate_attack=devil_attack,
            member_statements=statements,
            devil_advocate_attack_points=attack_points,
            debate_transcript=debate_transcript,
            created_at=now,
        )

    def _build_factor_snapshot(self, security_analysis, factor_snapshot, sector=None) -> dict:
        """构建供角色函数使用的归一化因子快照。

        合并顺序（后者覆盖前者）：
          1. SecurityAnalysis.factor_profile（复合分，缺估值原始字段）
          2. 调用方显式传入的 factor_snapshot（数据层 FactorSnapshot / 原始 dict）
        并补充：
          - sector（行业集中度与动量匹配用）
          - pe_percentile：若缺失但有 pe_ttm，用启发式映射兜底
          - revenue_growth_yoy：映射自 revenue_growth_1y
        """
        fs: dict = {}
        fp = getattr(security_analysis, "factor_profile", None) or {}
        if isinstance(fp, dict):
            fs.update(fp)
        if isinstance(factor_snapshot, dict):
            fs.update(factor_snapshot)
        elif factor_snapshot is not None:
            # 数据层 FactorSnapshot dataclass
            for k in (
                "pe_ttm",
                "pb",
                "fcf_yield",
                "revenue_growth_1y",
                "revenue_growth_yoy",
                "liquidity_score",
                "sector",
            ):
                v = getattr(factor_snapshot, k, None)
                if v is not None:
                    fs[k] = v

        if sector:
            fs["sector"] = sector

        # 派生 pe_percentile（启发式，仅当缺失且可拿到 pe_ttm）
        if fs.get("pe_percentile") is None and fs.get("pe_ttm") is not None:
            fs["pe_percentile"] = _pe_to_percentile(fs["pe_ttm"])

        # revenue_growth_1y → revenue_growth_yoy（角色函数两种键名都认）
        if fs.get("revenue_growth_yoy") is None and fs.get("revenue_growth_1y") is not None:
            fs["revenue_growth_yoy"] = fs["revenue_growth_1y"]

        return fs

    def save(self, decision: "CommitteeDecision", db=None) -> int:
        """将委员会决策持久化到 evaluation_db。

        需要 committee_decisions 表已迁移到 v2.1.1
        （见 EvaluationDB.migrate_committee_decisions_v2_1_1）。
        """
        target = db or self.db
        if target is None:
            raise ValueError("save() 需要 db 参数或 CommitteeAgent 初始化时传入 db")
        return target.insert_committee_decision(decision)

    def _thesis_to_dict(self, sa) -> dict:
        """Convert SecurityAnalysis thesis to dict for scoring functions."""
        thesis = sa.thesis
        if thesis is None:
            return {"claim": "", "evidence": [], "invalidation": [], "family": "unknown"}

        return {
            "claim": thesis.claim or "",
            "evidence": [
                {"metric": e.get("metric"), "value": e.get("value")}
                for e in (thesis.evidence or [])
            ],
            "invalidation": [
                {"condition": i.get("condition")} for i in (thesis.invalidation or [])
            ],
            "family": thesis.family or "unknown",
            "pattern": thesis.pattern or "",
            "thesis_id": thesis.thesis_id or "",
        }

    def _generate_statements(self, scores: dict, attack_points: list, thesis: dict) -> dict:
        """Generate natural language statements from scores using LLM.

        LLM only expands attack_points anchors — never invents new ones.
        """
        if not self.llm:
            return {}

        statements = {}
        role_genomes = self.config.get("committee", {}).get("roles", {})

        for role_key in [
            "valuation_reviewer",
            "industry_reviewer",
            "risk_controller",
            "quant_auditor",
            "devil_advocate",
        ]:
            genome = role_genomes.get(role_key, {}).get("genome", {})
            voice = genome.get("voice", "")
            doctrine = genome.get("doctrine", "")

            prompt = f"""[LLM] 投资委员会陈述生成 — 仅扩写给定锚点，不引入新事实。

角色: {role_key}
角色信条: {doctrine}
表达风格: {voice}
当前评分: {scores.get(role_key.replace("_reviewer", "").replace("_controller", "").replace("_auditor", "").replace("_advocate", ""), 50):.0f}/100
"""

            if role_key == "devil_advocate" and attack_points:
                prompt += "\n强制攻击锚点（只能基于以下锚点展开，不得编造新攻击维度）:\n"
                for ap in attack_points:
                    prompt += f"  - {ap}\n"

            try:
                result = self.llm.complete(prompt)
                statements[role_key] = f"[LLM-v1.0] {result}" if result else ""
            except Exception:
                statements[role_key] = ""

        return statements

    def _generate_transcript(self, statements: dict, verdict: str) -> str:
        """Generate simulated committee debate transcript (display only)."""
        if not statements:
            return ""

        lines = ["=== 投资委员会辩论记录 ===", ""]
        for role, statement in statements.items():
            if statement:
                role_name = {
                    "valuation_reviewer": "估值审查员",
                    "industry_reviewer": "行业审查员",
                    "risk_controller": "风控官",
                    "quant_auditor": "量化审计员",
                    "devil_advocate": "魔鬼代言人",
                }.get(role, role)
                lines.append(f"【{role_name}】")
                lines.append(statement.replace("[LLM-v1.0] ", ""))
                lines.append("")

        verdict_cn = {
            "APPROVE": "✅ 批准",
            "APPROVE_WITH_CONDITIONS": "⚠️ 有条件批准",
            "REJECT": "❌ 否决",
            "RETURN_FOR_REVISION": "↩️ 退回修订",
        }.get(verdict, verdict)

        lines.append(f"【主席裁决】 {verdict_cn}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════
# Rule-Only LLM Bridge (spec §8 默认实现)
# ═══════════════════════════════════════════════


class RuleOnlyLLMBridge:
    """确定性陈述生成器：当 config llm.enabled=false 时使用。

    严格基于评分与 genome 的 voice/doctrine 生成质询文本，所有输出标注
    [LLM-v{version}] 前缀；绝不引入新事实、不修改评分、不修改 verdict
    （spec §8 LLM 集成边界）。这是关闭外部 LLM 时保证输出可用的替身。
    """

    ROLE_CN = {
        "valuation_reviewer": "估值审查员",
        "industry_reviewer": "行业审查员",
        "risk_controller": "风控官",
        "quant_auditor": "量化审计员",
        "devil_advocate": "魔鬼代言人",
    }
    SCORE_KEY = {
        "valuation_reviewer": "valuation",
        "industry_reviewer": "industry",
        "risk_controller": "risk",
        "quant_auditor": "quant",
        "devil_advocate": "devil_advocate",
    }

    def __init__(self, version: str = "1.0"):
        self.version = version

    def generate_statements(
        self, scores: dict, role_genomes: dict, thesis: dict, verdict: str
    ) -> dict:
        statements = {}
        for role_key, cn in self.ROLE_CN.items():
            genome = (role_genomes.get(role_key, {}) or {}).get("genome", {}) or {}
            voice = genome.get("voice", "")
            s = scores.get(self.SCORE_KEY[role_key], 50.0)
            if s < 60:
                conclusion = "发现明显隐患，要求补充证据或下调仓位。"
            else:
                conclusion = "暂未发现致命问题，予以通过。"
            statements[role_key] = (
                f"[LLM-v{self.version}] 【{cn}】评分 {s:.0f}/100。"
                f"以“{voice}”的视角质询：{conclusion}"
            )
        return statements

    def generate_devil_attack(self, attacks: list, thesis: dict) -> str:
        if not attacks:
            return (
                f"[LLM-v{self.version}] 魔鬼代言人：未找到可量化的致命缺陷，核心假设存活概率较高。"
            )
        lines = [f"[LLM-v{self.version}] 魔鬼代言人核心攻击点："]
        for a in attacks:
            lines.append(f"  - {a}")
        return "\n".join(lines)

    def generate_transcript(
        self, statements: dict, devil_attack: str, verdict: str, verdict_reason: str
    ) -> str:
        verdict_cn = {
            "APPROVE": "✅ 批准",
            "APPROVE_WITH_CONDITIONS": "⚠️ 有条件批准",
            "REJECT": "❌ 否决",
            "RETURN_FOR_REVISION": "↩️ 退回修订",
        }.get(verdict, verdict)

        lines = ["=== 投资委员会辩论记录 ===", ""]
        for role_key, cn in self.ROLE_CN.items():
            stmt = statements.get(role_key, "")
            if stmt:
                lines.append(f"【{cn}】")
                lines.append(stmt.replace(f"[LLM-v{self.version}] ", ""))
                lines.append("")
        if devil_attack:
            lines.append("【魔鬼代言人·攻击摘要】")
            lines.append(devil_attack.replace(f"[LLM-v{self.version}] ", ""))
            lines.append("")
        lines.append(f"【主席裁决】 {verdict_cn} — {verdict_reason}")
        return "\n".join(lines)


def _pe_to_percentile(pe_ttm) -> float:
    """PE(TTM) → 历史分位的启发式映射（A 股经验刻度）。

    真实分位需全市场样本，此处仅用于缺失 pe_percentile 时的兜底。
    """
    try:
        pe = float(pe_ttm)
    except (TypeError, ValueError):
        return 0.5
    if pe <= 0:
        return 0.05  # 亏损股，估值异常低位
    if pe <= 10:
        return 0.15
    if pe <= 15:
        return 0.30
    if pe <= 25:
        return 0.50
    if pe <= 40:
        return 0.70
    if pe <= 60:
        return 0.85
    return 0.95


# Portfolio Integration
# ═══════════════════════════════════════════════════════════


def apply_committee_decision(decision: CommitteeDecision, base_weight: float) -> float:
    """Apply committee verdict to position weight.

    Committee can only REDUCE position and confidence.
    """
    if decision.verdict in ("REJECT", "RETURN_FOR_REVISION"):
        return 0.0

    adjusted = base_weight * decision.position_cap_modifier

    # confidence_modifier is always ≤ 0.0
    actual_mod = min(0.0, decision.confidence_modifier)
    adjusted *= 1.0 + actual_mod

    return max(0.0, adjusted)


def process_investment_idea(
    research_agent,
    thesis_validator,
    committee,
    portfolio_agent,
    market_snap,
    stock_snap,
    factor_snap,
    db,
    portfolio_state=None,
):
    """Full pipeline: Research → Validate → Committee → Portfolio.

    Complete data flow per Phase 5A-3 §10.

    Args:
        db: EvaluationDB handle. Required to persist the research decision so
            ThesisValidator.validate() can load it by id. (The previous
            signature passed an unused ``memory`` argument into analyze(),
            which only accepts (market, stock, factors) — that raised a
            TypeError on every call. It also called validate(0) because the
            SecurityAnalysis had no research_decision_id, which raised a
            ValueError inside the validator.)
    """
    # 1. Research
    sa = research_agent.analyze(market_snap, stock_snap, factor_snap)
    if not sa or getattr(sa, "alpha_score", 0) < 4:
        return None

    # Persist research decision to obtain its id for validation
    code = getattr(stock_snap, "code", "")
    mdate = getattr(market_snap, "date", "")
    dh = hashlib.sha256(f"{sa.agent_id}|{code}|{mdate}".encode()).hexdigest()[:16]
    ih = hashlib.sha256(f"{code}|{mdate}".encode()).hexdigest()[:16]
    rid = db.insert_research_decision(
        agent_id=sa.agent_id,
        genome_hash=dh,
        security_id=code,
        thesis={
            "thesis_id": f"auto_{code}_{mdate}",
            "family": sa.thesis.family if sa.thesis else "value",
            "pattern": sa.thesis.pattern if sa.thesis else "auto",
            "claim": sa.thesis.claim[:100] if sa.thesis else "",
            "evidence": sa.thesis.evidence if sa.thesis else [],
            "catalyst": "",
            "invalidation": sa.thesis.invalidation if sa.thesis else [],
            "horizon": "12_months",
        },
        alpha_score=sa.alpha_score,
        confidence=sa.confidence,
        factor_snapshot={
            "quality": factor_snap.quality_score,
            "value": factor_snap.value_score,
            "growth": factor_snap.growth_score,
            "momentum": factor_snap.momentum_score,
        },
        risk_assessment=sa.risk_assessment,
        entry_price=getattr(stock_snap, "price", None) or 100,
        entry_date=mdate,
        decision_hash=dh,
        input_hash=ih,
    )

    # 2. Validation
    validation = thesis_validator.validate(rid)
    if getattr(validation, "routing_action", None) == "BLOCK":
        return None

    # 3. Committee
    market_dict = {
        "regime_type": getattr(market_snap, "regime_type", "rotation"),
        "risk_score": float(getattr(market_snap, "risk_score", 50.0)),
        "market_pe_percentile": getattr(market_snap, "market_pe_percentile", 0.55),
    }
    decision = committee.review(
        sa,
        validation,
        market_dict,
        portfolio_state or {"sector_weights": {}, "positions": []},
    )

    # 4. Portfolio
    if decision.verdict in ("APPROVE", "APPROVE_WITH_CONDITIONS"):
        enhanced = {
            "agent_id": sa.agent_id,
            "stock_code": sa.stock_code,
            "alpha_score": sa.alpha_score,
            "confidence": sa.confidence,
            "effective_confidence": max(
                0.0, validation.effective_confidence + decision.confidence_modifier
            ),
            "thesis": sa.thesis,
            "factor_profile": sa.factor_profile,
            "risk_assessment": sa.risk_assessment,
            "monitoring_flags": decision.monitoring_flags,
            "position_cap_modifier": decision.position_cap_modifier,
        }
        # Portfolio agent constructs the portfolio from the real SecurityAnalysis
        portfolio_decision = portfolio_agent.construct_portfolio(
            [sa], PortfolioState(), market_dict
        )
        return enhanced, decision, portfolio_decision

    return None
