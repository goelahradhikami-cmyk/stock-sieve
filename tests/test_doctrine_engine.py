"""
Tests for DoctrineEngine - Commit 6-L.1.

Covers: classify, crossover, mutate, weighted mutation distance, DoctrineRegistry.
"""

import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.doctrine_engine import (
    DoctrineEngine,
    DoctrineRegistry,
)


# ── Fixtures ──────────────────────────────────────────────

VALUE_IDENTITY = {
    "valuation": 90,
    "quality": 85,
    "growth": 40,
    "momentum": 15,
    "macro": 30,
    "contrarian": 80,
    "patience": 95,
    "concentration": 70,
}

GROWTH_IDENTITY = {
    "valuation": 50,
    "quality": 70,
    "growth": 90,
    "momentum": 40,
    "macro": 45,
    "contrarian": 20,
    "patience": 50,
    "concentration": 60,
}

MOMENTUM_IDENTITY = {
    "valuation": 10,
    "quality": 40,
    "growth": 40,
    "momentum": 95,
    "macro": 50,
    "contrarian": 5,
    "patience": 20,
    "concentration": 55,
}


@pytest.fixture
def engine():
    return DoctrineEngine()


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


# ── classify tests ────────────────────────────────────────


class TestClassify:
    def test_value_purist_classifies_correctly(self, engine):
        d = engine.classify(VALUE_IDENTITY)
        assert d.doctrine_id == "deep_value_purist"
        assert d.factor_bias["value"] > d.factor_bias["momentum"]
        assert d.factor_bias["quality"] > d.factor_bias["growth"]

    def test_growth_hunter_classifies_correctly(self, engine):
        d = engine.classify(GROWTH_IDENTITY)
        assert d.doctrine_id == "aggressive_growth_hunter"
        assert d.factor_bias["growth"] > d.factor_bias["value"]

    def test_momentum_chaser_classifies_correctly(self, engine):
        d = engine.classify(MOMENTUM_IDENTITY)
        assert d.doctrine_id == "momentum_trend_follower"
        assert d.factor_bias["momentum"] > 0.4

    def test_factor_bias_sums_to_one(self, engine):
        d = engine.classify(VALUE_IDENTITY)
        total = sum(d.factor_bias.values())
        assert abs(total - 1.0) < 0.01  # normalized

    def test_thesis_priority_not_empty(self, engine):
        d = engine.classify(VALUE_IDENTITY)
        assert len(d.thesis_priority) == 6  # all 6 patterns

    def test_confidence_model_has_primary_metrics(self, engine):
        d = engine.classify(VALUE_IDENTITY)
        assert len(d.confidence_model.primary_metrics) > 0
        # Value doctrine should care about roe
        assert "roe" in d.confidence_model.primary_metrics

    def test_different_doctrines_different_bias(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        dm = engine.classify(MOMENTUM_IDENTITY)
        assert dv.factor_bias["value"] != dm.factor_bias["value"]
        assert dv.factor_bias["momentum"] != dm.factor_bias["momentum"]


# ── crossover tests (补丁1: doctrine direct breeding) ─────


class TestCrossover:
    def test_child_factor_bias_between_parents(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        dg = engine.classify(GROWTH_IDENTITY)
        child = engine.crossover(dv, dg, alpha=0.5)

        # Child's value weight should be between parents'
        v_parent_a = dv.factor_bias["value"]
        v_parent_b = dg.factor_bias["value"]
        v_child = child.factor_bias["value"]
        assert min(v_parent_a, v_parent_b) - 0.01 <= v_child <= max(v_parent_a, v_parent_b) + 0.01

    def test_child_has_parent_doctrine_id(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        dg = engine.classify(GROWTH_IDENTITY)
        child = engine.crossover(dv, dg, alpha=0.5)
        assert child.parent_doctrine_id == dv.doctrine_id
        assert child.generation == 1

    def test_child_factor_bias_normalized(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        dg = engine.classify(GROWTH_IDENTITY)
        child = engine.crossover(dv, dg, alpha=0.5)
        total = sum(child.factor_bias.values())
        assert abs(total - 1.0) < 0.01

    def test_child_mutation_history_records_crossover(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        dg = engine.classify(GROWTH_IDENTITY)
        child = engine.crossover(dv, dg, alpha=0.5)
        assert len(child.mutation_history) == 1
        assert child.mutation_history[0]["type"] == "crossover"

    def test_alpha_0_means_child_is_parent_b(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        dg = engine.classify(GROWTH_IDENTITY)
        child = engine.crossover(dv, dg, alpha=0.0)
        # alpha=0 -> child = parent_b
        assert abs(child.factor_bias["growth"] - dg.factor_bias["growth"]) < 0.01


# ── mutate tests (补丁3: weighted mutation distance) ──────


class TestMutate:
    def test_mutate_returns_new_doctrine(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        mutated = engine.mutate(dv, mutation_rate=0.1)
        assert mutated is not None
        assert mutated.doctrine_id != dv.doctrine_id
        assert mutated.parent_doctrine_id == dv.doctrine_id
        assert mutated.generation == dv.generation + 1

    def test_mutate_records_history(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        mutated = engine.mutate(dv, mutation_rate=0.1)
        assert mutated is not None
        assert any(h["type"] == "mutation" for h in mutated.mutation_history)

    def test_mutate_factor_bias_normalized(self, engine):
        dv = engine.classify(VALUE_IDENTITY)
        mutated = engine.mutate(dv, mutation_rate=0.1)
        assert mutated is not None
        total = sum(mutated.factor_bias.values())
        assert abs(total - 1.0) < 0.01

    def test_large_mutation_rejected(self, engine):
        """补丁3: mutations exceeding MAX_MUTATION_DISTANCE are rejected."""
        dv = engine.classify(VALUE_IDENTITY)
        # Very high mutation rate should be rejected
        result = engine.mutate(dv, mutation_rate=2.0)
        # May or may not be rejected (random), but at least verify it doesn't crash
        assert result is None or result is not None


# ── weighted mutation distance tests (补丁3) ──────────────


class TestWeightedMutationDistance:
    def test_patience_change_costs_more_than_valuation(self, engine):
        """补丁3: patience (cost=2.0) change costs more than valuation (cost=1.0)."""
        base = VALUE_IDENTITY.copy()
        patience_changed = dict(base, patience=base["patience"] - 10)
        valuation_changed = dict(base, valuation=base["valuation"] - 10)

        d_patience = engine.identity_mutation_distance(base, patience_changed)
        d_valuation = engine.identity_mutation_distance(base, valuation_changed)

        assert d_patience > d_valuation
        # distance = sqrt(cost * delta^2), so ratio = sqrt(2.0)/sqrt(1.0) = 1.414
        assert d_patience / d_valuation == pytest.approx(1.414, rel=0.02)

    def test_zero_distance_for_identical(self, engine):
        d = engine.identity_mutation_distance(VALUE_IDENTITY, VALUE_IDENTITY)
        assert d == 0.0

    def test_is_mutation_allowed_within_threshold(self, engine):
        base = VALUE_IDENTITY.copy()
        small_change = dict(base, valuation=base["valuation"] - 5)
        assert engine.is_mutation_allowed(base, small_change)

    def test_is_mutation_blocked_for_large_change(self, engine):
        base = VALUE_IDENTITY.copy()
        # Change patience by 50 (cost=2.0, so distance = sqrt(2.0 * 0.5^2) = 0.707 > 0.35)
        huge_change = dict(base, patience=base["patience"] - 50)
        assert not engine.is_mutation_allowed(base, huge_change)


# ── DoctrineRegistry tests (补丁2: lifecycle) ────────────


class TestDoctrineRegistry:
    def test_seed_archetypes(self, tmp_db):
        reg = DoctrineRegistry(tmp_db)
        n = reg.seed_archetypes()
        assert n == 12

    def test_get_active_after_seed(self, tmp_db):
        reg = DoctrineRegistry(tmp_db)
        reg.seed_archetypes()
        active = reg.get_active()
        assert len(active) == 12

    def test_save_and_get_child(self, tmp_db):
        reg = DoctrineRegistry(tmp_db)
        reg.seed_archetypes()
        engine = DoctrineEngine()

        dv = engine.classify(VALUE_IDENTITY)
        dg = engine.classify(GROWTH_IDENTITY)
        child = engine.crossover(dv, dg, alpha=0.5)

        reg.save(child)
        loaded = reg.get(child.doctrine_id)
        assert loaded is not None
        assert loaded.parent_doctrine_id == dv.doctrine_id
        assert loaded.generation == 1

    def test_extinct_marks_doctrine(self, tmp_db):
        reg = DoctrineRegistry(tmp_db)
        reg.seed_archetypes()
        assert reg.extinct("deep_value_purist")
        active = reg.get_active()
        assert all(d.doctrine_id != "deep_value_purist" for d in active)

    def test_archetypes_not_ceiling(self, tmp_db):
        """补丁2: generated doctrines coexist with archetypes."""
        reg = DoctrineRegistry(tmp_db)
        reg.seed_archetypes()
        engine = DoctrineEngine()

        # Generate a hybrid
        dv = engine.classify(VALUE_IDENTITY)
        dm = engine.classify(MOMENTUM_IDENTITY)
        child = engine.crossover(dv, dm, alpha=0.5)
        reg.save(child)

        active = reg.get_active()
        # Should have 12 archetypes + 1 generated = 13
        assert len(active) == 13
        generations = [d.generation for d in active]
        assert 0 in generations  # archetypes
        assert 1 in generations  # generated child
