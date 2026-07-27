"""Characterization tests for CrowdingCalculator (6-S.17.4).

RECORD what the module currently does — not what it should do.

Covered surface:
  - lookback constants
  - _offset_date calendar arithmetic
  - _load_kline_before: strict < trade_date filter, tail-or-all semantics
  - _compute_momentum / _compute_volatility / _compute_volume_features
  - _load_market_cap from security_master
  - compute(): insufficient history short-circuit, composite always None
  - compute_turnover_percentile: nearest snapshot, >=10 gate, formula
"""

import sqlite3

import numpy as np
import pandas as pd
import pytest

from src.thesis.crowding_calculator import (
    LOOKBACK_20D,
    LOOKBACK_60D,
    MIN_HISTORY_DAYS,
    CrowdingCalculator,
)

SM_SCHEMA = (
    "CREATE TABLE security_master (security_id TEXT, code TEXT, industry TEXT, "
    "total_mv REAL, float_mv REAL)"
)
FS_SCHEMA = "CREATE TABLE finance_snapshots (code TEXT, date TEXT, turnover_pct REAL)"


class FakeLocal:
    """Stand-in for LocalDataProvider returning a fixed kline DataFrame."""

    def __init__(self, df):
        self.df = df
        self.calls = []

    def get_daily_kline(self, code, start_date=None, end_date=None):
        self.calls.append((code, start_date, end_date))
        return self.df


def make_kline(n, start="2024-01-01", close_start=1.0, volume=1000.0):
    dates = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "close": [close_start + i for i in range(n)],
            "volume": [volume] * n,
        }
    )


@pytest.fixture()
def calc(tmp_path):
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute(SM_SCHEMA)
    conn.execute(FS_SCHEMA)
    conn.commit()
    conn.close()
    c = CrowdingCalculator(cache_db=str(db))
    return c, str(db)


def insert_sm(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO security_master VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def insert_fs(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO finance_snapshots VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


class TestConstants:
    def test_lookbacks(self):
        assert LOOKBACK_20D == 20
        assert LOOKBACK_60D == 60
        assert MIN_HISTORY_DAYS == 25


class TestOffsetDate:
    def test_offsets(self, calc):
        c, _ = calc
        # plain calendar arithmetic (not trading-day aware); 2024 is a leap
        # year, so 2024-03-13 minus 365 days lands on 2023-03-14
        assert c._offset_date("2024-03-13", -365) == "2023-03-14"
        assert c._offset_date("2024-01-01", 30) == "2024-01-31"


class TestLoadKlineBefore:
    def test_strict_before_filter(self, calc):
        c, _ = calc
        df = make_kline(5, start="2024-03-10")  # 03-10 .. 03-14
        c.local = FakeLocal(df)
        out = c._load_kline_before("600519", "2024-03-13", days=70)
        # 03-13 and 03-14 excluded (strict <)
        assert list(out["date"].dt.strftime("%Y-%m-%d")) == [
            "2024-03-10",
            "2024-03-11",
            "2024-03-12",
        ]

    def test_tail_when_enough_rows(self, calc):
        c, _ = calc
        df = make_kline(100, start="2024-01-01")
        c.local = FakeLocal(df)
        out = c._load_kline_before("600519", "2024-12-31", days=70)
        assert len(out) == 70  # tail(days)

    def test_all_rows_when_fewer_than_days(self, calc):
        c, _ = calc
        df = make_kline(30, start="2024-01-01")
        c.local = FakeLocal(df)
        out = c._load_kline_before("600519", "2024-12-31", days=70)
        assert len(out) == 30  # QUIRK-adjacent (pinned): no tail, returns all

    def test_empty_returns_none(self, calc):
        c, _ = calc
        c.local = FakeLocal(pd.DataFrame({"date": [], "close": [], "volume": []}))
        assert c._load_kline_before("600519", "2024-12-31", days=70) is None


class TestMomentum:
    def test_return_20d_and_60d(self, calc):
        c, _ = calc
        kline = make_kline(70)  # closes 1..70
        from src.thesis.crowding_calculator import CrowdingResult

        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._compute_momentum(r, kline, "2024-12-31")
        assert r.return_20d == pytest.approx(70.0 / 50.0 - 1.0)
        assert r.return_60d == pytest.approx(70.0 / 10.0 - 1.0)

    def test_boundary_21_rows_only_20d(self, calc):
        c, _ = calc
        from src.thesis.crowding_calculator import CrowdingResult

        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._compute_momentum(r, make_kline(21), "2024-12-31")
        assert r.return_20d == pytest.approx(21.0 / 1.0 - 1.0)
        assert r.return_60d is None  # needs >= 61

    def test_20_rows_no_momentum(self, calc):
        c, _ = calc
        from src.thesis.crowding_calculator import CrowdingResult

        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._compute_momentum(r, make_kline(20), "2024-12-31")
        assert r.return_20d is None


class TestVolatility:
    def test_realized_vol_20d(self, calc):
        c, _ = calc
        from src.thesis.crowding_calculator import CrowdingResult

        kline = make_kline(30)  # closes 1..30
        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._compute_volatility(r, kline, "2024-12-31")
        closes = kline["close"].values
        expected = float(np.std(np.diff(np.log(closes[-21:])), ddof=0))
        assert r.realized_vol_20d == pytest.approx(expected)

    def test_insufficient_rows_none(self, calc):
        c, _ = calc
        from src.thesis.crowding_calculator import CrowdingResult

        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._compute_volatility(r, make_kline(20), "2024-12-31")
        assert r.realized_vol_20d is None


class TestVolumeFeatures:
    def test_volume_ratio_and_gap(self, calc):
        c, _ = calc
        from src.thesis.crowding_calculator import CrowdingResult

        kline = make_kline(30)
        kline.loc[kline.index[-1], "volume"] = 3000.0
        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._compute_volume_features(r, kline, "2024-12-31")
        assert r.volume_ratio == pytest.approx(3.0)  # 3000 / mean(20x1000)
        # std of 20 identical 1000s = 0 -> abnormal_volume stays None
        assert r.abnormal_volume is None
        assert r.price_gap == pytest.approx(abs(30.0 / 29.0 - 1.0))

    def test_abnormal_volume_zscore(self, calc):
        c, _ = calc
        from src.thesis.crowding_calculator import CrowdingResult

        kline = make_kline(30)
        # alternating volumes in the 20d window -> nonzero std
        vols = [1000.0 if i % 2 == 0 else 2000.0 for i in range(30)]
        kline["volume"] = vols
        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._compute_volume_features(r, kline, "2024-12-31")
        window = np.array(vols[-21:-1])
        avg = float(np.mean(window))
        std = float(np.std(window, ddof=0))
        assert r.volume_ratio == pytest.approx(vols[-1] / avg)
        assert r.abnormal_volume == pytest.approx((vols[-1] - avg) / std)

    def test_zero_avg_volume_ratio_none(self, calc):
        c, _ = calc
        from src.thesis.crowding_calculator import CrowdingResult

        kline = make_kline(30, volume=0.0)
        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._compute_volume_features(r, kline, "2024-12-31")
        assert r.volume_ratio is None  # avg_vol > 0 guard
        assert r.abnormal_volume is None  # std_vol > 0 guard
        assert r.price_gap is not None  # close-based, unaffected


class TestMarketCap:
    def test_loads_total_and_float_mv(self, calc):
        c, db = calc
        insert_sm(db, [("600519", "600519", "白酒", 2.1e12, 2.0e12)])
        from src.thesis.crowding_calculator import CrowdingResult

        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._load_market_cap(r, "600519")
        assert r.market_cap == 2.1e12
        assert r.float_mcap == 2.0e12

    def test_missing_row_leaves_none(self, calc):
        c, _ = calc
        from src.thesis.crowding_calculator import CrowdingResult

        r = CrowdingResult(security_id="600519", trade_date="2024-12-31")
        c._load_market_cap(r, "999999")
        assert r.market_cap is None
        assert r.float_mcap is None


class TestComputeEndToEnd:
    def test_insufficient_history_short_circuits(self, calc):
        c, _ = calc
        c.local = FakeLocal(make_kline(24))  # < MIN_HISTORY_DAYS
        r = c.compute("600519", "2024-12-31")
        assert r.return_20d is None
        assert r.volume_ratio is None
        assert r.market_cap is None  # not even attempted
        assert r.crowding_score_v1 is None

    def test_full_compute(self, calc):
        c, db = calc
        insert_sm(db, [("600519", "600519", "白酒", 2.1e12, 2.0e12)])
        c.local = FakeLocal(make_kline(80, start="2024-01-01"))
        r = c.compute("600519", "2024-12-31")
        assert r.return_20d is not None
        assert r.return_60d is not None
        assert r.realized_vol_20d is not None
        assert r.volume_ratio == pytest.approx(1.0)  # constant volumes
        assert r.price_gap is not None
        assert r.market_cap == 2.1e12
        # QUIRK (pinned): crowding_score_v1 is ALWAYS None from compute();
        # the cross-sectional composite is only produced by the backfill script
        assert r.crowding_score_v1 is None

    def test_code_zfilled(self, calc):
        c, _ = calc
        fake = FakeLocal(make_kline(30))
        c.local = fake
        c.compute("1", "2024-12-31")
        assert fake.calls[0][0] == "000001"


class TestTurnoverPercentile:
    def test_no_snapshot_returns_none(self, calc):
        c, _ = calc
        assert c.compute_turnover_percentile("600519", "2024-08-29") is None

    def test_fewer_than_10_stocks_none(self, calc):
        c, db = calc
        insert_fs(db, [(f"60000{i}", "2024-08-28", float(i)) for i in range(9)])
        assert c.compute_turnover_percentile("600001", "2024-08-29") is None

    def test_percentile_formula(self, calc):
        c, db = calc
        # turnovers 1..10, my stock = 5 -> count(<=5)=5 -> 50.0
        rows = [(f"60000{i}", "2024-08-28", float(i + 1)) for i in range(10)]
        insert_fs(db, rows)
        assert c.compute_turnover_percentile("600004", "2024-08-29") == pytest.approx(50.0)

    def test_nearest_date_le_trade_date(self, calc):
        c, db = calc
        insert_fs(db, [(f"60000{i}", "2024-08-20", float(i + 1)) for i in range(10)])
        insert_fs(db, [(f"60000{i}", "2024-08-30", 99.0) for i in range(10)])
        # 08-30 snapshot is AFTER trade_date -> must use 08-20
        assert c.compute_turnover_percentile("600009", "2024-08-29") == pytest.approx(100.0)

    def test_null_turnover_excluded(self, calc):
        c, db = calc
        rows = [(f"60000{i}", "2024-08-28", float(i + 1)) for i in range(10)]
        rows.append(("700000", "2024-08-28", None))
        insert_fs(db, rows)
        # NULL row not counted in cross-section (11 rows, 10 non-null)
        assert c.compute_turnover_percentile("600009", "2024-08-29") == pytest.approx(100.0)
