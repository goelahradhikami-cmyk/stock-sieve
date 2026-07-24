"""Characterization tests for InvestmentMemory (6-S.7).

RECORD what the module currently does — not what it should do.

Covered surface:
  - belief-state audit bands + can_evolve
  - win_rate / posterior math, INSERT OR REPLACE upsert
  - get_beliefs / get_evolvable_beliefs filtering & ordering
  - apply_decay exp decay, bad-date skip
  - get_memory_summary, _row_to_belief conversions, to_dict
"""

import math

import pytest

from src.thesis.investment_memory import (
    CONDITIONAL,
    DECAY_LAMBDA,
    HYPOTHESIS,
    REJECTED,
    VERIFIED,
    InvestmentMemory,
)


@pytest.fixture()
def mem(tmp_path):
    return InvestmentMemory(eval_db=str(tmp_path / "eval.db"))


def store(m, state="PANIC", doctrine="quality", audits=(0.9, 0.9, 0.9, 0.9), **kw):
    base = dict(
        market_state=state,
        doctrine=doctrine,
        n_theses=10,
        n_success=7,
        avg_return=0.08,
        avg_alpha=0.05,
        sample_score=audits[0],
        regime_score=audits[1],
        causal_score=audits[2],
        decay_score=audits[3],
    )
    base.update(kw)
    return m.store_belief(**base)


class TestBeliefBands:
    def test_verified(self, mem):
        b = store(mem, audits=(0.9, 0.9, 0.9, 0.9))  # 0.9 >= 0.85
        assert b.belief_state == VERIFIED
        assert b.can_evolve is True
        assert b.audit_score == pytest.approx(0.9)

    def test_bands_and_evolve(self, mem):
        assert store(mem, doctrine="a", audits=(0.85, 0.85, 0.85, 0.85)).belief_state == VERIFIED
        b = store(mem, doctrine="b", audits=(0.6, 0.6, 0.6, 0.6))
        assert b.belief_state == CONDITIONAL and b.can_evolve is True
        c = store(mem, doctrine="c", audits=(0.39, 0.39, 0.39, 0.39))
        assert c.belief_state == HYPOTHESIS and c.can_evolve is False
        d = store(mem, doctrine="d", audits=(0.2, 0.2, 0.2, 0.2))
        assert d.belief_state == HYPOTHESIS
        e = store(mem, doctrine="e", audits=(0.1, 0.1, 0.1, 0.1))
        assert e.belief_state == REJECTED and e.can_evolve is False

    def test_boundary_04(self, mem):
        b = store(mem, doctrine="x", audits=(0.4, 0.4, 0.4, 0.4))
        assert b.belief_state == CONDITIONAL  # >= 0.4


class TestMath:
    def test_win_rate_and_posterior(self, mem):
        b = store(mem, n_theses=10, n_success=7)
        assert b.win_rate == pytest.approx(0.7)
        assert b.posterior_alpha == pytest.approx(9.0)  # 2+7
        assert b.posterior_beta == pytest.approx(5.0)  # 2+3

    def test_zero_theses_win_rate_half(self, mem):
        b = store(mem, n_theses=0, n_success=0)
        assert b.win_rate == 0.5
        assert b.posterior_alpha == 2.0 and b.posterior_beta == 2.0

    def test_belief_id_format(self, mem):
        b = store(mem, state="EARLY_RECOVERY", doctrine="value")
        assert b.belief_id == "EARLY_RECOVERY+value"

    def test_decay_lambda(self):
        assert DECAY_LAMBDA == pytest.approx(math.log(2) / 365)


class TestPersistence:
    def test_upsert_replaces(self, mem):
        store(mem, n_success=7)
        b2 = store(mem, n_success=3)  # same belief_id
        beliefs = mem.get_beliefs("PANIC")
        assert len(beliefs) == 1
        assert beliefs[0].n_success == 3
        assert beliefs[0].belief_id == b2.belief_id

    def test_get_beliefs_order_and_filter(self, mem):
        store(mem, doctrine="low", audits=(0.5, 0.5, 0.5, 0.5))
        store(mem, doctrine="high", audits=(0.9, 0.9, 0.9, 0.9))
        store(mem, state="STABILIZING", doctrine="other", audits=(0.7, 0.7, 0.7, 0.7))
        panic = mem.get_beliefs("PANIC")
        assert [b.doctrine for b in panic] == ["high", "low"]  # audit_score DESC
        assert len(mem.get_beliefs()) == 3  # no filter

    def test_get_evolvable(self, mem):
        store(mem, doctrine="a", audits=(0.9, 0.9, 0.9, 0.9))  # evolvable
        store(mem, doctrine="b", audits=(0.1, 0.1, 0.1, 0.1))  # REJECTED
        evo = mem.get_evolvable_beliefs()
        assert [b.doctrine for b in evo] == ["a"]

    def test_missing_audits_roundtrip(self, mem):
        store(mem, missing_audits=["regime_stability"])
        b = mem.get_beliefs("PANIC")[0]
        assert b.missing_audits == ["regime_stability"]
        assert b.can_evolve is True  # bool conversion
        assert b.decay_factor == 1.0


class TestDecay:
    def test_exp_decay(self, mem):
        b = store(mem)
        # backdate last_updated by editing db directly via apply on future date
        updated = mem.apply_decay(as_of_date=b.last_updated[:10])
        assert updated == 1
        # same-day -> decay ~1.0 (0 days)
        after = mem.get_beliefs("PANIC")[0]
        assert after.decay_factor == pytest.approx(1.0)
        # 365 days later -> ~0.5
        from datetime import datetime, timedelta

        future = (datetime.fromisoformat(b.last_updated) + timedelta(days=365)).isoformat()
        mem.apply_decay(as_of_date=future)
        after2 = mem.get_beliefs("PANIC")[0]
        assert after2.decay_factor == pytest.approx(0.5, abs=0.01)

    def test_summary(self, mem):
        store(mem, doctrine="a", audits=(0.9, 0.9, 0.9, 0.9))
        store(mem, doctrine="b", audits=(0.1, 0.1, 0.1, 0.1))
        s = mem.get_memory_summary()
        assert s["total"] == 2
        assert s["by_state"] == {VERIFIED: 1, REJECTED: 1}
        assert s["evolvable"] == 1
        assert s["avg_decay"] == 1.0


class TestToDict:
    def test_shape(self, mem):
        b = store(mem, missing_audits=["x"])
        d = b.to_dict()
        assert d["belief_id"] == "PANIC+quality"
        assert d["state"] == "PANIC"
        assert d["win_rate"] == 0.7
        assert d["missing"] == ["x"]
        assert d["posterior"] == "Beta(9.0, 5.0)"
        assert d["can_evolve"] is True
