"""Characterization tests for EventReactionCalculator (6-S.15.1).

RECORD what the module currently does — not what it should do.

Covered surface:
  - constants, dataclass defaults
  - _load_earnings: vintage query (available_date / report_date+90d fallback),
    /100 scaling, 1st & 2nd derivative, FRM ±0.02 thresholds
  - trading calendar helpers (_next_trading_day strict-after, _offset fallbacks)
  - _index_close exact-then-prior fallback, zero-guard
  - _get_sector_code caching, _get_sector_cumulative_return compounding
  - compute(): full wiring with seeded DBs + fake kline provider
"""

import sqlite3

import pytest

from src.thesis.event_reaction import (
    PRIMARY_WINDOW,
    WINDOWS,
    EventReactionCalculator,
    EventReactionResult,
)


@pytest.fixture()
def calc(tmp_path):
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE akshare_financials (code TEXT, earnings_yoy REAL, report_date TEXT, available_date TEXT)"
    )
    conn.execute("CREATE TABLE trading_calendar (trade_date TEXT, is_trading INTEGER)")
    conn.execute(
        "CREATE TABLE market_index_daily (index_code TEXT, trade_date TEXT, adj_close REAL)"
    )
    conn.execute("CREATE TABLE security_master (code TEXT, industry TEXT)")
    conn.execute(
        "CREATE TABLE industry_daily_returns (industry TEXT, trade_date TEXT, return REAL)"
    )
    conn.commit()
    conn.close()
    return EventReactionCalculator(cache_db=str(db)), str(db)


def sql(db, stmt, rows=()):
    conn = sqlite3.connect(db)
    conn.executemany(stmt, rows) if rows else conn.execute(stmt)
    conn.commit()
    conn.close()


def seed_calendar(db, days):
    sql(db, "INSERT INTO trading_calendar VALUES (?,1)", [(d,) for d in days])


DAYS = [f"2024-09-{d:02d}" for d in range(1, 28)]  # 27 trading days in Sept


class TestConstants:
    def test_windows(self):
        assert WINDOWS == [1, 5, 10, 20]
        assert PRIMARY_WINDOW == 5

    def test_result_defaults_none(self):
        r = EventReactionResult(security_id="600519", available_date="2024-08-30")
        assert r.return_t1 is None
        assert r.sector_adjusted_t5 is None
        assert r.residual_t20 is None
        d = r.to_dict()
        assert d["security_id"] == "600519"


class TestLoadEarnings:
    def test_scaling_and_derivatives(self, calc):
        c, db = calc
        sql(
            db,
            "INSERT INTO akshare_financials VALUES (?,?,?,?)",
            [
                ("600519", 30.0, "2024-06-30", "2024-08-30"),
                ("600519", 20.0, "2024-03-31", "2024-04-30"),
                ("600519", 25.0, "2023-12-31", "2024-01-31"),
            ],
        )
        r = EventReactionResult(security_id="600519", available_date="2024-08-30")
        c._load_earnings(r, "600519", "2024-08-30")
        assert r.earnings_yoy_current == pytest.approx(0.30)
        assert r.earnings_yoy_previous == pytest.approx(0.20)
        assert r.earnings_yoy_previous2 == pytest.approx(0.25)
        # 1st derivative: 0.30-0.20 = 0.10 ; prev accel: 0.20-0.25 = -0.05
        assert r.earnings_acceleration == pytest.approx(0.10)
        assert r.earnings_acceleration_2nd == pytest.approx(0.10 - (-0.05))
        assert r.frm_direction == "improving"  # > 0.02

    def test_frm_thresholds(self, calc):
        c, db = calc
        sql(
            db,
            "INSERT INTO akshare_financials VALUES (?,?,?,?)",
            [
                ("600519", 21.0, "2024-06-30", "2024-08-30"),
                ("600519", 20.0, "2024-03-31", "2024-04-30"),
            ],
        )
        r = EventReactionResult(security_id="600519", available_date="2024-08-30")
        c._load_earnings(r, "600519", "2024-08-30")
        assert r.earnings_acceleration == pytest.approx(0.01)
        assert r.frm_direction == "stable"  # within ±0.02
        assert r.earnings_acceleration_2nd is None  # only 2 periods

    def test_vintage_cutoff(self, calc):
        c, db = calc
        # available AFTER as_of -> invisible
        sql(
            db,
            "INSERT INTO akshare_financials VALUES (?,?,?,?)",
            [
                ("600519", 99.0, "2024-06-30", "2024-09-15"),
            ],
        )
        r = EventReactionResult(security_id="600519", available_date="2024-08-30")
        c._load_earnings(r, "600519", "2024-08-30")
        assert r.earnings_yoy_current is None

    def test_report_date_90d_fallback(self, calc):
        c, db = calc
        # available_date NULL -> fallback date(report_date, '+90 days') <= as_of
        sql(
            db,
            "INSERT INTO akshare_financials VALUES (?,?,?,?)",
            [
                ("600519", 12.0, "2024-03-31", None),  # +90d = 2024-06-29 <= as_of
            ],
        )
        r = EventReactionResult(security_id="600519", available_date="2024-08-30")
        c._load_earnings(r, "600519", "2024-08-30")
        assert r.earnings_yoy_current == pytest.approx(0.12)


class TestCalendar:
    def test_next_trading_day_strictly_after(self, calc):
        c, db = calc
        seed_calendar(db, DAYS)
        assert c._next_trading_day("2024-09-01") == "2024-09-02"
        assert c._next_trading_day("2024-08-30") == "2024-09-01"
        assert c._next_trading_day("2024-09-27") is None

    def test_offset_trading_day(self, calc):
        c, db = calc
        seed_calendar(db, DAYS)
        assert c._offset_trading_day("2024-09-01", 5) == "2024-09-06"
        assert c._offset_trading_day("2024-09-27", 1) is None
        # start not in calendar -> first day >= start
        assert c._offset_trading_day("2024-08-25", 1) == "2024-09-02"
        # start after all days -> None
        assert c._offset_trading_day("2024-12-31", 1) is None


class TestIndexClose:
    def test_exact_then_prior_fallback(self, calc):
        c, db = calc
        sql(
            db,
            "INSERT INTO market_index_daily VALUES (?,?,?)",
            [
                ("000300", "2024-09-01", 4000.0),
                ("000300", "2024-09-05", 4100.0),
            ],
        )
        assert c._index_close("000300", "2024-09-05") == 4100.0
        # no exact row -> nearest prior
        assert c._index_close("000300", "2024-09-03") == 4000.0
        # nothing before -> None
        assert c._index_close("000300", "2024-08-01") is None

    def test_market_return_zero_guard(self, calc):
        c, db = calc
        sql(
            db,
            "INSERT INTO market_index_daily VALUES (?,?,?)",
            [
                ("000300", "2024-09-01", 0.0),
                ("000300", "2024-09-05", 4100.0),
            ],
        )
        assert c._get_market_return("2024-09-01", "2024-09-05") is None  # p0 == 0
        sql(db, "UPDATE market_index_daily SET adj_close=4000.0 WHERE trade_date='2024-09-01'")
        assert c._get_market_return("2024-09-01", "2024-09-05") == pytest.approx(0.025)


class TestSector:
    def test_sector_code_cached(self, calc):
        c, db = calc
        sql(db, "INSERT INTO security_master VALUES (?,?)", [("600519", "白酒")])
        assert c._get_sector_code("600519") == "白酒"
        assert c._get_sector_code("000001") is None  # caches None too
        assert "000001" in c._sector_cache

    def test_cumulative_return_compounding(self, calc):
        c, db = calc
        sql(
            db,
            "INSERT INTO industry_daily_returns VALUES (?,?,?)",
            [
                ("白酒", "2024-09-01", 0.01),
                ("白酒", "2024-09-02", 0.02),
                ("白酒", "2024-09-03", None),  # skipped
                ("白酒", "2024-09-04", -0.01),
            ],
        )
        # window (start, end]: excludes 09-01
        got = c._get_sector_cumulative_return("白酒", "2024-09-01", "2024-09-04")
        assert got == pytest.approx((1.02) * (0.99) - 1.0)
        assert c._get_sector_cumulative_return(None, "a", "b") is None
        assert c._get_sector_cumulative_return("不存在", "a", "b") is None


class TestComputeIntegration:
    def test_full_wiring(self, calc):
        c, db = calc
        seed_calendar(db, DAYS)
        sql(
            db,
            "INSERT INTO akshare_financials VALUES (?,?,?,?)",
            [
                ("600519", 30.0, "2024-06-30", "2024-08-30"),
                ("600519", 20.0, "2024-03-31", "2024-04-30"),
            ],
        )
        sql(db, "INSERT INTO security_master VALUES (?,?)", [("600519", "白酒")])
        # event_start = 2024-09-01 (first day after 2024-08-30... but 08-30 not in cal;
        # next_trading_day finds first cal day > 2024-08-30 = 2024-09-01)
        idx_days = ["2024-09-01", "2024-09-06", "2024-09-11", "2024-09-21"]
        for i, d in enumerate(idx_days):
            sql(
                db,
                "INSERT INTO market_index_daily VALUES (?,?,?)",
                [("000300", d, 4000.0 * (1.01**i))],
            )
        sql(
            db,
            "INSERT INTO industry_daily_returns VALUES (?,?,?)",
            [("白酒", d, 0.005) for d in DAYS[1:22]],
        )

        class FakeKline:
            empty = False

            def __len__(self):
                return 2

            def __getitem__(self, k):
                class V:
                    values = [100.0, 103.0]

                return V()

        c.local = type("L", (), {"get_daily_kline": lambda self, *a: FakeKline()})()

        r = c.compute("600519", "2024-08-30")
        assert r.security_id == "600519"
        assert r.return_t1 == pytest.approx(0.03)  # fake kline +3%
        assert r.earnings_acceleration == pytest.approx(0.10)
        assert r.sector_code == "白酒"
        # market t1: no 09-02 index row -> nearest-prior fallback = 09-01 close
        # -> market_return_t1 = 0.0 -> market_adjusted = raw 0.03
        assert r.market_adjusted_t1 == pytest.approx(0.03)
        # sector t1: single day 0.005 -> sector_adjusted = 0.03-0.005
        assert r.sector_adjusted_t1 == pytest.approx(0.025)
        # t5 window: event_end = 09-06, index 4000*1.01 -> market_ret 0.01
        assert r.return_t5 == pytest.approx(0.03)
        assert r.market_adjusted_t5 == pytest.approx(0.03 - 0.01)
        # residual_t5 = stock - market - sector
        assert r.residual_t5 == pytest.approx(r.return_t5 - r.market_return_t5 - r.sector_return_t5)

    def test_no_trading_after_announcement(self, calc):
        c, db = calc
        seed_calendar(db, DAYS)
        sql(
            db,
            "INSERT INTO akshare_financials VALUES (?,?,?,?)",
            [
                ("600519", 30.0, "2024-06-30", "2024-08-30"),
            ],
        )
        r = c.compute("600519", "2024-10-01")  # after all calendar days
        assert r.return_t1 is None
        assert r.earnings_yoy_current == pytest.approx(0.30)  # earnings still loaded
