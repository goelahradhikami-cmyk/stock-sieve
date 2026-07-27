"""Characterization tests for SustainabilityCalculator (6-S.16.1).

RECORD what the module currently does — not what it should do.

Covered surface:
  - threshold constants
  - _load_vintage_periods: vintage gate, 90d fallback, /100, operating_margin
  - _load_industry: security_id OR code lookup
  - _compute_alignment: sign-match + 0.3 ratio flag, elasticity edge cases
  - _compute_persistence: accel placeholders, reversal count, consistency flag
  - _compute_margin_normalization: 3q stats, std floor, industry zscore
  - _compute_composite: hard AND, INSUFFICIENT_DATA, failure_reason order
"""

import sqlite3

import numpy as np
import pytest

from src.thesis.sustainability_calculator import (
    ALIGNMENT_MIN_REVENUE_RATIO,
    CONSISTENCY_MAX_REVERSALS,
    MARGIN_STD_FLOOR,
    MARGIN_ZSCORE_PEAK,
    MIN_INDUSTRY_PEERS,
    MIN_PERIODS_FOR_3Q,
    SustainabilityCalculator,
    SustainabilityResult,
)

FIN_SCHEMA = (
    "CREATE TABLE akshare_financials (code TEXT, report_date TEXT, available_date TEXT, "
    "earnings_yoy REAL, revenue_yoy REAL, net_profit REAL, revenue REAL, "
    "operating_profit REAL, total_assets REAL, equity REAL)"
)
SM_SCHEMA = (
    "CREATE TABLE security_master (security_id TEXT, code TEXT, industry TEXT, "
    "total_mv REAL, float_mv REAL)"
)


@pytest.fixture()
def calc(tmp_path):
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute(FIN_SCHEMA)
    conn.execute(SM_SCHEMA)
    conn.commit()
    conn.close()
    return SustainabilityCalculator(cache_db=str(db)), str(db)


def insert_fin(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO akshare_financials VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def insert_sm(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO security_master VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def fin(code, rd, ad, ey, ry, np_=None, rev=None, op=None):
    return (code, rd, ad, ey, ry, np_, rev, op, None, None)


def res():
    return SustainabilityResult(security_id="600519", as_of_date="2024-08-29")


class TestConstants:
    def test_thresholds(self):
        assert ALIGNMENT_MIN_REVENUE_RATIO == 0.30
        assert CONSISTENCY_MAX_REVERSALS == 1
        assert MARGIN_ZSCORE_PEAK == 1.5
        assert MARGIN_STD_FLOOR == 0.005
        assert MIN_INDUSTRY_PEERS == 5
        assert MIN_PERIODS_FOR_3Q == 3


class TestLoadVintagePeriods:
    def test_vintage_gate_excludes_future(self, calc):
        c, db = calc
        insert_fin(db, [
            fin("600519", "2024-06-30", "2024-08-30", 20.0, 10.0),  # after as_of
            fin("600519", "2024-03-31", "2024-04-30", 15.0, 8.0),
        ])
        periods = c._load_vintage_periods("600519", "2024-08-29")
        assert len(periods) == 1
        assert periods[0]["report_date"] == "2024-03-31"

    def test_90d_fallback_when_available_date_null(self, calc):
        c, db = calc
        insert_fin(db, [
            # report_date + 90d = 2024-09-28 > as_of -> excluded
            fin("600519", "2024-06-30", None, 20.0, 10.0),
            # report_date + 90d = 2024-06-29 <= as_of -> included
            fin("600519", "2024-03-31", None, 15.0, 8.0),
        ])
        periods = c._load_vintage_periods("600519", "2024-08-29")
        assert len(periods) == 1
        assert periods[0]["report_date"] == "2024-03-31"

    def test_yoy_divided_by_100(self, calc):
        c, db = calc
        insert_fin(db, [fin("600519", "2024-06-30", "2024-08-01", 25.0, 12.5)])
        periods = c._load_vintage_periods("600519", "2024-08-29")
        assert periods[0]["earnings_yoy"] == 0.25
        assert periods[0]["revenue_yoy"] == 0.125

    def test_operating_margin_computation(self, calc):
        c, db = calc
        insert_fin(db, [
            fin("600519", "2024-06-30", "2024-08-01", 20.0, 10.0, rev=100.0, op=10.0),
            fin("600519", "2024-03-31", "2024-04-30", 20.0, 10.0, rev=0.0, op=10.0),
            fin("600519", "2023-12-31", "2024-01-30", 20.0, 10.0, rev=None, op=10.0),
        ])
        periods = c._load_vintage_periods("600519", "2024-08-29")
        assert periods[0]["operating_margin"] == pytest.approx(0.1)
        assert periods[1]["operating_margin"] is None  # rev == 0
        assert periods[2]["operating_margin"] is None  # rev is None

    def test_order_desc_limit(self, calc):
        c, db = calc
        insert_fin(db, [
            fin("600519", "2024-06-30", "2024-08-01", 1.0, 1.0),
            fin("600519", "2024-03-31", "2024-04-30", 2.0, 2.0),
            fin("600519", "2023-12-31", "2024-01-30", 3.0, 3.0),
            fin("600519", "2023-09-30", "2023-10-30", 4.0, 4.0),
        ])
        periods = c._load_vintage_periods("600519", "2024-08-29", limit=3)
        assert [p["report_date"] for p in periods] == [
            "2024-06-30", "2024-03-31", "2023-12-31",
        ]


class TestLoadIndustry:
    def test_found_by_security_id(self, calc):
        c, db = calc
        insert_sm(db, [("600519", "600519", "白酒", None, None)])
        assert c._load_industry("600519") == "白酒"

    def test_not_found_returns_none(self, calc):
        c, _ = calc
        assert c._load_industry("999999") is None


class TestAlignment:
    def test_flag_1_sign_match_and_ratio(self, calc):
        c, _ = calc
        r = res()
        c._compute_alignment(r, {"revenue_yoy": 0.20, "earnings_yoy": 0.50})
        assert r.alignment_flag == 1  # 0.20 >= 0.3*0.50 = 0.15
        assert r.profit_elasticity == pytest.approx(2.5)

    def test_flag_0_revenue_too_small(self, calc):
        c, _ = calc
        r = res()
        c._compute_alignment(r, {"revenue_yoy": 0.10, "earnings_yoy": 0.50})
        assert r.alignment_flag == 0  # 0.10 < 0.15

    def test_flag_0_sign_mismatch(self, calc):
        c, _ = calc
        r = res()
        c._compute_alignment(r, {"revenue_yoy": -0.10, "earnings_yoy": 0.50})
        assert r.alignment_flag == 0
        assert r.profit_elasticity == pytest.approx(-5.0)  # raw still stored

    def test_flag_1_both_negative(self, calc):
        c, _ = calc
        r = res()
        c._compute_alignment(r, {"revenue_yoy": -0.20, "earnings_yoy": -0.50})
        assert r.alignment_flag == 1  # signs match, 0.20 >= 0.15

    def test_none_inputs_flag_none(self, calc):
        c, _ = calc
        r = res()
        c._compute_alignment(r, {"revenue_yoy": None, "earnings_yoy": 0.50})
        assert r.alignment_flag is None
        assert r.profit_elasticity is None

    def test_zero_revenue_elasticity_none(self, calc):
        c, _ = calc
        r = res()
        c._compute_alignment(r, {"revenue_yoy": 0.0, "earnings_yoy": 0.50})
        assert r.profit_elasticity is None  # |rev| <= 1e-6
        assert r.alignment_flag == 0  # sign(0) != sign(0.5)

    def test_boundary_ratio_exactly_0_3(self, calc):
        c, _ = calc
        r = res()
        # use literals that are exactly representable: 0.15 >= 0.30*0.50
        c._compute_alignment(r, {"revenue_yoy": 0.15, "earnings_yoy": 0.50})
        assert r.alignment_flag == 1  # >= is inclusive


class TestPersistence:
    def test_basic_accels_and_flag(self, calc):
        c, _ = calc
        r = res()
        c._compute_persistence(
            r,
            {"earnings_yoy": 0.50},
            {"earnings_yoy": 0.25},
            {"earnings_yoy": 0.0},
        )
        assert r.accel_q0 == 0.25
        assert r.accel_q1 == 0.25
        assert r.accel_q2 == 0.25  # QUIRK (pinned): placeholder = accel_q1
        assert r.accel_trend == 0.0
        assert r.accel_volatility == 0.0
        assert r.reversal_count == 0
        assert r.consistency_flag == 1

    def test_accel_q2_placeholder_quirk(self, calc):
        """QUIRK (pinned): accel_q2 always equals accel_q1 (4th period not loaded).

        Consequence: reversal_count can never exceed 1, so the
        CONSISTENCY_MAX_REVERSALS=1 check is vacuous in practice.
        """
        c, _ = calc
        r = res()
        c._compute_persistence(
            r,
            {"earnings_yoy": 0.25},
            {"earnings_yoy": 0.50},
            {"earnings_yoy": 0.0},
        )
        assert r.accel_q1 == 0.50
        assert r.accel_q2 == r.accel_q1
        assert r.accel_trend == pytest.approx(-0.75)  # -0.25 - 0.50

    def test_reversal_count_skips_zeros(self, calc):
        c, _ = calc
        r = res()
        c._compute_persistence(
            r,
            {"earnings_yoy": 0.50},
            {"earnings_yoy": 0.0},
            {"earnings_yoy": 0.25},
        )
        # accels: +0.5, -0.25, -0.25 -> signs +,-,- -> 1 flip
        assert r.reversal_count == 1
        assert r.consistency_flag == 1  # accel_q0 > 0 and 1 <= 1

    def test_zero_accel_not_counted_as_flip(self, calc):
        c, _ = calc
        r = res()
        c._compute_persistence(
            r,
            {"earnings_yoy": 0.25},
            {"earnings_yoy": 0.25},
            {"earnings_yoy": 0.0},
        )
        # accels: 0.0, +0.25, +0.25 -> sign 0 skipped -> 0 flips
        assert r.reversal_count == 0
        assert r.consistency_flag == 0  # accel_q0 == 0.0 is not > 0

    def test_negative_accel_q0_fails(self, calc):
        c, _ = calc
        r = res()
        c._compute_persistence(
            r,
            {"earnings_yoy": 0.0},
            {"earnings_yoy": 0.25},
            {"earnings_yoy": 0.25},
        )
        assert r.accel_q0 == -0.25
        assert r.consistency_flag == 0

    def test_none_earnings_flag_none(self, calc):
        c, _ = calc
        r = res()
        c._compute_persistence(
            r,
            {"earnings_yoy": 0.50},
            {"earnings_yoy": None},
            {"earnings_yoy": 0.0},
        )
        assert r.consistency_flag is None
        assert r.accel_q0 is None


class TestMarginNormalization:
    def _qs(self, m0, m1, m2):
        return (
            {"operating_margin": m0},
            {"operating_margin": m1},
            {"operating_margin": m2},
        )

    def test_company_zscore(self, calc):
        c, _ = calc
        r = res()
        q0, q1, q2 = self._qs(0.10, 0.20, 0.15)
        c._compute_margin_normalization(r, q0, q1, q2)
        assert r.operating_margin_current == 0.10
        assert r.operating_margin_3q_median == pytest.approx(0.15)
        expected_std = float(np.std([0.10, 0.20, 0.15], ddof=0))
        assert r.operating_margin_3q_std == pytest.approx(expected_std)
        assert r.company_margin_zscore == pytest.approx((0.10 - 0.15) / expected_std)
        assert r.industry_margin_zscore is None  # no industry set on result
        assert r.margin_normalization_flag == 1  # |z| < 1.5

    def test_std_floor_on_constant_margins(self, calc):
        c, _ = calc
        r = res()
        # 0.125 is exactly representable -> std is exactly 0.0 (0.10 would
        # leave a ~1e-17 float residue in np.std)
        q0, q1, q2 = self._qs(0.125, 0.125, 0.125)
        c._compute_margin_normalization(r, q0, q1, q2)
        assert r.operating_margin_3q_std == 0.0  # raw std stored unfloored
        assert r.company_margin_zscore == 0.0  # (cur-median)/max(0, 0.005)
        assert r.margin_normalization_flag == 1

    def test_peak_margin_flag_0(self, calc):
        c, _ = calc
        r = res()
        q0, q1, q2 = self._qs(0.50, 0.10, 0.10)
        c._compute_margin_normalization(r, q0, q1, q2)
        expected_std = float(np.std([0.50, 0.10, 0.10], ddof=0))
        assert r.company_margin_zscore == pytest.approx(0.40 / expected_std)
        assert r.company_margin_zscore > MARGIN_ZSCORE_PEAK
        assert r.margin_normalization_flag == 0

    def test_fewer_than_3_valid_margins_flag_none(self, calc):
        c, _ = calc
        r = res()
        q0, q1, q2 = self._qs(0.10, None, 0.15)
        c._compute_margin_normalization(r, q0, q1, q2)
        assert r.margin_normalization_flag is None

    def test_industry_zscore_needs_5_peers(self, calc):
        c, db = calc
        insert_sm(db, [("600519", "600519", "白酒", None, None)])
        # only 4 peers -> None
        for i in range(4):
            code = f"60000{i}"
            insert_sm(db, [(code, code, "白酒", None, None)])
            insert_fin(db, [fin(code, "2024-06-30", "2024-08-01", 1.0, 1.0,
                                rev=100.0, op=10.0)])
        z = c._industry_margin_zscore("600519", "白酒", "2024-06-30", 0.30)
        assert z is None

    def test_industry_zscore_computed(self, calc):
        c, db = calc
        insert_sm(db, [("600519", "600519", "白酒", None, None)])
        peer_margins = [0.08, 0.10, 0.12, 0.09, 0.11]
        for i, m in enumerate(peer_margins):
            code = f"60000{i}"
            insert_sm(db, [(code, code, "白酒", None, None)])
            insert_fin(db, [fin(code, "2024-06-30", "2024-08-01", 1.0, 1.0,
                                rev=100.0, op=m * 100.0)])
        z = c._industry_margin_zscore("600519", "白酒", "2024-06-30", 0.30)
        peer_std = max(float(np.std(peer_margins, ddof=0)), MARGIN_STD_FLOOR)
        assert z == pytest.approx((0.30 - 0.10) / peer_std)

    def test_industry_none_or_report_date_none(self, calc):
        c, _ = calc
        assert c._industry_margin_zscore("600519", None, "2024-06-30", 0.30) is None
        assert c._industry_margin_zscore("600519", "白酒", None, 0.30) is None

    def test_flag_uses_max_of_zscores(self, calc):
        """QUIRK (pinned): flag rejects only when max(z) >= 1.5, i.e. a high
        industry z alone kills even with low company z."""
        c, db = calc
        insert_sm(db, [("600519", "600519", "白酒", None, None)])
        for i in range(5):
            code = f"60000{i}"
            insert_sm(db, [(code, code, "白酒", None, None)])
            insert_fin(db, [fin(code, "2024-06-30", "2024-08-01", 1.0, 1.0,
                                rev=100.0, op=10.0)])
        r = res()
        r.industry = "白酒"
        r.report_date = "2024-06-30"
        # company margins flat (z=0) but industry peers all at 0.10, current 0.30
        # -> industry z = (0.30-0.10)/max(0, 0.005) = 40 -> flag 0
        q0, q1, q2 = self._qs(0.30, 0.30, 0.30)
        c._compute_margin_normalization(r, q0, q1, q2)
        assert r.company_margin_zscore == 0.0
        assert r.industry_margin_zscore == pytest.approx(40.0)
        assert r.margin_normalization_flag == 0


class TestComposite:
    def test_any_none_flag_pass_none(self, calc):
        c, _ = calc
        r = res()
        r.alignment_flag = 1
        r.consistency_flag = None
        r.margin_normalization_flag = 1
        c._compute_composite(r)
        assert r.sustainability_pass is None
        assert r.failure_reason == "INSUFFICIENT_DATA"

    def test_all_pass(self, calc):
        c, _ = calc
        r = res()
        r.alignment_flag = 1
        r.consistency_flag = 1
        r.margin_normalization_flag = 1
        c._compute_composite(r)
        assert r.sustainability_pass == 1
        assert r.failure_reason is None

    def test_failure_reason_order(self, calc):
        c, _ = calc
        r = res()
        r.alignment_flag = 0
        r.consistency_flag = 0
        r.margin_normalization_flag = 0
        c._compute_composite(r)
        assert r.sustainability_pass == 0
        assert r.failure_reason == "ALIGNMENT_DECOUPLE"  # first failing wins

        r2 = res()
        r2.alignment_flag = 1
        r2.consistency_flag = 0
        r2.margin_normalization_flag = 0
        c._compute_composite(r2)
        assert r2.failure_reason == "CONSISTENCY_SPIKE"

        r3 = res()
        r3.alignment_flag = 1
        r3.consistency_flag = 1
        r3.margin_normalization_flag = 0
        c._compute_composite(r3)
        assert r3.failure_reason == "MARGIN_PEAK"


class TestComputeEndToEnd:
    def test_insufficient_periods(self, calc):
        c, db = calc
        insert_fin(db, [fin("600519", "2024-06-30", "2024-08-01", 20.0, 10.0)])
        r = c.compute("600519", "2024-08-29")
        assert r.sustainability_pass is None
        assert r.failure_reason == "INSUFFICIENT_DATA"

    def test_full_pass(self, calc):
        c, db = calc
        # 3 periods: earnings accelerating gently, aligned revenue, flat margins
        insert_fin(db, [
            fin("600519", "2024-06-30", "2024-08-01", 25.0, 20.0, rev=100.0, op=10.0),
            fin("600519", "2024-03-31", "2024-04-30", 20.0, 15.0, rev=100.0, op=10.0),
            fin("600519", "2023-12-31", "2024-01-30", 18.0, 14.0, rev=100.0, op=10.0),
        ])
        insert_sm(db, [("600519", "600519", "白酒", None, None)])
        r = c.compute("600519", "2024-08-29")
        assert r.industry == "白酒"
        assert r.report_date == "2024-06-30"
        assert r.alignment_flag == 1  # 0.20 >= 0.3*0.25
        assert r.consistency_flag == 1  # accels +0.05,+0.02,+0.02
        assert r.margin_normalization_flag == 1  # flat margins, z=0
        assert r.sustainability_pass == 1
        assert r.failure_reason is None

    def test_code_zfilled(self, calc):
        c, db = calc
        insert_fin(db, [
            fin("000001", "2024-06-30", "2024-08-01", 25.0, 20.0),
            fin("000001", "2024-03-31", "2024-04-30", 20.0, 15.0),
            fin("000001", "2023-12-31", "2024-01-30", 18.0, 14.0),
        ])
        r = c.compute("1", "2024-08-29")  # zfill("1") -> "000001"
        assert r.security_id == "000001"
        assert r.sustainability_pass is not None or r.failure_reason == "INSUFFICIENT_DATA"
