"""Full-system integration tests — pytest-discoverable function-style rewrite
of the legacy ``tests/integration_test.py`` script.

Every section of the original script is mapped to one or more ``test_*``
functions using plain ``assert``. Database-touching tests isolate state via
the ``db`` fixture (a fresh SQLite file under ``tmp_path``) so the real
``data/evaluation.db`` is never polluted. No section makes network/external
calls (mootdx / 东财), so nothing is skipped for connectivity reasons.
"""

import json
import os

import numpy as np
import pandas as pd
import pytest

# Project paths (resolved from this file's location, independent of CWD).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config", "personalities")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# The original script ensured the data dir exists; keep that for any component
# that resolves a relative "data/..." path against the project root.
os.makedirs(DATA_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db(tmp_path):
    """A fresh, fully-migrated EvaluationDB isolated under tmp_path."""
    from src.data.evaluation_db import EvaluationDB

    db_path = str(tmp_path / "integration_eval.db")
    d = EvaluationDB(db_path=db_path)
    d.init_db()
    # Idempotent v2.0 -> v2.1 migration (adds failure_patterns,
    # committee_decisions, candidate_rules, thesis_patterns, ...).
    d.migrate_v2_1()
    return d


@pytest.fixture
def composite_result():
    """Synthetic CompositeResult used by factor-engine and research-agent tests."""
    from src.factors.engine import FactorEngine

    np.random.seed(42)
    dates = pd.date_range("2026-01-01", periods=100, freq="B")
    prices = pd.DataFrame(
        {
            "date": dates,
            "close": 100 * (1 + np.random.normal(0.001, 0.02, 100)).cumprod(),
            "volume": np.random.uniform(1e6, 1e7, 100),
        }
    )
    financial = {
        "roe": 0.25,
        "roic": 0.18,
        "gross_margin": 0.65,
        "net_margin": 0.20,
        "pe_ttm": 18.5,
        "pb": 3.2,
        "fcf_yield": 0.05,
        "debt_to_equity": 0.8,
        "interest_coverage": 12.0,
        "revenue_growth_1y": 0.18,
        "earnings_growth_1y": 0.22,
    }
    fe = FactorEngine()
    return fe.compute_single_stock("600519", financial, prices)


# ═══════════════════════════════════════════════════════════════════════
# 1. Module imports — each import is a standalone test so failures surface
#    the exact broken module instead of being swallowed by a check() helper.
# ═══════════════════════════════════════════════════════════════════════


def test_import_evaluation_db():
    from src.data.evaluation_db import EvaluationDB

    assert EvaluationDB is not None


def test_import_data_provider():
    from src.data.provider import DataProvider, MarketSnapshot, StockSnapshot

    assert DataProvider is not None and MarketSnapshot is not None and StockSnapshot is not None


def test_import_market_brain():
    from src.data.market_brain import MarketBrain

    assert MarketBrain is not None


def test_import_factor_engine():
    from src.factors.engine import FactorEngine

    assert FactorEngine is not None


def test_import_evolution_engine():
    from src.evolution.genome import AgentGenome
    from src.evolution.spec_engine import (
        CrossoverEngine,
        EvolutionEngine,
        MutationEngine,
        SandboxValidator,
        SelectionEngine,
        SurvivalCriteria,
    )

    assert all(
        c is not None
        for c in (
            AgentGenome,
            EvolutionEngine,
            SelectionEngine,
            MutationEngine,
            CrossoverEngine,
            SandboxValidator,
            SurvivalCriteria,
        )
    )


def test_import_research_agent():
    from src.agents.research_agent import ResearchAgent, SecurityAnalysis

    assert ResearchAgent is not None and SecurityAnalysis is not None


def test_import_portfolio_agent():
    from src.agents.portfolio_agent import PortfolioAgent, PortfolioState, RiskPolicy

    assert PortfolioAgent is not None and PortfolioState is not None and RiskPolicy is not None


def test_import_committee_agent():
    from src.agents.committee_agent import CommitteeAgent, CommitteeDecision

    assert CommitteeAgent is not None and CommitteeDecision is not None


def test_import_post_mortem():
    from src.evaluation.post_mortem import ErrorCategory, ErrorSubtype, PostMortemAnalyzer

    assert PostMortemAnalyzer is not None and ErrorCategory is not None and ErrorSubtype is not None


def test_import_thesis_validator():
    from src.validation.thesis_validator import ThesisValidator

    assert ThesisValidator is not None


def test_import_rule_registry():
    from src.validation.rule_registry import RuleRegistry

    assert RuleRegistry is not None


def test_import_evidence_checker():
    from src.validation.evidence_checker import EvidenceChecker

    assert EvidenceChecker is not None


def test_import_complexity_checker():
    from src.validation.complexity_checker import ComplexityChecker

    assert ComplexityChecker is not None


def test_import_report_exporter():
    from src.utils.report_exporter import ReportExporter

    assert ReportExporter is not None


# ═══════════════════════════════════════════════════════════════════════
# 2. Database
# ═══════════════════════════════════════════════════════════════════════


def test_database_tables(db):
    conn = db.connect()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    conn.close()
    table_names = [r[0] for r in rows]

    expected = [
        "agent_genome_snapshots",
        "agent_performance",
        "calibration_log",
        "committee_decisions",
        "decision_events",
        "evaluation_results",
        "factor_memory",
        "failure_patterns",
        "market_regime_snapshots",
        "portfolio_decisions",
        "post_mortems",
        "research_decisions",
        "thesis_outcomes",
    ]
    assert len(table_names) >= 13, f"found {len(table_names)} tables, expected >= 13"
    for name in expected:
        assert name in table_names, f"missing table: {name}"


# ═══════════════════════════════════════════════════════════════════════
# 3. personality_score
# ═══════════════════════════════════════════════════════════════════════


def test_compute_personality_score():
    from src.data.evaluation_db import compute_personality_score

    ps = compute_personality_score(0.25, 1.5, -0.15, 0.85, 0.72)
    assert 0 < ps < 1.0, f"personality_score out of range: {ps} (return=0.25, sharpe=1.5, dd=-0.15)"


# ═══════════════════════════════════════════════════════════════════════
# 4. Factor Engine
# ═══════════════════════════════════════════════════════════════════════


def test_factor_engine_families():
    from src.factors.engine import FactorEngine

    fe = FactorEngine()
    assert len(fe.get_family_names()) == 6, (
        f"expected 6 factor families, got {fe.get_family_names()}"
    )
    assert len(fe.get_factor_names()) >= 25, (
        f"expected >= 25 factors, got {len(fe.get_factor_names())}"
    )


def test_factor_engine_compute_single_stock(composite_result):
    result = composite_result
    assert result.quality_score > 0, f"quality_score={result.quality_score}"
    assert result.value_score > 0, f"value_score={result.value_score}"
    assert result.growth_score > 0, f"growth_score={result.growth_score}"


# ═══════════════════════════════════════════════════════════════════════
# 5. MarketBrain
# ═══════════════════════════════════════════════════════════════════════


def test_market_brain_classify():
    from src.data.market_brain import MarketBrain

    np.random.seed(123)
    dates = pd.date_range("2026-01-01", periods=100, freq="B")
    idx_data = pd.DataFrame(
        {
            "date": dates,
            "close": 3500 * (1 + np.random.normal(0.0005, 0.015, 100)).cumprod(),
            "volume": np.random.uniform(1e10, 1e11, 100),
        }
    )
    mb = MarketBrain()
    regime = mb.classify(idx_data)
    assert regime.regime_type in ("bull", "bear", "crisis", "rotation", "unknown"), (
        f"regime_type={regime.regime_type}"
    )
    assert 0 <= regime.risk_score <= 100, f"risk_score={regime.risk_score}"


# ═══════════════════════════════════════════════════════════════════════
# 6. Research Agent
# ═══════════════════════════════════════════════════════════════════════


def test_research_agent_analyze(composite_result):
    from src.data.provider import MarketSnapshot, StockSnapshot
    from src.agents.research_agent import ResearchAgent

    genome_path = os.path.join(CONFIG_DIR, "value_purist.yaml")
    assert os.path.exists(genome_path), f"genome not found: {genome_path}"
    with open(genome_path, encoding="utf-8") as f:
        genome_yaml = f.read()

    agent = ResearchAgent(genome_yaml)
    market = MarketSnapshot(date="2026-07-14", regime_type="rotation")
    stock = StockSnapshot(
        code="600519", name="贵州茅台", price=1680.0, pe_ttm=30.0, pb=8.5, float_mcap=21000
    )
    sa = agent.analyze(market, stock, composite_result)

    assert agent.genome.agent_id == "value_purist_v1", f"agent_id={agent.genome.agent_id}"
    assert sa.alpha_score > 0, f"alpha_score={sa.alpha_score}"
    assert sa.confidence > 0, f"confidence={sa.confidence}"
    assert sa.thesis is not None, "missing thesis"
    if sa.thesis:
        assert len(sa.thesis.claim) > 0, "empty thesis claim"
        assert len(sa.thesis.invalidation) > 0, "empty invalidation"
    assert len(sa.decision_fingerprint) > 0, "empty decision_fingerprint"


# ═══════════════════════════════════════════════════════════════════════
# 7. Portfolio Agent
# ═══════════════════════════════════════════════════════════════════════


def test_portfolio_agent_construct(composite_result):
    from src.data.provider import MarketSnapshot, StockSnapshot
    from src.agents.research_agent import ResearchAgent
    from src.agents.portfolio_agent import PortfolioAgent, PortfolioState

    with open(os.path.join(CONFIG_DIR, "value_purist.yaml"), encoding="utf-8") as f:
        genome_yaml = f.read()
    agent = ResearchAgent(genome_yaml)
    market = MarketSnapshot(date="2026-07-14", regime_type="rotation")
    stock = StockSnapshot(
        code="600519", name="贵州茅台", price=1680.0, pe_ttm=30.0, pb=8.5, float_mcap=21000
    )
    sa = agent.analyze(market, stock, composite_result)

    pa = PortfolioAgent()
    state = PortfolioState()
    market_dict = {"regime_type": "rotation", "risk_score": 45, "market_pe_percentile": 0.65}
    pdec = pa.construct_portfolio([sa], state, market_dict)

    assert len(pdec.decisions) > 0, "no portfolio decisions"
    if pdec.decisions:
        d = pdec.decisions[0]
        assert d.action in ("BUY", "HOLD"), f"action={d.action}"
        assert d.target_weight > 0, f"target_weight={d.target_weight}"
        assert len(d.position_engine_trace) >= 5, "position_engine_trace too short"


# ═══════════════════════════════════════════════════════════════════════
# 8. Thesis Validator sub-components
# ═══════════════════════════════════════════════════════════════════════


def test_evidence_checker():
    from src.validation.evidence_checker import EvidenceChecker

    ev_checker = EvidenceChecker()
    ev_score, ev_fails = ev_checker.check(
        [
            {"metric": "roe", "value": 0.25, "condition": ">0.20"},
            {"metric": "pe_ttm", "value": 35, "condition": "<30"},
        ],
        {"roe": 0.25, "pe_ttm": 35},
    )
    assert ev_score == 50.0, f"evidence_score={ev_score}"
    assert len(ev_fails) == 1, f"failures={len(ev_fails)}"


def test_complexity_checker():
    from src.validation.complexity_checker import ComplexityChecker

    comp_checker = ComplexityChecker()
    penalty, _ = comp_checker.check("简单品牌护城河", [])
    assert penalty <= 1.0, f"simple_penalty={penalty}"


def test_rule_registry(db):
    from src.validation.rule_registry import RuleRegistry

    registry = RuleRegistry(db)
    rules = registry.get_validated_rules()
    assert len(rules) >= 3, f"static_rules={len(rules)}"


# ═══════════════════════════════════════════════════════════════════════
# 9. Post-Mortem
# ═══════════════════════════════════════════════════════════════════════


def _seed_post_mortem_data(db):
    """Insert a failing research decision + evaluation result; return eval id."""
    conn = db.connect()
    conn.execute("DELETE FROM evaluation_results")
    conn.execute("DELETE FROM research_decisions")
    conn.execute(
        """INSERT INTO research_decisions
        (agent_id, genome_hash, security_id, thesis_id, thesis_family,
         thesis_pattern, thesis_claim, thesis_evidence, thesis_invalidation,
         thesis_catalyst, alpha_score, confidence, factor_snapshot, risk_assessment,
         decision_hash, input_hash, entry_price, entry_date)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """,
        (
            "value_purist_v1",
            "abc",
            "600519",
            "t1",
            "value",
            "quality_compound",
            "品牌护城河创造超额回报",
            json.dumps([{"metric": "roe", "value": 0.31, "condition": ">0.20"}]),
            json.dumps([{"condition": "roe < 0.20"}]),
            "消费复苏",
            7.5,
            8.0,
            json.dumps({"roe": 0.31, "gross_margin": 0.92}),
            json.dumps({"idiosyncratic_risk": "low", "expected_drawdown_12m": 0.18}),
            "hash2",
            "input2",
            1680.0,
            "2026-07-14",
        ),
    )
    rd_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO evaluation_results
        (research_decision_id, horizon_days, eval_date, stock_return, market_return,
         sector_return, agent_top10_ew_return, max_drawdown_during, max_profit_during, verdict)
        VALUES (?, 30, '2026-08-14', -0.12, 0.05, 0.10, 0.03, -0.18, 0.08, ?)
    """,
        (rd_id, "market_alpha_negative"),
    )
    eval_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return eval_id


def test_post_mortem_run(db):
    from src.evaluation.post_mortem import PostMortemAnalyzer

    eval_id = _seed_post_mortem_data(db)
    pme = PostMortemAnalyzer(db)
    pm_result = pme.run(eval_id)
    assert pm_result.error_category is not None, "error_category is None"
    assert len(pm_result.error_subtype.value) > 0, (
        f"type={pm_result.error_category.value}/{pm_result.error_subtype.value}"
    )
    assert len(pm_result.mutation_candidates) > 0, "no mutation_candidates"
    assert len(pm_result.lessons.get("lesson", "")) > 0, "no lesson"


# ═══════════════════════════════════════════════════════════════════════
# 10. Committee Agent
# ═══════════════════════════════════════════════════════════════════════


def test_committee_review(db):
    from src.agents.committee_agent import CommitteeAgent

    class TThesis:
        def __init__(self):
            self.claim = "品牌护城河创造超额回报"
            self.evidence = [{"metric": "roe", "value": 0.31, "condition": ">0.20"}]
            self.invalidation = [{"condition": "roe < 0.20"}]
            self.family = "value"
            self.pattern = "quality_compound"
            self.thesis_id = "t1"

    class TSA:
        agent_id = "value_purist_v1"
        stock_code = "600519"
        alpha_score = 7.5
        confidence = 8.0
        factor_profile = {
            "roe": 0.31,
            "gross_margin": 0.92,
            "pe_percentile": 0.65,
            "fcf_yield": 0.04,
        }
        risk_assessment = {
            "idiosyncratic_risk": "low",
            "sector": "消费",
            "expected_drawdown_12m": 0.18,
        }
        thesis = TThesis()

    class TVal:
        research_decision_id = 1
        verdict = "PASS"
        effective_confidence = 7.5
        counter_warnings = []

    committee = CommitteeAgent(db)
    decision = committee.review(
        TSA(),
        TVal(),
        {"sector_momentum": {"消费": 0.08}},
        {"sector_weights": {"消费": 0.15}},
    )

    assert decision.verdict in ("APPROVE", "APPROVE_WITH_CONDITIONS", "REJECT"), (
        f"verdict={decision.verdict}"
    )
    assert 0 <= decision.weighted_score <= 100, f"weighted={decision.weighted_score}"
    assert 0 <= decision.valuation_score <= 100, f"val={decision.valuation_score}"
    assert 0 <= decision.industry_score <= 100, f"ind={decision.industry_score}"
    assert 0 <= decision.risk_score <= 100, f"risk={decision.risk_score}"
    assert 0 <= decision.devil_advocate_score <= 100, f"da={decision.devil_advocate_score}"
    assert len(decision.devil_advocate_attack_points) > 0, "no attack_points"


# ═══════════════════════════════════════════════════════════════════════
# 11. Committee -> Portfolio integration
# ═══════════════════════════════════════════════════════════════════════


def test_apply_committee_decision():
    from src.agents.committee_agent import CommitteeDecision, apply_committee_decision

    approve_d = CommitteeDecision(
        committee_id="c1",
        research_decision_id=1,
        verdict="APPROVE",
        position_cap_modifier=1.0,
        confidence_modifier=0.0,
    )
    final_w = apply_committee_decision(approve_d, 0.10)
    assert final_w <= 0.10, f"weight 0.10 -> {final_w}"

    reject_d = CommitteeDecision(committee_id="x", research_decision_id=1, verdict="REJECT")
    reject_w = apply_committee_decision(reject_d, 0.10)
    assert reject_w == 0.0, f"reject -> {reject_w}"


# ═══════════════════════════════════════════════════════════════════════
# 12. Chairman verdict rules
# ═══════════════════════════════════════════════════════════════════════


def test_chairman_decision():
    from src.agents.committee_agent import chairman_decision

    v1, ws1, _ = chairman_decision(
        {"valuation": 80, "industry": 75, "risk": 85, "quant": 70, "devil_advocate": 75}, 0
    )
    assert v1 == "APPROVE", f"high -> {v1} (ws={ws1})"

    v2, ws2, _ = chairman_decision(
        {"valuation": 55, "industry": 60, "risk": 65, "quant": 50, "devil_advocate": 45}, 0
    )
    assert v2 == "APPROVE_WITH_CONDITIONS", f"med -> {v2} (ws={ws2})"

    v3, ws3, _ = chairman_decision(
        {"valuation": 60, "industry": 40, "risk": 25, "quant": 50, "devil_advocate": 55}, 0
    )
    assert v3 == "REJECT", f"low_risk -> {v3} (ws={ws3})"


# ═══════════════════════════════════════════════════════════════════════
# 13. Evolution Engine
# ═══════════════════════════════════════════════════════════════════════


def test_evolution_engine_init(db):
    from src.evolution.spec_engine import EvolutionEngine

    ee = EvolutionEngine(db)
    assert ee is not None, "engine init"
    assert ee.selection is not None, "selection"
    assert ee.mutation is not None, "mutation"
    assert ee.crossover is not None, "crossover"
    assert ee.sandbox is not None, "sandbox"


def test_evolution_crossover(db):
    from src.agents.research_agent import ResearchAgent
    from src.evolution.spec_engine import EvolutionEngine

    with open(os.path.join(CONFIG_DIR, "value_purist.yaml"), encoding="utf-8") as f:
        ga_yaml = f.read()
    with open(os.path.join(CONFIG_DIR, "growth_hunter.yaml"), encoding="utf-8") as f:
        gb_yaml = f.read()

    ee = EvolutionEngine(db)
    ga = ResearchAgent(ga_yaml).genome
    gb = ResearchAgent(gb_yaml).genome
    child = ee.crossover.crossover(ga, gb)

    assert child.generation > ga.generation, f"child={child.agent_id[:20]}..."
    assert len(child.identity_vector) == 8, f"identity_vector len={len(child.identity_vector)}"
    assert len(child.factor_weights) > 0, "empty factor_weights"
    dist_ab = ga.identity_distance(gb)
    assert dist_ab > 0.5, f"parent_dist={dist_ab} (max possible ~2.83)"


# ═══════════════════════════════════════════════════════════════════════
# 14. Report Exporter
# ═══════════════════════════════════════════════════════════════════════


def test_report_exporter_committee_md(db):
    from src.utils.report_exporter import ReportExporter

    exporter = ReportExporter(db)
    path = exporter.export_committee_report(format="md")
    assert isinstance(path, str) and len(path) > 0, f"got: {str(path)[:100]}"


# ═══════════════════════════════════════════════════════════════════════
# 15. CLI verification
# ═══════════════════════════════════════════════════════════════════════


def test_cli_functions_exist():
    import src.cli as cli_module

    assert hasattr(cli_module, "cmd_init"), "missing cmd_init"
    assert hasattr(cli_module, "cmd_factor"), "missing cmd_factor"
    assert hasattr(cli_module, "cmd_fuse"), "missing cmd_fuse"
    assert hasattr(cli_module, "cmd_export"), "missing cmd_export"
    assert hasattr(cli_module, "cmd_status"), "missing cmd_status"


def test_cli_status_output(monkeypatch, tmp_path, capsys):
    """cmd_status uses a relative ``data/evaluation.db`` path, so chdir into a
    tmp dir to avoid touching the project's real evaluation database."""
    import src.cli as cli_module
    from src.data.evaluation_db import EvaluationDB

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    db = EvaluationDB("data/evaluation.db")
    db.init_db()
    db.migrate_v2_1()
    cli_module.cmd_status(None)
    captured = capsys.readouterr()
    assert "活跃 Agent" in captured.out, "status output missing '活跃 Agent'"
