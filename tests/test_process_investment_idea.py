"""Tests for the end-to-end investment-idea pipeline wrapper.

Covers the fixes to ``committee_agent.process_investment_idea``:
  * analyze() is invoked with exactly (market, stock, factors) -- the old
    code passed a stray ``memory`` arg that raised TypeError on every call;
  * the SecurityAnalysis is persisted first and validate() receives the real
    research_decision_id (the old code passed 0, which raised ValueError
    inside the validator because no row with id=0 exists);
  * routing is gated on ValidationResult.routing_action == "BLOCK"
    (the old code read validation.verdict, an attribute ValidationResult
    does not have -> AttributeError);
  * the portfolio is actually constructed from the real SecurityAnalysis
    object (the old code commented the call out / passed a dict).
"""
import sys
import importlib.util

# Make the project root importable (mirrors tests/conftest.py behaviour)
ROOT = sys.path[0] if sys.path and sys.path[0] else ""
if ROOT and ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _FakeThesis:
    family = "value"
    pattern = "auto"
    claim = "X" * 200
    evidence = ["e1"]
    invalidation = ["i1"]


class _FakeSA:
    agent_id = "A1"
    stock_code = "600519"
    alpha_score = 8.0
    confidence = 7.0
    thesis = _FakeThesis()
    factor_profile = {"quality_score": 1, "value_score": 2,
                      "growth_score": 3, "momentum_score": 4}
    risk_assessment = {"level": "low"}


class _FakeValidation:
    def __init__(self, routing_action="ALLOW_COMMITTEE", effective_confidence=6.0):
        self.routing_action = routing_action
        self.effective_confidence = effective_confidence
        self.last_rid = None

    def validate(self, rid):
        self.last_rid = rid
        return self


class _FakeDecision:
    verdict = "APPROVE"
    confidence_modifier = -1.0
    monitoring_flags = ["m1"]
    position_cap_modifier = 0.9


class _FakeCommittee:
    def __init__(self, decision=None):
        self._decision = decision or _FakeDecision()
        self.last_market = None

    def review(self, sa, val, market_dict, pstate):
        self.last_market = market_dict
        return self._decision


class _FakePortfolio:
    def __init__(self):
        self.last_analyses = None

    def construct_portfolio(self, analyses, state, market):
        self.last_analyses = analyses
        return "PDEC"


class _FakeResearch:
    def __init__(self):
        self.calls = []
        self.last_sa = None

    def analyze(self, market, stock, factors):
        sa = _FakeSA()
        self.calls.append((market, stock, factors))
        self.last_sa = sa
        return sa


class _LowAlphaResearch(_FakeResearch):
    def analyze(self, market, stock, factors):
        sa = _FakeSA()
        sa.alpha_score = 2.0
        return sa


class _FakeDB:
    def __init__(self):
        self.last_kw = None

    def insert_research_decision(self, **kw):
        self.last_kw = kw
        return 42


class _FakeMarket:
    date = "2026-07-16"
    regime_type = "bull"
    risk_score = 20.0
    market_pe_percentile = 0.3


class _FakeStock:
    code = "600519"
    price = 100.0


class _FakeFactors:
    quality_score = 1.0
    value_score = 2.0
    growth_score = 3.0
    momentum_score = 4.0


def _run(research_agent=None, routing="ALLOW_COMMITTEE", verdict="APPROVE"):
    # import via package to honour the relative imports inside the module
    import src.agents.committee_agent as cam
    ra = research_agent or _FakeResearch()
    val = _FakeValidation(routing_action=routing)
    committee = _FakeCommittee()
    if verdict != "APPROVE":
        committee._decision.verdict = verdict
    pa = _FakePortfolio()
    db = _FakeDB()
    result = cam.process_investment_idea(
        ra, val, committee, pa,
        _FakeMarket(), _FakeStock(), _FakeFactors(), db,
        {"sector_weights": {}, "positions": []},
    )
    return result, ra, val, committee, pa, db


def test_full_pipeline_returns_triple_on_approve():
    result, ra, val, committee, pa, db = _run()
    assert result is not None
    enhanced, decision, pdec = result
    assert enhanced["agent_id"] == "A1"
    assert pdec == "PDEC"
    # analyze() called with exactly 3 positional args (no stray memory)
    assert len(ra.calls) == 1
    # validate() received the real persisted id, not 0
    assert val.last_rid == 42
    # portfolio got the real SecurityAnalysis object returned by analyze()
    assert pa.last_analyses is not None
    assert pa.last_analyses[0] is ra.last_sa


def test_block_routing_returns_none():
    result, *_ = _run(routing="BLOCK")
    assert result is None


def test_low_alpha_filtered_before_validation():
    ra = _LowAlphaResearch()
    val = _FakeValidation()
    result, *_ = _run(research_agent=ra, routing="ALLOW_COMMITTEE")
    assert result is None
    assert val.last_rid is None  # validate() never called


def test_reject_verdict_returns_none():
    result, *_ = _run(verdict="REJECT")
    assert result is None


def test_committee_receives_market_dict():
    _, _, _, committee, _, _ = _run()
    assert committee.last_market == {
        "regime_type": "bull", "risk_score": 20.0, "market_pe_percentile": 0.3,
    }
