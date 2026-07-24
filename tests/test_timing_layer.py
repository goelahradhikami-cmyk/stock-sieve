"""Characterization tests for ThesisTimingLayer (6-Q.3/6-Q.4).

RECORD what the module currently does — not what it should do.

Covered surface:
  - overlay: base weight, factor blending (note the total_score +1 quirk),
    conviction thresholds, risk veto, clamp, normalization
  - _compute_market_timing: regime tables, momentum overlay, crowding penalty
  - _compute_factor_momentum dispersion proxy
  - _read_crowding AVG-over-all-rows quirk
  - get_weighted_returns
"""

import sqlite3

import numpy as np
import pytest

from src.thesis.timing_layer import ThesisTimingLayer, TimingResult


@pytest.fixture()
def layer(tmp_path):
    ev = tmp_path / "eval.db"
    cache = tmp_path / "cache.db"
    e = sqlite3.connect(ev)
    e.execute("CREATE TABLE market_regime_snapshots (obs_date TEXT, regime_type TEXT)")
    e.execute(
        "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, "
        "quality_score REAL, value_score REAL, growth_score REAL, "
        "momentum_score REAL, risk_score REAL, sentiment_score REAL)"
    )
    e.execute("CREATE TABLE alpha_decay_history (generation INTEGER, crowding_score REAL)")
    e.commit()
    e.close()
    c = sqlite3.connect(cache)
    c.execute(
        "CREATE TABLE akshare_financials (code TEXT, report_date TEXT, "
        "revenue_yoy REAL, earnings_yoy REAL, roe REAL, total_assets REAL, equity REAL)"
    )
    c.commit()
    c.close()
    return ThesisTimingLayer(eval_db=str(ev), cache_db=str(cache)), str(ev), str(cache)


def make_picks(n=4, **per_stock):
    picks = []
    for i in range(n):
        p = {
            "security_id": f"60000{i}",
            "quality_score": 50,
            "value_score": 50,
            "growth_score": 50,
            "momentum_score": 50,
            "risk_score": 50,
        }
        p.update(per_stock.get(f"60000{i}", {}))
        picks.append(p)
    return picks


class TestEmptyAndBase:
    def test_empty_picks(self, layer):
        tl, _, _ = layer
        r = tl.apply_timing_overlay([], None, "2026-01-15")
        assert isinstance(r, TimingResult)
        assert r.adjustments == []
        assert r.vetoed_count == 0

    def test_base_weight_equal(self, layer):
        tl, _, _ = layer
        r = tl.apply_timing_overlay(make_picks(4), None, "2026-01-15")
        assert all(a.base_weight == pytest.approx(0.25) for a in r.adjustments)
        # QUIRK (pinned): total_score = q+v+g+m+1 (the +1), so even with empty
        # factor multipliers stock_timing = 200/201, not exactly 1.0
        blend = 200 / 201
        assert all(a.timing_multiplier == pytest.approx(blend) for a in r.adjustments)
        # |blend - 1.0| < 0.05 -> reason stays "no_adjustment"
        assert all(a.timing_reason == "no_adjustment" for a in r.adjustments)
        assert sum(a.final_weight for a in r.adjustments) == pytest.approx(1.0)


class TestRiskVeto:
    def test_veto_conditions(self, layer):
        tl, _, _ = layer
        # veto needs risk<20 AND quality<30
        picks = make_picks(4, **{
            "600000": {"risk_score": 10, "quality_score": 20},   # veto
            "600001": {"risk_score": 10, "quality_score": 50},   # no (quality ok)
            "600002": {"risk_score": 30, "quality_score": 20},   # no (risk ok)
        })
        r = tl.apply_timing_overlay(picks, None, "2026-01-15")
        vetoed = [a for a in r.adjustments if a.vetoed]
        assert len(vetoed) == 1
        assert vetoed[0].security_id == "600000"
        assert vetoed[0].timing_multiplier == 0.0
        assert "RISK_VETO" in vetoed[0].timing_reason
        assert r.vetoed_count == 1
        # vetoed excluded from normalization
        assert sum(a.final_weight for a in r.adjustments if not a.vetoed) == pytest.approx(1.0)
        # but its 0.0 still counts in avg_timing_multiplier;
        # note 600002 has quality=20 -> its blend is 170/171, not 200/201
        expected_avg = (200 / 201 + 200 / 201 + 170 / 171 + 0.0) / 4
        assert r.avg_timing_multiplier == pytest.approx(expected_avg)


class TestConviction:
    def test_conviction_multipliers(self, layer):
        tl, _, cache = layer
        # seed earnings so sigmoid(accel*10) > 0.6 for 600000 (accel > ~0.092)
        # and < 0.3 for 600001 (accel < -0.085)
        conn = sqlite3.connect(cache)
        rows = []
        # 600000: earnings accelerating: latest 30, oldest 10 -> delta 0.20
        for i, (rd, ey) in enumerate([("2025-12-31", 30.0), ("2025-09-30", 20.0), ("2025-06-30", 10.0)]):
            rows.append(("600000", rd, 0.0, ey, 0.1, None, None))
        # 600001: decelerating hard: earn delta -0.25, rev delta -0.20
        # -> accel = -0.15 -> sigmoid(-1.5) ≈ 0.18 < 0.3
        # (mild decel like accel=-0.067 -> sigmoid ≈ 0.34 does NOT trigger 0.7x)
        for rd, ey, ry in [("2025-12-31", 5.0, 5.0), ("2025-09-30", 15.0, 15.0), ("2025-06-30", 30.0, 25.0)]:
            rows.append(("600001", rd, ry, ey, 0.1, None, None))
        conn.executemany("INSERT INTO akshare_financials VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        r = tl.apply_timing_overlay(make_picks(4), None, "2026-01-15")
        mults = {a.security_id: a.timing_multiplier for a in r.adjustments}
        blend = 200 / 201  # the total_score +1 quirk, see TestEmptyAndBase
        assert mults["600000"] == pytest.approx(blend * 1.3)
        assert mults["600001"] == pytest.approx(blend * 0.7)
        assert mults["600002"] == pytest.approx(blend)  # no data -> sigmoid(0)=0.5 neutral
        reasons = {a.security_id: a.timing_reason for a in r.adjustments}
        assert "earnings_accelerating" in reasons["600000"]
        assert "earnings_decelerating" in reasons["600001"]


class TestMarketTiming:
    def test_crash_regime_table(self, layer):
        tl, ev, _ = layer
        conn = sqlite3.connect(ev)
        conn.execute("INSERT INTO market_regime_snapshots VALUES ('2026-01-15','crash')")
        conn.commit()
        conn.close()
        regime, mults = tl._compute_market_timing("2026-01-15", None)
        assert regime == "crash"
        assert mults["quality"] == pytest.approx(1.4)
        assert mults["momentum"] == pytest.approx(0.5)

    def test_sideway_empty_mults(self, layer):
        tl, _, _ = layer
        regime, mults = tl._compute_market_timing("2026-01-15", None)
        assert regime == "unknown" or regime == "sideway"
        # factor_mults may gain momentum-overlay entries; with no snapshot rows -> {}
        assert mults == {}

    def test_momentum_overlay(self, layer):
        tl, ev, _ = layer
        # 25 stocks, quality std ~ 29 -> momentum = (29-15)/40 ~ 0.35
        conn = sqlite3.connect(ev)
        conn.execute("INSERT INTO market_regime_snapshots VALUES ('2026-01-15','sideway')")
        scores = [10 + i * 3.3 for i in range(25)]
        conn.executemany(
            "INSERT INTO stock_factor_snapshot VALUES (?,?,?,?,?,?,?,?)",
            [(f"s{i}", "2026-01-15", q, 50.0, 50.0, 50.0, 50.0, 50.0) for i, q in enumerate(scores)],
        )
        conn.commit()
        conn.close()
        regime, mults = tl._compute_market_timing("2026-01-15", None)
        std = float(np.std(scores))
        mom = max(0.0, (std - 15.0) / 40.0)
        assert mults["quality"] == pytest.approx(1.0 * max(0.5, min(2.0, 1.0 + mom * 2.0)))
        # other families: std of identical 50s = 0 -> momentum 0 -> mult 1.0
        assert mults["value"] == pytest.approx(1.0)
        if mom > 0.02:
            assert "quality_momentum_up" in regime

    def test_crowding_penalty_and_label(self, layer):
        tl, ev, _ = layer
        conn = sqlite3.connect(ev)
        conn.execute("INSERT INTO market_regime_snapshots VALUES ('2026-01-15','bull')")
        conn.executemany(
            "INSERT INTO alpha_decay_history VALUES (?,?)",
            [(i, 0.9) for i in range(40)],
        )
        conn.commit()
        conn.close()
        regime, mults = tl._compute_market_timing("2026-01-15", None)
        assert mults["momentum"] == pytest.approx(1.4 * 0.85)
        assert "crowded" in regime

    def test_crowding_avg_all_rows_quirk(self, layer):
        # SQL is SELECT AVG(crowding_score) ... ORDER BY generation DESC LIMIT 32
        # on an AGGREGATE query -> AVG over ALL rows (LIMIT applies to the
        # single aggregate row). Pinned: all 40 rows averaged, not last 32.
        tl, ev, _ = layer
        conn = sqlite3.connect(ev)
        conn.executemany(
            "INSERT INTO alpha_decay_history VALUES (?,?)",
            [(i, 1.0) for i in range(32)] + [(32 + i, 0.0) for i in range(8)],
        )
        conn.commit()
        conn.close()
        assert tl._read_crowding() == pytest.approx(32 / 40)


class TestFactorMomentumProxy:
    def test_fewer_than_20_rows(self, layer):
        tl, ev, _ = layer
        conn = sqlite3.connect(ev)
        conn.executemany(
            "INSERT INTO stock_factor_snapshot VALUES (?,?,?,?,?,?,?,?)",
            [(f"s{i}", "2026-01-15", 50, 50, 50, 50, 50, 50) for i in range(10)],
        )
        conn.commit()
        conn.close()
        assert tl._compute_factor_momentum("2026-01-15") == {}

    def test_dispersion_mapping(self, layer):
        tl, ev, _ = layer
        conn = sqlite3.connect(ev)
        scores = [10 + i * 3.3 for i in range(25)]
        conn.executemany(
            "INSERT INTO stock_factor_snapshot VALUES (?,?,?,?,?,?,?,?)",
            [(f"s{i}", "2026-01-15", q, None, 50, 50, 50, 50) for i, q in enumerate(scores)],
        )
        conn.commit()
        conn.close()
        mom = tl._compute_factor_momentum("2026-01-15")
        std = float(np.std(scores))
        assert mom["quality"] == pytest.approx(max(0.0, (std - 15.0) / 40.0))
        assert mom["value"] == 0.0  # all NULL -> <10 scores -> 0.0
        assert mom["growth"] == 0.0  # std 0 -> max(0, -15/40) = 0


class TestWeightedReturns:
    def test_fallback_mean(self, layer):
        tl, _, _ = layer
        # no adjustments -> equal-weight mean
        assert tl.get_weighted_returns([], [0.1, 0.2], TimingResult()) == pytest.approx(0.15)
        assert tl.get_weighted_returns([], [], TimingResult()) == 0.0

    def test_weighted_sum_excludes_vetoed(self, layer):
        tl, _, _ = layer
        r = tl.apply_timing_overlay(
            make_picks(2, **{"600000": {"risk_score": 10, "quality_score": 10}}),
            None,
            "2026-01-15",
        )
        # 600000 vetoed -> all weight on 600001
        got = tl.get_weighted_returns([], [0.5, 0.1], r)
        assert got == pytest.approx(0.1)
