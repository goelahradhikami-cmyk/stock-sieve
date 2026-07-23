"""Tests for Commit 4: Post-Mortem Engine v1.0."""

from src.postmortem.classifier import FailureClassifier
from src.postmortem.engine import PostMortemEngine


def test_classifier_stock_failure():
    """Test stock selection failure detection."""
    classifier = FailureClassifier()
    eval_data = {
        "alpha_vs_market": -0.2,
        "alpha_vs_sector": -0.15,
        "stock_return": -0.18,
        "max_drawdown_during": 0,
        "net_return": -0.18,
    }
    attr_data = {"selection_alpha": -0.12, "execution_cost": 0.01}
    failures = classifier.classify(eval_data, attr_data)
    types = [f["type"] for f in failures]
    assert "stock_selection_failure" in types


def test_classifier_market_failure():
    """Test market regime failure detection."""
    classifier = FailureClassifier()
    eval_data = {
        "alpha_vs_market": -0.20,
        "stock_return": -0.15,
        "alpha_vs_sector": 0.0,
        "max_drawdown_during": 0,
        "net_return": -0.15,
    }
    failures = classifier.classify(eval_data, {})
    types = [f["type"] for f in failures]
    assert "market_regime_failure" in types


def test_classifier_timing_early():
    """Test early entry detection."""
    classifier = FailureClassifier()
    eval_data = {
        "alpha_vs_market": 0.0,
        "stock_return": 0.05,
        "max_drawdown_during": -0.25,
        "net_return": 0.08,
    }
    failures = classifier.classify(eval_data, {})
    types = [f["type"] for f in failures]
    assert "timing_failure_early" in types


def test_classifier_timing_late_exit():
    """Test late exit detection."""
    classifier = FailureClassifier()
    eval_data = {
        "alpha_vs_market": 0.0,
        "stock_return": 0.0,
        "evaluation_type": "EXIT",
        "exit_opportunity_cost": 0.20,
        "max_drawdown_during": 0,
        "net_return": 0,
    }
    failures = classifier.classify(eval_data, {})
    types = [f["type"] for f in failures]
    assert "timing_failure_late_exit" in types


def test_classifier_no_failure():
    """Test that profitable trades generate no failures."""
    classifier = FailureClassifier()
    eval_data = {
        "alpha_vs_market": 0.10,
        "alpha_vs_sector": 0.05,
        "stock_return": 0.15,
        "max_drawdown_during": -0.05,
        "net_return": 0.15,
    }
    attr_data = {"selection_alpha": 0.08, "execution_cost": 0.005}
    failures = classifier.classify(eval_data, attr_data)
    assert len(failures) == 0


def test_engine_generate_rule():
    """Test rule generation from failures."""
    engine = PostMortemEngine()

    # Stock selection failure → risk control rule
    rule = engine._generate_rule({"id": 1}, {"type": "stock_selection_failure"})
    assert rule is not None
    assert rule["rule_type"] == "risk_control"
    assert "pe_percentile" in rule["condition_json"]

    # Timing failure → thesis filter
    rule = engine._generate_rule({"id": 2}, {"type": "timing_failure_early"})
    assert rule is not None
    assert rule["rule_type"] == "thesis_filter"

    # Market regime → risk control
    rule = engine._generate_rule({"id": 3}, {"type": "market_regime_failure"})
    assert rule is not None
    assert rule["rule_type"] == "risk_control"


def test_engine_lifecycle():
    """Test rule approve/retire lifecycle."""
    engine = PostMortemEngine()

    # Insert a test rule
    engine._save_candidate_rule(
        {
            "rule_type": "test",
            "condition_json": "{}",
            "action_json": "{}",
            "confidence": 0.7,
            "source": "test",
        }
    )
    rules = engine.get_pending_rules(min_confidence=0.5)
    assert len(rules) > 0

    # Approve
    rule_id = rules[-1]["id"]
    engine.approve_rule(rule_id)

    # Retire
    engine.retire_rule(rule_id)

    # Should not appear in pending
    rules = engine.get_pending_rules(min_confidence=0.5)
    assert all(r["id"] != rule_id for r in rules)
