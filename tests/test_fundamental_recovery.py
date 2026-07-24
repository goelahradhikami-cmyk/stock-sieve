"""Characterization tests for FundamentalRecoveryScorer (6-S.12.2).

RECORD what the module currently does — not what it should do.

Covered surface:
  - three subscore band tables (all boundaries)
  - _classify_revision ±0.02 strict
  - MARKET_STATE_WEIGHTS + unknown fallback
  - compute: <2 periods neutral path, amplification formula, clamps
  - _load_vintage_periods: vintage cutoff, 90d fallback, /100, net_margin
"""

import sqlite3

import pytest

from src.thesis.fundamental_recovery import (
    MARKET_STATE_WEIGHTS,
    REVISION_THRESHOLD,
    FundamentalRecoveryScorer,
)

SCHEMA = (
    "CREATE TABLE akshare_financials (code TEXT, report_date TEXT, available_date TEXT, "
    "earnings_yoy REAL, revenue_yoy REAL, net_profit REAL, revenue REAL, roe REAL, total_assets REAL)"
)


@pytest.fixture()
def scorer(tmp_path):
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    return FundamentalRecoveryScorer(cache_db=str(db)), str(db)


def insert(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO akshare_financials VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def fin(code, rd, ad, ey, ry, np_=None, rev=None):
    return (code, rd, ad, ey, ry, np_, rev, None, None)


class TestConstants:
    def test_weights(self):
        assert MARKET_STATE_WEIGHTS == {
            "EARLY_RECOVERY": 1.50,
            "STABILIZING": 1.00,
            "CONFIRMED_RECOVERY": 0.60,
            "PANIC": 0.50,
            "EUPHORIA": 0.50,
            "unknown": 0.50,
        }
        assert REVISION_THRESHOLD == 0.02


class TestEarningsBands:
    def test_all_bands(self, scorer):
        s, _ = scorer
        assert s._score_earnings_acceleration(0.20, 0.05) == 90.0  # +0.15
        assert s._score_earnings_acceleration(0.10, 0.05) == 70.0  # +0.05
        assert s._score_earnings_acceleration(0.056, 0.05) == 60.0  # +0.006
        assert s._score_earnings_acceleration(0.052, 0.05) == 50.0  # +0.002 stable
        assert s._score_earnings_acceleration(0.04, 0.05) == 40.0  # -0.01
        assert s._score_earnings_acceleration(-0.02, 0.05) == 30.0  # -0.07
        assert s._score_earnings_acceleration(-0.20, 0.05) == 10.0  # -0.25

    def test_boundaries_strict(self, scorer):
        # use previous=0.0 so revision equals the threshold literal EXACTLY
        # (avoid float subtraction artifacts like 0.07-0.05=0.020000000000000004)
        s, _ = scorer
        assert s._score_earnings_acceleration(0.10, 0.0) == 70.0  # ==0.10 not >
        assert s._score_earnings_acceleration(0.02, 0.0) == 60.0
        assert s._score_earnings_acceleration(0.005, 0.0) == 50.0
        assert s._score_earnings_acceleration(-0.005, 0.0) == 40.0
        assert s._score_earnings_acceleration(-0.02, 0.0) == 30.0
        assert s._score_earnings_acceleration(-0.10, 0.0) == 10.0

    def test_none_neutral(self, scorer):
        s, _ = scorer
        assert s._score_earnings_acceleration(None, 0.05) == 50.0
        assert s._score_earnings_acceleration(0.05, None) == 50.0


class TestMarginBands:
    def test_all_bands(self, scorer):
        s, _ = scorer
        assert s._score_margin_stabilization(0.15, 0.10) == 85.0  # +0.05
        assert s._score_margin_stabilization(0.12, 0.10) == 70.0  # +0.02
        assert s._score_margin_stabilization(0.105, 0.10) == 60.0  # +0.005
        assert s._score_margin_stabilization(0.10, 0.10) == 50.0  # 0
        assert s._score_margin_stabilization(0.095, 0.10) == 50.0  # -0.005 > -0.01
        assert s._score_margin_stabilization(0.08, 0.10) == 35.0  # -0.02
        assert s._score_margin_stabilization(0.05, 0.10) == 20.0  # -0.05

    def test_boundaries_strict(self, scorer):
        s, _ = scorer
        assert s._score_margin_stabilization(0.13, 0.10) == 70.0  # ==0.03
        assert s._score_margin_stabilization(0.11, 0.10) == 60.0  # ==0.01
        assert s._score_margin_stabilization(0.09, 0.10) == 35.0  # ==-0.01 -> next band > -0.03
        assert s._score_margin_stabilization(0.07, 0.10) == 20.0  # ==-0.03

    def test_none_neutral(self, scorer):
        s, _ = scorer
        assert s._score_margin_stabilization(None, None) == 50.0


class TestRevenueBands:
    def test_all_bands(self, scorer):
        s, _ = scorer
        assert s._score_revenue_acceleration(0.20, 0.10) == 85.0  # +0.10
        assert s._score_revenue_acceleration(0.13, 0.10) == 70.0  # +0.03
        assert s._score_revenue_acceleration(0.11, 0.10) == 58.0  # +0.01
        assert s._score_revenue_acceleration(0.10, 0.10) == 50.0  # 0
        assert s._score_revenue_acceleration(0.07, 0.10) == 35.0  # -0.03
        assert s._score_revenue_acceleration(0.02, 0.10) == 20.0  # -0.08

    def test_boundaries_strict(self, scorer):
        # exact literals via previous=0.0 (avoid float subtraction artifacts)
        s, _ = scorer
        assert s._score_revenue_acceleration(0.05, 0.0) == 70.0  # ==0.05 not >
        assert s._score_revenue_acceleration(0.02, 0.0) == 58.0
        assert s._score_revenue_acceleration(0.0, 0.0) == 50.0  # ==0 -> >-0.02 band
        assert s._score_revenue_acceleration(-0.02, 0.0) == 35.0
        assert s._score_revenue_acceleration(-0.05, 0.0) == 20.0

    def test_float_artifact_near_boundary(self, scorer):
        # pinned quirk: 0.08-0.10 = -0.020000000000000004 in float,
        # which is NOT > -0.02 -> falls into the 35.0 band, not 50.0
        s, _ = scorer
        assert s._score_revenue_acceleration(0.08, 0.10) == 35.0


class TestClassifyRevision:
    def test_bands(self, scorer):
        s, _ = scorer
        assert s._classify_revision(0.10, 0.05) == "improving"
        assert s._classify_revision(-0.10, 0.05) == "deteriorating"
        assert s._classify_revision(0.06, 0.05) == "stable"

    def test_boundaries_strict(self, scorer):
        # exact literals via previous=0.0: revision == ±0.02 -> NOT strict -> stable
        s, _ = scorer
        assert s._classify_revision(0.02, 0.0) == "stable"
        assert s._classify_revision(-0.02, 0.0) == "stable"

    def test_none_unknown(self, scorer):
        s, _ = scorer
        assert s._classify_revision(None, 0.05) == "unknown"


class TestVintageLoading:
    def test_vintage_cutoff_and_scaling(self, scorer):
        s, db = scorer
        insert(db, [
            fin("600519", "2024-06-30", "2024-08-30", 30.0, 15.0, 5.0, 100.0),
            fin("600519", "2024-03-31", "2024-04-30", 20.0, 10.0, 4.0, 100.0),
            fin("600519", "2024-09-30", "2024-11-15", 99.0, 99.0),  # future
        ])
        periods = s._load_vintage_periods("600519", "2024-09-01")
        assert len(periods) == 2
        assert periods[0]["earnings_yoy"] == pytest.approx(0.30)  # /100
        assert periods[0]["revenue_yoy"] == pytest.approx(0.15)
        assert periods[0]["net_margin"] == pytest.approx(0.05)
        assert periods[1]["earnings_yoy"] == pytest.approx(0.20)

    def test_90d_fallback(self, scorer):
        s, db = scorer
        insert(db, [
            fin("600519", "2024-06-30", None, 30.0, 15.0),  # +90d = 2024-09-28
        ])
        assert len(s._load_vintage_periods("600519", "2024-09-01")) == 0
        assert len(s._load_vintage_periods("600519", "2024-10-01")) == 1

    def test_net_margin_zero_revenue(self, scorer):
        s, db = scorer
        insert(db, [fin("600519", "2024-06-30", "2024-08-30", 30.0, 15.0, 5.0, 0.0)])
        periods = s._load_vintage_periods("600519", "2024-09-01")
        assert periods[0]["net_margin"] is None


class TestCompute:
    def seed2(self, db):
        # earnings: 0.30-0.20 -> band 70 ; margin: 0.10-0.05=0.05 -> 85 ;
        # revenue: 0.25-0.15=0.10 -> 85
        insert(db, [
            fin("600519", "2024-06-30", "2024-08-30", 30.0, 25.0, 10.0, 100.0),
            fin("600519", "2024-03-31", "2024-04-30", 20.0, 15.0, 5.0, 100.0),
        ])

    def test_insufficient_periods_neutral(self, scorer):
        s, _ = scorer
        r = s.compute("600519", "2024-09-01", "EARLY_RECOVERY")
        assert r.score == pytest.approx(min(100, max(0, 50.0 * 1.5)))  # 75.0
        r2 = s.compute("600519", "2024-09-01", "CONFIRMED_RECOVERY")
        assert r2.score == pytest.approx(30.0)  # 50 * 0.6

    def test_composite_amplification(self, scorer):
        s, db = scorer
        self.seed2(db)
        base = 0.5 * 70 + 0.3 * 85 + 0.2 * 85  # 77.5
        r = s.compute("600519", "2024-09-01", "STABILIZING")
        assert r.score == pytest.approx(50.0 + (base - 50.0) * 1.0)  # = 77.5
        # EARLY_RECOVERY amplifies
        r2 = s.compute("600519", "2024-09-01", "EARLY_RECOVERY")
        assert r2.score == pytest.approx(50.0 + (base - 50.0) * 1.5)
        # unknown state -> weight 0.5, dampens toward 50
        r3 = s.compute("600519", "2024-09-01")
        assert r3.score == pytest.approx(50.0 + (base - 50.0) * 0.5)
        assert r3.market_state_weight == 0.5

    def test_revision_direction_wired(self, scorer):
        s, db = scorer
        self.seed2(db)
        r = s.compute("600519", "2024-09-01", "STABILIZING")
        assert r.revision_direction == "improving"  # +0.10 > 0.02

    def test_clamp(self, scorer):
        s, db = scorer
        # extreme improvement -> amplified beyond 100 -> clamped
        insert(db, [
            fin("600519", "2024-06-30", "2024-08-30", 80.0, 60.0, 30.0, 100.0),
            fin("600519", "2024-03-31", "2024-04-30", 0.0, 0.0, 5.0, 100.0),
        ])
        r = s.compute("600519", "2024-09-01", "EARLY_RECOVERY")
        assert r.score == 100.0

    def test_to_dict(self, scorer):
        s, db = scorer
        self.seed2(db)
        d = s.compute("600519", "2024-09-01", "STABILIZING").to_dict()
        assert d["score"] == 77.5
        assert d["earnings_acceleration"] == 70.0
        assert d["margin_stabilization"] == 85.0
        assert d["revision_direction"] == "improving"
