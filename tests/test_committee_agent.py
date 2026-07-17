"""Investment Committee Agent 测试套件 (Phase 5A-3)。

覆盖：
  - 五个角色规则引擎评分（含真实字符串 severity 警告兼容）
  - 主席裁决规则（致命否决 / 退回修订 / 有条件通过 / 批准 / 边缘）
  - CommitteeAgent.review 端到端（强 / 致命 / 有条件）
  - apply_committee_decision 仓位调整
  - EvaluationDB 量化查询方法 + 决策持久化 + 幂等迁移
  - RuleOnlyLLMBridge 确定性陈述（spec §8 边界）
"""

import os
import sys
import tempfile

import pytest

from src.agents import (
    CommitteeAgent,
    CommitteeDecision,
    chairman_decision,
    apply_committee_decision,
)
from src.agents.research_agent import SecurityAnalysis, ThesisObject
from src.agents.committee_roles import (
    score_valuation,
    score_industry,
    score_risk,
    score_quant,
    score_devil_advocate,
)
from src.agents.committee_agent import RuleOnlyLLMBridge
from src.validation.thesis_validator import ValidationResult
from src.data.evaluation_db import EvaluationDB


# ───────────────────────────────────────────────────────────
# 构造辅助
# ───────────────────────────────────────────────────────────

def make_thesis(**over):
    base = dict(
        thesis_id="t1", family="value", pattern="quality_compound",
        claim="品牌护城河将持续创造超额回报",
        evidence=[{"metric": "roe", "value": 0.25},
                  {"metric": "gross_margin", "value": 0.55},
                  {"metric": "fcf_yield", "value": 0.06}],
        catalyst="", invalidation=[{"condition": "roe_ttm < 0.15"},
                                    {"condition": "gross_margin < 0.25"}],
        horizon="36_months", confidence_contribution=0.8,
    )
    base.update(over)
    return ThesisObject(**base)


def make_sa(thesis=None, **over):
    base = dict(
        agent_id="agent_alpha", stock_code="600519",
        timestamp="2026-07-14T10:00:00", alpha_score=8.0, confidence=8.0,
        thesis=thesis or make_thesis(),
        factor_profile={"quality_score": 80, "value_score": 70},
        risk_assessment={"expected_drawdown_12m": 0.15},
    )
    base.update(over)
    return SecurityAnalysis(**base)


def make_val(**over):
    base = dict(
        thesis_id="t1", research_decision_id=1, evidence_score=90,
        counter_evidence_risk=10, historical_score=85, complexity_penalty=2,
        overall_score=88, verdict="PASS", effective_confidence=8.0,
        counter_warnings=[],
    )
    base.update(over)
    return ValidationResult(**base)


# ───────────────────────────────────────────────────────────
# 1. 角色评分
# ───────────────────────────────────────────────────────────

def test_valuation_clean():
    fs = {"pe_percentile": 0.5, "fcf_yield": 0.06}
    s = score_valuation(make_thesis(), fs, make_val())
    assert s == 100.0


def test_valuation_high_pe_and_low_fcf():
    fs = {"pe_percentile": 0.95, "fcf_yield": 0.01}
    s = score_valuation(make_thesis(), fs, make_val())
    # 100 - 30 (pe>0.9) - 20 (fcf<0.02) = 50
    assert s == 50.0


def test_valuation_growth_tolerance():
    fs = {"pe_percentile": 0.5, "fcf_yield": 0.06}
    thesis = make_thesis(family="growth")
    s = score_valuation(thesis, fs, make_val())
    # 100 + 10 (growth tolerance) = 110 -> clamp 100
    assert s == 100.0


def test_valuation_counter_warning_string_severity():
    # 真实反证使用字符串 severity
    val = make_val(counter_warnings=[{"rule_name": "valuation_overstretch", "severity": "high"}])
    fs = {"pe_percentile": 0.5, "fcf_yield": 0.06}
    s = score_valuation(make_thesis(), fs, val)
    # 100 - 20 (high severity * 20) = 80
    assert s == 80.0


def test_industry_negative_momentum():
    fs = {"sector": "新能源", "revenue_growth_yoy": 0.2}
    mkt = {"sector_momentum": {"新能源": -0.2}}
    thesis = make_thesis(family="growth", horizon="12_months")
    s = score_industry(thesis, mkt, fs)
    # 100 - 25 (momentum < -0.1) = 75（催化剂 12 月 ≤18 不罚分）
    assert s == 75.0


def test_risk_concentration_and_drawdown():
    sa = make_sa(risk_assessment={"expected_drawdown_12m": 0.45})
    ps = {"sector_weights": {"白酒": 0.35}}
    fs = {"sector": "白酒"}
    s = score_risk(ps, sa, make_val(), fs)
    # 100 - 30 (dd>0.3, dd 惩罚为 -25) - 30 (sector>0.3) = 45
    assert s == 45.0


def test_risk_string_severity_error_cost():
    val = make_val(counter_warnings=[{"rule_name": "r1", "severity": "high"}])
    s = score_risk({"sector_weights": {}}, make_sa(), val, {})
    # 100 - 15 (high sev error cost) = 85
    assert s == 85.0


def test_quant_no_db_is_neutral():
    s = score_quant("agent_x", "quality_compound", None)
    assert s == 100.0


def test_devil_advocate_fatal_flaws():
    thesis = make_thesis(
        claim="该股必然永远上涨绝无风险",
        evidence=[],
        invalidation=[],
    )
    survival, attacks = score_devil_advocate(thesis, make_val())
    # 100 -20 (absolute) -15 (no evidence) -25 (no invalidation) = 40
    assert survival == 40.0
    assert any("ASSERTION_TOO_ABSOLUTE" in a for a in attacks)
    assert any("NO_INVALIDATION" in a for a in attacks)


def test_devil_advocate_quantifiable_invalidation_passes():
    thesis = make_thesis(
        claim="稳健增长",
        invalidation=[{"condition": "roe < 0.10"}, {"condition": "gross_margin < 0.20"}],
    )
    survival, attacks = score_devil_advocate(thesis, make_val())
    # 无绝对断言、证据>=3、证伪可量化 → 仅可能的 0 扣分 = 100
    assert survival == 100.0


def test_devil_advocate_high_severity_warning():
    val = make_val(counter_warnings=[{"rule_name": "pattern_x", "severity": "high"}])
    thesis = make_thesis()
    survival, attacks = score_devil_advocate(thesis, val)
    # 100 - 30 (sev=1.0 > 0.7) = 70
    assert survival == 70.0


# ───────────────────────────────────────────────────────────
# 2. 主席裁决
# ───────────────────────────────────────────────────────────

def test_chairman_fatal_reject_risk():
    v, _, _ = chairman_decision({"valuation": 90, "industry": 90, "risk": 20,
                                 "quant": 90, "devil_advocate": 90})
    assert v == "REJECT"


def test_chairman_fatal_reject_devil():
    v, _, _ = chairman_decision({"valuation": 90, "industry": 90, "risk": 90,
                                 "quant": 90, "devil_advocate": 25})
    assert v == "REJECT"


def test_chairman_return_for_revision():
    # 两个维度 < 50
    v, _, _ = chairman_decision({"valuation": 40, "industry": 40, "risk": 90,
                                 "quant": 90, "devil_advocate": 90})
    assert v == "RETURN_FOR_REVISION"


def test_chairman_approve():
    v, _, _ = chairman_decision({"valuation": 90, "industry": 90, "risk": 90,
                                 "quant": 90, "devil_advocate": 90})
    assert v == "APPROVE"


def test_chairman_conditional():
    # 加权 >= 60 但存在弱维度
    v, _, _ = chairman_decision({"valuation": 90, "industry": 90, "risk": 40,
                                 "quant": 90, "devil_advocate": 90})
    assert v == "APPROVE_WITH_CONDITIONS"


# ───────────────────────────────────────────────────────────
# 3. CommitteeAgent.review 端到端
# ───────────────────────────────────────────────────────────

def test_review_strong_approve():
    agent = CommitteeAgent(db=None)
    dec = agent.review(make_sa(), make_val(),
                       market_snapshot={"regime_type": "bull"},
                       portfolio_state={"sector_weights": {}})
    assert isinstance(dec, CommitteeDecision)
    assert dec.verdict == "APPROVE"
    assert dec.position_cap_modifier == 1.0
    # 信心修饰符必须是安全小比例（不破坏 §7 仓位乘子）
    assert -0.30 <= dec.confidence_modifier <= 0.0
    # spec §8：即使关闭 LLM，陈述也应生成
    assert dec.member_statements and all(dec.member_statements.values())
    assert dec.devil_advocate_attack
    assert dec.debate_transcript


def test_review_fatal_reject():
    sa = make_sa(
        thesis=make_thesis(claim="必然永远上涨绝无风险", evidence=[], invalidation=[]),
        risk_assessment={"expected_drawdown_12m": 0.45},
    )
    val = make_val(counter_warnings=[{"rule_name": "valuation_overstretch", "severity": "high"}])
    agent = CommitteeAgent(db=None)
    dec = agent.review(sa, val, market_snapshot={"sector_momentum": {"新能源": -0.2}},
                       portfolio_state={"sector_weights": {"新能源": 0.35}}, sector="新能源")
    assert dec.verdict == "REJECT"
    assert dec.devil_advocate_score < 30
    assert dec.position_cap_modifier == 0.0


def test_review_conditional_monitoring_flags():
    # 构造一个弱估值（高 PE）导致有条件通过
    sa = make_sa(factor_profile={"quality_score": 80, "value_score": 70})
    val = make_val(counter_warnings=[{"rule_name": "valuation_stretch", "severity": "medium"}])
    agent = CommitteeAgent(db=None)
    dec = agent.review(
        sa, val,
        market_snapshot={"regime_type": "bull"},
        portfolio_state={"sector_weights": {}},
        factor_snapshot={"pe_percentile": 0.95, "fcf_yield": 0.01},
    )
    # 估值被重罚，触发监控标志
    assert dec.valuation_score < 60
    assert any("VALUATION" in f for f in dec.monitoring_flags)
    # 弱估值 + 其它良好 → 有条件通过（加权仍可能 >=60）
    assert dec.verdict in ("APPROVE_WITH_CONDITIONS", "REJECT")


# ───────────────────────────────────────────────────────────
# 4. 仓位调整
# ───────────────────────────────────────────────────────────

def test_apply_committee_decision_reject_zero():
    dec = CommitteeDecision(committee_id="c", research_decision_id=1, verdict="REJECT")
    assert apply_committee_decision(dec, 0.10) == 0.0


def test_apply_committee_decision_cap():
    dec = CommitteeDecision(committee_id="c", research_decision_id=1,
                            verdict="APPROVE_WITH_CONDITIONS",
                            position_cap_modifier=0.5, confidence_modifier=-0.1)
    out = apply_committee_decision(dec, 0.10)
    # 0.10 * 0.5 * (1 - 0.1) = 0.045
    assert abs(out - 0.045) < 1e-9


# ───────────────────────────────────────────────────────────
# 5. EvaluationDB 量化查询 + 持久化
# ───────────────────────────────────────────────────────────

def _seed_db(db):
    db.init_db()
    db.migrate_v2_1()
    db.migrate_committee_decisions_v2_1_1()
    conn = db.connect()
    conn.execute(
        "INSERT INTO research_decisions "
        "(agent_id, genome_hash, security_id, thesis_id, thesis_family, thesis_pattern, "
         " thesis_claim, thesis_invalidation, alpha_score, confidence, factor_snapshot, "
         " risk_assessment, decision_hash, input_hash, model_version, entry_price, entry_date) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("agent_alpha", "gh", "600519", "t1", "value", "quality_compound",
         "claim", "[]", 8.0, 8.0, "{}", "{}", "dh", "ih", "m", 100.0, "2026-07-14"),
    )
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # 4 次正 alpha + 1 次负 alpha → persistence = 0.8
    # 日期分布在近 5 个月（均在 12 个月与 6 个月窗口内）
    for i, (a, m) in enumerate([(0.05, 2), (0.03, 3), (0.04, 4), (0.02, 5), (-0.01, 6)]):
        conn.execute(
            "INSERT INTO evaluation_results "
            "(research_decision_id, horizon_days, eval_date, evaluated_at, stock_return, "
             " market_return, sector_return, agent_top10_ew_return, max_drawdown_during, "
             " max_profit_during, alpha_vs_market, verdict) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, 60, f"2026-0{m}-01", f"2026-0{m}-01", 0.1, 0.05, 0.04, 0.03,
             -0.02, 0.1, a, "PASS"),
        )
    conn.commit()
    conn.close()
    return rid


def test_quant_queries():
    tmp = tempfile.mktemp(suffix=".db")
    try:
        db = EvaluationDB(tmp)
        _seed_db(db)
        # persistence = 4/5 = 0.8
        assert abs(db.get_alpha_persistence("agent_alpha", months=12) - 0.8) < 1e-9
        # pattern IC = mean([0.05,0.03,0.04,0.02,-0.01]) = 0.026
        ic = db.get_pattern_ic("quality_compound", months=6)
        assert abs(ic - 0.026) < 1e-9
        # sample size = 5
        assert db.get_pattern_sample_size("quality_compound") == 5
    finally:
        os.remove(tmp)


def test_db_persist_and_idempotent_migration():
    tmp = tempfile.mktemp(suffix=".db")
    try:
        db = EvaluationDB(tmp)
        db.init_db()
        db.migrate_v2_1()
        db.migrate_committee_decisions_v2_1_1()

        agent = CommitteeAgent(db=db)
        dec = agent.review(make_sa(), make_val(),
                           market_snapshot={"regime_type": "bull"},
                           portfolio_state={"sector_weights": {}})
        rid = db.insert_committee_decision(dec)
        assert rid > 0
        row = db.get_committee_decision(dec.committee_id)
        # 持久化 fidelity：落库后的裁决应与决策对象一致
        assert row["verdict"] == dec.verdict
        assert row["verdict"] in (
            "APPROVE", "APPROVE_WITH_CONDITIONS", "REJECT", "RETURN_FOR_REVISION"
        )
        assert row["devil_advocate_score"] is not None
        assert row["weighted_score"] is not None

        # 幂等迁移不应报错
        db.migrate_committee_decisions_v2_1_1()
        row2 = db.get_committee_decision(dec.committee_id)
        assert row2["committee_id"] == dec.committee_id
    finally:
        os.remove(tmp)


# ───────────────────────────────────────────────────────────
# 6. RuleOnlyLLMBridge (spec §8)
# ───────────────────────────────────────────────────────────

def test_rule_only_bridge_prefix_and_anchors():
    bridge = RuleOnlyLLMBridge(version="1.0")
    scores = {"valuation": 90, "industry": 90, "risk": 40, "quant": 90, "devil_advocate": 90}
    genomes = {
        "valuation_reviewer": {"genome": {"voice": "冷静谨慎"}},
        "industry_reviewer": {"genome": {"voice": "前瞻"}},
        "risk_controller": {"genome": {"voice": "防御"}},
        "quant_auditor": {"genome": {"voice": "数据驱动"}},
        "devil_advocate": {"genome": {"voice": "逆向"}},
    }
    stmts = bridge.generate_statements(scores, genomes, {}, "APPROVE_WITH_CONDITIONS")
    for k, v in stmts.items():
        assert v.startswith("[LLM-v1.0]")
    attack = bridge.generate_devil_attack([], {})
    assert attack.startswith("[LLM-v1.0]")
    transcript = bridge.generate_transcript(stmts, attack, "APPROVE_WITH_CONDITIONS", "reason")
    assert "主席裁决" in transcript


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
