"""Characterization tests for ThesisSignalEngine (6-Q.1).

RECORD what the module currently does — not what it should do.

Covered surface:
  - ThesisSignals.to_dict / weight constants / code zfill
  - _compute_acceleration: row-count gates, delta math, confidence
  - _compute_catalyst: row-count gates, growth math, zero-denominator guards
  - compute_signals composite: mispricing gap, weighted score, confidence avg
  - compute_signals_batch: per-code exception swallow
"""

import sqlite3

import pytest

from src.thesis.signal_engine import ThesisSignalEngine, ThesisSignals

SCHEMA = (
    "CREATE TABLE akshare_financials (code TEXT, report_date TEXT, "
    "revenue_yoy REAL, earnings_yoy REAL, roe REAL, total_assets REAL, equity REAL)"
)


@pytest.fixture()
def engine(tmp_path):
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    return ThesisSignalEngine(cache_db=str(db)), str(db)


def insert(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO akshare_financials VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


# ── dataclass & constants ──────────────────────────────────


class TestDataclass:
    def test_defaults(self):
        s = ThesisSignals(code="600519")
        assert s.fundamental_acceleration == 0.0
        assert s.mispricing_gap == 0.0
        assert s.catalyst_proxy == 0.0
        assert s.thesis_score == 0.0
        assert s.confidence == 0.0

    def test_to_dict(self):
        s = ThesisSignals(code="600519", fundamental_acceleration=0.1)
        d = s.to_dict()
        assert d == {
            "code": "600519",
            "fundamental_acceleration": 0.1,
            "mispricing_gap": 0.0,
            "catalyst_proxy": 0.0,
            "thesis_score": 0.0,
            "confidence": 0.0,
        }

    def test_weights(self):
        assert ThesisSignalEngine.W_ACCELERATION == 0.4
        assert ThesisSignalEngine.W_MISPRICING == 0.4
        assert ThesisSignalEngine.W_CATALYST == 0.2


# ── _compute_acceleration ──────────────────────────────────


class TestAcceleration:
    def test_fewer_than_3_rows(self, engine):
        eng, db = engine
        insert(db, [("600519", "2023-03-31", 10.0, 5.0, 0.1, None, None)])
        accel, conf = eng._compute_acceleration("600519", "2023-06-30")
        assert accel is None and conf == 0.0

    def test_delta_math(self, engine):
        eng, db = engine
        # rows DESC by report_date: [0]=latest
        rows = [
            ("600519", "2023-03-31", 20.0, 12.0, 0.15, None, None),
            ("600519", "2022-12-31", 15.0, 10.0, 0.12, None, None),
            ("600519", "2022-09-30", 10.0, 8.0, 0.10, None, None),
        ]
        insert(db, rows)
        accel, conf = eng._compute_acceleration("600519", "2023-06-30")
        # deltas: rev (20-10)/100=0.10 ; earn (12-8)/100=0.04 ; roe 0.15-0.10=0.05
        assert accel == pytest.approx((0.10 + 0.04 + 0.05) / 3)
        assert conf == pytest.approx(3 / 4.0)  # 3 rows / 4

    def test_null_fields_skipped(self, engine):
        eng, db = engine
        rows = [
            ("600519", "2023-03-31", None, 12.0, 0.15, None, None),
            ("600519", "2022-12-31", None, 10.0, 0.12, None, None),
            ("600519", "2022-09-30", None, 8.0, 0.10, None, None),
        ]
        insert(db, rows)
        accel, _ = eng._compute_acceleration("600519", "2023-06-30")
        # only earn + roe deltas: (0.04 + 0.05) / 2
        assert accel == pytest.approx((0.04 + 0.05) / 2)

    def test_all_null_returns_none(self, engine):
        eng, db = engine
        rows = [
            ("600519", "2023-03-31", None, None, None, None, None),
            ("600519", "2022-12-31", None, None, None, None, None),
            ("600519", "2022-09-30", None, None, None, None, None),
        ]
        insert(db, rows)
        accel, conf = eng._compute_acceleration("600519", "2023-06-30")
        assert accel is None and conf == 0.0


# ── _compute_catalyst ──────────────────────────────────────


class TestCatalyst:
    def test_fewer_than_2_rows(self, engine):
        eng, db = engine
        accel, conf = eng._compute_catalyst("600519", "2023-06-30")
        assert accel is None and conf == 0.0

    def test_growth_math(self, engine):
        eng, db = engine
        rows = [
            ("600519", "2023-03-31", None, None, None, 110.0, 55.0),
            ("600519", "2022-03-31", None, None, None, 100.0, 50.0),
        ]
        insert(db, rows)
        cat, conf = eng._compute_catalyst("600519", "2023-06-30")
        # asset_growth = 0.10, equity_growth = 0.10 -> catalyst = 0.10
        assert cat == pytest.approx(0.10)
        assert conf == pytest.approx(2 / 4.0)

    def test_zero_denominator_guard(self, engine):
        eng, db = engine
        rows = [
            ("600519", "2023-03-31", None, None, None, 110.0, 55.0),
            ("600519", "2022-03-31", None, None, None, 0.0, None),
        ]
        insert(db, rows)
        cat, _ = eng._compute_catalyst("600519", "2023-06-30")
        assert cat == pytest.approx(0.0)  # both growths guarded to 0.0


# ── compute_signals composite ──────────────────────────────


class TestComputeSignals:
    def seed(self, db):
        rows = [
            ("600519", "2023-03-31", 20.0, 12.0, 0.15, 110.0, 55.0),
            ("600519", "2022-12-31", 15.0, 10.0, 0.12, 105.0, 52.0),
            ("600519", "2022-09-30", 10.0, 8.0, 0.10, 100.0, 50.0),
        ]
        insert(db, rows)

    def test_composite_math(self, engine):
        eng, db = engine
        self.seed(db)
        s = eng.compute_signals("600519", "2023-06-30", price_performance=0.05)
        accel = (0.10 + 0.04 + 0.05) / 3
        mispricing = accel - 0.05
        catalyst = ((110 - 100) / 100 + (55 - 50) / 50) / 2
        assert s.fundamental_acceleration == pytest.approx(accel)
        assert s.mispricing_gap == pytest.approx(mispricing)
        assert s.catalyst_proxy == pytest.approx(catalyst)
        assert s.thesis_score == pytest.approx(0.4 * accel + 0.4 * mispricing + 0.2 * catalyst)
        # confidence = (3/4 + 3/4) / 2
        assert s.confidence == pytest.approx(0.75)

    def test_code_zfilled(self, engine):
        eng, db = engine
        self.seed(db)
        s = eng.compute_signals(600519, "2023-06-30", price_performance=0.0)
        assert s.code == "600519"

    def test_missing_data_zeros(self, engine):
        eng, db = engine
        s = eng.compute_signals("000001", "2023-06-30", price_performance=0.02)
        assert s.fundamental_acceleration == 0.0
        # actual behavior: accel None -> mispricing = 0.0 (price_perf ignored)
        assert s.mispricing_gap == 0.0
        assert s.catalyst_proxy == 0.0
        assert s.confidence == 0.0


class TestBatch:
    def test_exception_swallowed_per_code(self, tmp_path):
        # engine pointing at db WITHOUT the table -> OperationalError per code
        eng = ThesisSignalEngine(cache_db=str(tmp_path / "empty.db"))
        out = eng.compute_signals_batch(["600519", "000001"], "2023-06-30")
        assert out == {}
