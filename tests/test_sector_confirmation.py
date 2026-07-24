"""Characterization tests for SectorConfirmationScorer (6-S.12.3).

RECORD what the module currently does — not what it should do.

Covered surface:
  - two subscore band tables (all boundaries)
  - _score_consistency: fallbacks + rate mapping
  - _shift_trading_days forward/backward offset math
  - data helpers (sector / cumulative / index close fallback)
  - compute(): three data-unavailable exits + full wiring
"""

import sqlite3

import pytest

from src.thesis.sector_confirmation import SectorConfirmationScorer

DAYS = [f"2024-08-{d:02d}" for d in range(1, 30)]


@pytest.fixture()
def scorer(tmp_path):
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trading_calendar (trade_date TEXT, is_trading INTEGER)")
    conn.execute("CREATE TABLE security_master (code TEXT, industry TEXT)")
    conn.execute("CREATE TABLE industry_daily_returns (industry TEXT, trade_date TEXT, return REAL)")
    conn.execute("CREATE TABLE market_index_daily (index_code TEXT, trade_date TEXT, adj_close REAL)")
    conn.executemany("INSERT INTO trading_calendar VALUES (?,1)", [(d,) for d in DAYS])
    conn.commit()
    conn.close()
    return SectorConfirmationScorer(cache_db=str(db)), str(db)


def sql(db, stmt, rows):
    conn = sqlite3.connect(db)
    conn.executemany(stmt, rows)
    conn.commit()
    conn.close()


class TestRelativeStrengthBands:
    def test_all(self, scorer):
        s, _ = scorer
        assert s._score_relative_strength(None) == 50.0
        assert s._score_relative_strength(0.06) == 90.0
        assert s._score_relative_strength(0.03) == 72.0
        assert s._score_relative_strength(0.01) == 60.0
        assert s._score_relative_strength(-0.01) == 45.0
        assert s._score_relative_strength(-0.03) == 30.0
        assert s._score_relative_strength(-0.10) == 15.0

    def test_boundaries_strict(self, scorer):
        s, _ = scorer
        assert s._score_relative_strength(0.05) == 72.0  # ==0.05 not >
        assert s._score_relative_strength(0.02) == 60.0
        assert s._score_relative_strength(0.0) == 45.0  # ==0 -> -0.02 band
        assert s._score_relative_strength(-0.02) == 30.0
        assert s._score_relative_strength(-0.05) == 15.0


class TestSectorStrengthBands:
    def test_all(self, scorer):
        s, _ = scorer
        assert s._score_sector_strength(None) == 50.0
        assert s._score_sector_strength(0.04) == 80.0
        assert s._score_sector_strength(0.02) == 65.0
        assert s._score_sector_strength(0.005) == 55.0
        assert s._score_sector_strength(-0.005) == 45.0
        assert s._score_sector_strength(-0.02) == 35.0
        assert s._score_sector_strength(-0.05) == 20.0

    def test_boundaries_strict(self, scorer):
        s, _ = scorer
        assert s._score_sector_strength(0.03) == 65.0
        assert s._score_sector_strength(0.01) == 55.0
        assert s._score_sector_strength(0.0) == 45.0
        assert s._score_sector_strength(-0.01) == 35.0
        assert s._score_sector_strength(-0.03) == 20.0


class TestShiftTradingDays:
    def test_backward(self, scorer):
        # ACTUAL behavior (pinned): negative branch uses `trade_date <= ?`
        # with OFFSET -offset-1, so offset -1 returns the date ITSELF when
        # it is a trading day; -2 gives the truly previous trading day.
        s, _ = scorer
        assert s._shift_trading_days("2024-08-10", -1) == "2024-08-10"  # itself!
        assert s._shift_trading_days("2024-08-10", -2) == "2024-08-09"
        assert s._shift_trading_days("2024-08-10", -5) == "2024-08-06"
        assert s._shift_trading_days("2024-08-01", -2) is None

    def test_forward(self, scorer):
        s, _ = scorer
        # offset >=0: >= date, OFFSET offset -> offset=0 is the date itself
        assert s._shift_trading_days("2024-08-10", 0) == "2024-08-10"
        assert s._shift_trading_days("2024-08-10", 5) == "2024-08-15"
        assert s._shift_trading_days("2024-08-29", 5) is None


class TestConsistency:
    def test_no_dates_neutral(self, scorer):
        s, db = scorer
        sql(db, "DELETE FROM trading_calendar", [])
        assert s._score_consistency("600519", "白酒", "2024-08-29") == 50.0

    def test_rate_mapping(self, scorer):
        s, db = scorer
        sql(db, "INSERT INTO industry_daily_returns VALUES (?,?,?)", [
            ("白酒", d, 0.005) for d in DAYS
        ])

        # fake stock: daily return +2% (always beats sector 0.5%)
        import pandas as pd

        def fake_kline(code, start, end):
            return pd.DataFrame({"close": [100.0, 102.0]})

        s.local = type("L", (), {"get_daily_kline": staticmethod(fake_kline)})()
        got = s._score_consistency("600519", "白酒", "2024-08-29")
        assert got == pytest.approx(100.0)  # 50 + (1.0-0.5)*100

        # stock always loses
        def fake_kline2(code, start, end):
            return pd.DataFrame({"close": [100.0, 100.0]})

        s.local = type("L", (), {"get_daily_kline": staticmethod(fake_kline2)})()
        got2 = s._score_consistency("600519", "白酒", "2024-08-29")
        assert got2 == pytest.approx(0.0)


class TestDataHelpers:
    def test_get_sector(self, scorer):
        s, db = scorer
        sql(db, "INSERT INTO security_master VALUES (?,?)", [("600519", "白酒"), ("000001", "")])
        assert s._get_sector("600519") == "白酒"
        assert s._get_sector("000001") is None  # empty string -> None
        assert s._get_sector("999999") is None

    def test_sector_cumulative_window(self, scorer):
        s, db = scorer
        sql(db, "INSERT INTO industry_daily_returns VALUES (?,?,?)", [
            ("白酒", "2024-08-05", 0.01),
            ("白酒", "2024-08-06", 0.02),
            ("白酒", "2024-08-07", None),
            ("白酒", "2024-08-08", 0.03),
        ])
        # (start, end]: excludes 08-05
        got = s._get_sector_cumulative("白酒", "2024-08-05", "2024-08-08")
        assert got == pytest.approx(1.02 * 1.03 - 1.0)
        assert s._get_sector_cumulative("白酒", "2024-07-01", "2024-07-02") is None

    def test_index_close_fallback(self, scorer):
        s, db = scorer
        sql(db, "INSERT INTO market_index_daily VALUES (?,?,?)", [
            ("000300", "2024-08-05", 4000.0),
        ])
        assert s._index_close("000300", "2024-08-05") == 4000.0
        assert s._index_close("000300", "2024-08-10") == 4000.0  # prior
        assert s._index_close("000300", "2024-08-01") is None
        assert s._get_market_return("2024-08-05", "2024-08-10") == 0.0


class TestCompute:
    def wire(self, s, db, stock_ret=0.10):
        import pandas as pd

        sql(db, "INSERT INTO security_master VALUES (?,?)", [("600519", "白酒")])
        sql(db, "INSERT INTO industry_daily_returns VALUES (?,?,?)", [
            ("白酒", d, 0.002) for d in DAYS
        ])
        sql(db, "INSERT INTO market_index_daily VALUES (?,?,?)", [
            ("000300", "2024-08-09", 4000.0),
            ("000300", "2024-08-29", 4040.0),
        ])

        def fake_kline(code, start, end):
            if start < end and (end == "2024-08-29"):
                return pd.DataFrame({"close": [100.0, 100.0 * (1 + stock_ret)]})
            return pd.DataFrame({"close": [100.0, 100.5]})  # daily consistency calls

        s.local = type("L", (), {"get_daily_kline": staticmethod(fake_kline)})()

    def test_no_start_date(self, scorer):
        s, db = scorer
        sql(db, "DELETE FROM trading_calendar", [])
        r = s.compute("600519", "2024-08-29")
        assert r.data_available is False and r.score == 50.0

    def test_no_sector(self, scorer):
        s, _ = scorer
        r = s.compute("600519", "2024-08-29")
        assert r.data_available is False and r.score == 50.0

    def test_no_sector_returns(self, scorer):
        s, db = scorer
        sql(db, "INSERT INTO security_master VALUES (?,?)", [("600519", "白酒")])
        r = s.compute("600519", "2024-08-29")
        assert r.data_available is False and r.score == 50.0

    def test_full_wiring(self, scorer):
        s, db = scorer
        self.wire(s, db, stock_ret=0.10)
        r = s.compute("600519", "2024-08-29")
        assert r.data_available is True
        assert r.sector == "白酒"
        assert r.stock_return_20d == pytest.approx(0.10)
        # sector cumulative: 19 days in (08-09, 08-29] at 0.002
        import math

        exp_sector = math.prod([1.002] * 19) - 1
        assert r.sector_return_20d == pytest.approx(exp_sector)
        assert r.rs_vs_sector == pytest.approx(0.10 - exp_sector)
        # market: 4040/4000 - 1 = 0.01
        assert r.market_return_20d == pytest.approx(0.01)
        assert r.sector_vs_market == pytest.approx(exp_sector - 0.01)
        # composite
        exp_score = (
            0.50 * r.relative_strength + 0.30 * r.sector_strength + 0.20 * r.consistency
        )
        assert r.score == pytest.approx(min(100.0, max(0.0, exp_score)))
