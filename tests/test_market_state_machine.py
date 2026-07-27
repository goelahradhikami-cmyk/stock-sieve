"""Characterization tests for MarketStateMachine (6-S.5.3).

RECORD what the module currently does — not what it should do.
This module has ZERO production callers (verified 2026-07-27;
state_transition.py only mentions it in a docstring) and was archived to
src/thesis/archive/ on 2026-07-27; the safety net travels with the code.

Covered surface:
  - classify: all five branches + boundary behavior (monkeypatched scorers)
  - _compute_vol_score: <30 rows fallback, formula, clamp
  - _compute_breadth_score: momentum>50 fraction, empty -> 0.5
  - _compute_trend_score: <20 rows fallback, ma60 formula
  - to_dict rounding
"""

import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from src.thesis.archive.market_state_machine import MarketStateMachine

DATE = "2024-08-29"


@pytest.fixture()
def msm(tmp_path):
    cache_db = tmp_path / "cache.db"
    eval_db = tmp_path / "evaluation.db"
    conn = sqlite3.connect(cache_db)
    conn.execute(
        "CREATE TABLE market_index_daily (index_code TEXT, trade_date TEXT, close REAL)"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(eval_db)
    conn.execute(
        "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, "
        "momentum_score REAL)"
    )
    conn.commit()
    conn.close()
    m = MarketStateMachine(eval_db=str(eval_db), cache_db=str(cache_db))
    return m, str(cache_db), str(eval_db)


def stub_scorers(m, vol=(0.5, -0.05), breadth=0.5, trend=(0.5, 0.0), recovery=0.5):
    m._compute_vol_score = lambda d: vol
    m._compute_breadth_score = lambda d: breadth
    m._compute_trend_score = lambda d: trend
    m._compute_recovery_prob = lambda d: recovery


def insert_index(cache_db, closes, start="2024-06-01"):
    conn = sqlite3.connect(cache_db)
    from datetime import date, timedelta

    d0 = date.fromisoformat(start)
    for i, c in enumerate(closes):
        conn.execute(
            "INSERT INTO market_index_daily VALUES (?,?,?)",
            ("000300", (d0 + timedelta(days=i)).isoformat(), c),
        )
    conn.commit()
    conn.close()


def insert_snapshot(eval_db, rows, date=DATE):
    conn = sqlite3.connect(eval_db)
    conn.executemany("INSERT INTO stock_factor_snapshot VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


class TestClassifyBranches:
    def test_panic_when_vol_score_low(self, msm):
        m, _, _ = msm
        stub_scorers(m, vol=(0.29, 0.0))
        r = m.classify(DATE)
        assert r.state_type == "PANIC"
        assert r.anomaly_weight == 0.0
        assert r.allows_anomaly is False
        assert "< 0.3" in r.state_reason

    def test_false_recovery_when_vol_not_contracting(self, msm):
        m, _, _ = msm
        stub_scorers(m, vol=(0.6, -0.009))  # vol_chg >= -0.01
        r = m.classify(DATE)
        assert r.state_type == "FALSE_RECOVERY"
        assert r.anomaly_weight == 0.0
        assert "not contracting" in r.state_reason

    def test_vol_chg_boundary_exactly_minus_001(self, msm):
        m, _, _ = msm
        stub_scorers(m, vol=(0.6, -0.01))  # NOT < -0.01
        r = m.classify(DATE)
        assert r.state_type == "FALSE_RECOVERY"

    def test_confirmed_recovery(self, msm):
        m, _, _ = msm
        stub_scorers(m, vol=(0.8, -0.04), breadth=0.50, recovery=0.60)
        r = m.classify(DATE)
        assert r.state_type == "CONFIRMED_RECOVERY"
        assert r.anomaly_weight == 1.0
        assert r.allows_anomaly is True

    def test_early_recovery(self, msm):
        m, _, _ = msm
        stub_scorers(m, vol=(0.6, -0.02), breadth=0.42, recovery=0.50)
        r = m.classify(DATE)
        assert r.state_type == "EARLY_RECOVERY"
        assert r.anomaly_weight == 0.6
        assert r.allows_anomaly is True

    def test_confirmed_needs_both_strong_vol_and_breadth(self, msm):
        m, _, _ = msm
        # vol_chg < -0.03 but breadth == 0.45 (not > 0.45) -> EARLY
        stub_scorers(m, vol=(0.8, -0.04), breadth=0.45, recovery=0.60)
        r = m.classify(DATE)
        assert r.state_type == "EARLY_RECOVERY"

    def test_false_recovery_low_recovery_prob(self, msm):
        m, _, _ = msm
        stub_scorers(m, vol=(0.6, -0.02), breadth=0.50, recovery=0.46)
        r = m.classify(DATE)
        assert r.state_type == "FALSE_RECOVERY"
        assert "not enough recovery" in r.state_reason

    def test_default_uncertain_branch(self, msm):
        """QUIRK (pinned): recovery_prob == 0.48 exactly satisfies neither
        > 0.48 nor < 0.48, so classification falls to the default
        'uncertain' FALSE_RECOVERY branch."""
        m, _, _ = msm
        stub_scorers(m, vol=(0.6, -0.02), breadth=0.50, recovery=0.48)
        r = m.classify(DATE)
        assert r.state_type == "FALSE_RECOVERY"
        assert r.state_reason.startswith("uncertain:")

    def test_breadth_boundary_040(self, msm):
        m, _, _ = msm
        # breadth == 0.40 is not > 0.40 -> falls through to default
        stub_scorers(m, vol=(0.6, -0.02), breadth=0.40, recovery=0.60)
        r = m.classify(DATE)
        assert r.state_type == "FALSE_RECOVERY"
        assert r.state_reason.startswith("uncertain:")


class TestVolScore:
    def test_fewer_than_30_rows_fallback(self, msm):
        m, cache_db, _ = msm
        insert_index(cache_db, [100.0] * 29)
        assert m._compute_vol_score(DATE) == (0.5, 0.0)

    def test_contraction_scores_above_half(self, msm):
        m, cache_db, _ = msm
        # 40 calm days then 30 volatile days early, calm at the end:
        # build 70 rows where the LAST 20 returns are calmer than the 60d window
        closes = [100.0]
        for i in range(69):
            step = 2.0 if i < 45 else 0.1  # volatile early, calm late
            closes.append(closes[-1] + step * (1 if i % 2 == 0 else -1))
        insert_index(cache_db, closes)
        vol_score, vol_chg = m._compute_vol_score(DATE)
        returns = np.diff(closes) / np.array(closes[:-1])
        vol_20d = float(np.std(returns[-20:]) * np.sqrt(252))
        vol_60d = float(np.std(returns[-60:]) * np.sqrt(252))
        expected_chg = vol_20d - vol_60d
        assert vol_chg == pytest.approx(expected_chg)
        assert vol_chg < 0  # contraction
        assert vol_score == pytest.approx(max(0, min(1, 0.5 - expected_chg * 5)))

    def test_clamp_bounds(self, msm):
        m, cache_db, _ = msm
        # violently expanding vol at the end -> vol_change * 5 > 0.5 -> clamp 0
        closes = [100.0 + 0.01 * i for i in range(50)]
        for i in range(20):
            closes.append(closes[-1] * (1.20 if i % 2 == 0 else 0.85))
        insert_index(cache_db, closes)
        vol_score, vol_chg = m._compute_vol_score(DATE)
        assert vol_chg > 0
        assert vol_score == 0.0


class TestBreadthScore:
    def test_fraction_above_50(self, msm):
        m, _, eval_db = msm
        insert_snapshot(eval_db, [
            ("AAA", DATE, 60.0),
            ("BBB", DATE, 50.0),  # not > 50
            ("CCC", DATE, 40.0),
            ("DDD", DATE, 55.0),
        ])
        assert m._compute_breadth_score(DATE) == pytest.approx(0.5)

    def test_empty_snapshot_half(self, msm):
        m, _, _ = msm
        assert m._compute_breadth_score(DATE) == 0.5


class TestTrendScore:
    def test_fewer_than_20_rows_fallback(self, msm):
        m, cache_db, _ = msm
        insert_index(cache_db, [100.0] * 19)
        assert m._compute_trend_score(DATE) == (0.5, 0.0)

    def test_flat_market_half(self, msm):
        m, cache_db, _ = msm
        insert_index(cache_db, [100.0] * 70)
        score, val = m._compute_trend_score(DATE)
        assert val == 0.0
        assert score == 0.5

    def test_uptrend_above_half(self, msm):
        m, cache_db, _ = msm
        closes = [100.0 + i for i in range(70)]  # steady uptrend
        insert_index(cache_db, closes)
        score, val = m._compute_trend_score(DATE)
        ma60 = float(np.mean(closes[-60:]))
        expected_val = float(np.clip((closes[-1] - ma60) / ma60, -1, 1))
        assert val == pytest.approx(expected_val)
        assert score == pytest.approx(max(0, min(1, 0.5 + expected_val * 0.5)))
        assert score > 0.5


class TestToDict:
    def test_rounding(self, msm):
        m, _, _ = msm
        stub_scorers(m, vol=(0.678, -0.04), breadth=0.456, trend=(0.567, 0.1),
                     recovery=0.6234)
        r = m.classify(DATE)
        d = r.to_dict()
        assert d["vol_score"] == 0.68
        assert d["breadth_score"] == 0.46
        assert d["trend_score"] == 0.57
        assert d["recovery_prob"] == 0.62
        assert d["anomaly_weight"] == 1.0
        assert d["state"] == "CONFIRMED_RECOVERY"
        assert d["allows_anomaly"] is True
