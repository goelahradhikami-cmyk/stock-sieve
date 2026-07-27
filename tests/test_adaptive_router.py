"""Characterization tests for AdaptiveRouter (6-Q.5b).

RECORD what the module currently does — not what it should do.
This module has ZERO production callers (verified 2026-07-27) and was
archived to src/thesis/archive/ on 2026-07-27; the safety net travels
with the code.

Covered surface:
  - TEMPERATURE / MOMENTUM_WINDOW constants
  - _compute_confidence: bias x momentum over 6 factor families
  - allocate: centered softmax, climate summary, decision reason
  - build_portfolio: <0.01 skip, equal-weight blend, normalization
"""

from types import SimpleNamespace

import numpy as np
import pytest

from src.thesis.archive.adaptive_router import AdaptiveRouter
from src.thesis.factor_momentum import FactorClimate, FactorMomentumEngine

FAMILIES = FactorMomentumEngine.FACTOR_FAMILIES


def make_router(tmp_path):
    return AdaptiveRouter(
        eval_db=str(tmp_path / "evaluation.db"), cache_db=str(tmp_path / "cache.db")
    )


def doctrine(did, bias):
    return SimpleNamespace(doctrine_id=did, factor_bias=bias)


def climate(momentum_map, regime="EARLY_RECOVERY"):
    return FactorClimate(
        date="2026-05-27",
        factors={f: {"momentum_60d": m} for f, m in momentum_map.items()},
        market_regime=regime,
    )


class FakeFME:
    def __init__(self, c):
        self.c = c

    def compute_factor_climate(self, trade_date):
        return self.c


class TestConstants:
    def test_values(self):
        assert AdaptiveRouter.TEMPERATURE == 15.0
        assert AdaptiveRouter.MOMENTUM_WINDOW == "momentum_60d"
        assert FAMILIES == ["quality", "value", "growth", "momentum", "risk", "sentiment"]


class TestConfidence:
    def test_bias_times_momentum_sum(self, tmp_path):
        r = make_router(tmp_path)
        d = doctrine("value_purist", {"value": 0.35, "quality": 0.35})
        c = climate({"value": 0.06, "quality": 0.02})
        # 0.35*0.06 + 0.35*0.02 = 0.028
        assert r._compute_confidence(d, c) == pytest.approx(0.028)

    def test_missing_bias_and_missing_factor_are_zero(self, tmp_path):
        r = make_router(tmp_path)
        d = doctrine("growth_hunter", {"growth": 0.45, "momentum": 0.15})
        c = climate({"growth": -0.02, "momentum": -0.03})
        # 0.45*(-0.02) + 0.15*(-0.03) = -0.0135 (docstring says -0.013)
        assert r._compute_confidence(d, c) == pytest.approx(-0.0135)

    def test_empty_bias_zero(self, tmp_path):
        r = make_router(tmp_path)
        assert r._compute_confidence(doctrine("x", {}), climate({"value": 0.5})) == 0.0


class TestAllocate:
    def test_softmax_sums_to_one(self, tmp_path):
        r = make_router(tmp_path)
        r.fme = FakeFME(climate({"value": 0.06, "growth": -0.02}))
        ds = [doctrine("a", {"value": 0.35}), doctrine("b", {"growth": 0.45})]
        decision = r.allocate(ds, "2026-05-27")
        assert sum(decision.doctrine_allocations.values()) == pytest.approx(1.0)
        # higher confidence -> higher allocation
        assert decision.doctrine_allocations["a"] > decision.doctrine_allocations["b"]

    def test_centered_softmax_exact(self, tmp_path):
        r = make_router(tmp_path)
        r.fme = FakeFME(climate({"value": 0.06, "quality": 0.02,
                                 "growth": -0.02, "momentum": -0.03}))
        ds = [doctrine("value_purist", {"value": 0.35, "quality": 0.35}),
              doctrine("growth_hunter", {"growth": 0.45, "momentum": 0.15})]
        decision = r.allocate(ds, "2026-05-27")
        confs = np.array([0.028, -0.0135])
        centered = confs - confs.mean()
        exp_vals = np.exp(centered * 15.0)
        expected = exp_vals[0] / exp_vals.sum()
        assert decision.doctrine_allocations["value_purist"] == pytest.approx(expected)
        assert decision.doctrine_confidences["value_purist"] == pytest.approx(0.028)

    def test_equal_confidence_uniform(self, tmp_path):
        r = make_router(tmp_path)
        r.fme = FakeFME(climate({}))
        ds = [doctrine("a", {}), doctrine("b", {}), doctrine("c", {})]
        decision = r.allocate(ds, "2026-05-27")
        for v in decision.doctrine_allocations.values():
            assert v == pytest.approx(1 / 3)

    def test_decision_fields(self, tmp_path):
        r = make_router(tmp_path)
        r.fme = FakeFME(climate({"value": 0.06, "growth": -0.02}, regime="PANIC"))
        ds = [doctrine("a", {"value": 0.35}), doctrine("b", {"growth": 0.45})]
        decision = r.allocate(ds, "2026-05-27")
        assert decision.market_regime == "PANIC"
        assert decision.strongest_factor == "value"
        assert decision.weakest_factor == "growth"
        # climate summary covers all 6 families, missing ones -> 0
        assert set(decision.factor_climate_summary.keys()) == set(FAMILIES)
        assert decision.factor_climate_summary["risk"] == 0
        assert "top_doctrine=a" in decision.decision_reason
        assert "regime=PANIC" in decision.decision_reason


class FakeBuilder:
    def __init__(self, picks_by_bias):
        self.picks_by_bias = picks_by_bias

    def score_universe(self, trade_date, factor_bias, top_n=20):
        return self.picks_by_bias.get(tuple(sorted(factor_bias.items())), [])


class TestBuildPortfolio:
    def _setup(self, tmp_path, monkeypatch, picks_by_bias, allocations):
        import src.thesis.archive.adaptive_router as mod

        monkeypatch.setattr(
            mod, "FactorSnapshotBuilder", lambda: FakeBuilder(picks_by_bias)
        )
        r = make_router(tmp_path)
        decision = SimpleNamespace(
            trade_date="2026-05-27", doctrine_allocations=allocations
        )
        return r, decision

    def test_skip_alloc_below_1pct(self, tmp_path, monkeypatch):
        bias = {"value": 0.5}
        r, decision = self._setup(
            tmp_path, monkeypatch,
            {tuple(sorted(bias.items())): [{"security_id": "AAA"}]},
            {"d1": 0.009},
        )
        out = r.build_portfolio([doctrine("d1", bias)], decision)
        assert out == {}  # 0.009 < 0.01 -> skipped, nothing to normalize

    def test_equal_weight_within_doctrine(self, tmp_path, monkeypatch):
        bias = {"value": 0.5}
        r, decision = self._setup(
            tmp_path, monkeypatch,
            {tuple(sorted(bias.items())): [{"security_id": "AAA"},
                                           {"security_id": "BBB"}]},
            {"d1": 1.0},
        )
        out = r.build_portfolio([doctrine("d1", bias)], decision)
        assert out == {"AAA": pytest.approx(0.5), "BBB": pytest.approx(0.5)}

    def test_blend_across_doctrines_and_normalize(self, tmp_path, monkeypatch):
        bias_v = {"value": 0.5}
        bias_g = {"growth": 0.5}
        r, decision = self._setup(
            tmp_path, monkeypatch,
            {
                tuple(sorted(bias_v.items())): [{"security_id": "AAA"}],
                tuple(sorted(bias_g.items())): [{"security_id": "AAA"},
                                                {"security_id": "BBB"}],
            },
            {"dv": 0.6, "dg": 0.4},
        )
        out = r.build_portfolio(
            [doctrine("dv", bias_v), doctrine("dg", bias_g)], decision
        )
        # raw: AAA = 0.6 + 0.4/2 = 0.8, BBB = 0.2; total 1.0 already
        assert out["AAA"] == pytest.approx(0.8)
        assert out["BBB"] == pytest.approx(0.2)
        assert sum(out.values()) == pytest.approx(1.0)

    def test_empty_picks_skipped(self, tmp_path, monkeypatch):
        r, decision = self._setup(tmp_path, monkeypatch, {}, {"d1": 1.0})
        out = r.build_portfolio([doctrine("d1", {"value": 0.5})], decision)
        assert out == {}
