"""Characterization tests for ExpectationGapEngine (6-S.15.2, frozen formula).

RECORD what the module currently does — not what it should do.
The gap formula gap_score = z(EA) - z(PR) is FROZEN (v3.3 Design Freeze);
these tests pin it, never redefine it.

Covered surface:
  - no-event / missing-input paths (data_available, confidence ladder 0.0/0.3/0.5/0.8)
  - distribution window (90d, >=5 rows), caching
  - z-score std=0 -> 0.0
  - frozen formula + percentile
"""

import sqlite3

import numpy as np
import pytest

from src.thesis.expectation_gap import ExpectationGapEngine, ExpectationGapScore

SCHEMA = (
    "CREATE TABLE earnings_event_reaction (security_id TEXT, available_date TEXT, "
    "earnings_acceleration REAL, earnings_acceleration_2nd REAL, "
    "sector_adjusted_t5 REAL, sector_adjusted_t20 REAL, frm_direction TEXT, "
    "earnings_yoy_current REAL, earnings_yoy_previous REAL)"
)


@pytest.fixture()
def engine(tmp_path):
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA)
    # _load_event_reaction falls back to akshare_financials when no eer row;
    # a missing table raises OperationalError (uncaught) — so create it.
    conn.execute(
        "CREATE TABLE akshare_financials (code TEXT, available_date TEXT)"
    )
    conn.commit()
    conn.close()
    return ExpectationGapEngine(cache_db=str(db)), str(db)


def insert(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO earnings_event_reaction VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def row(sid, date, ea, t5, ea2=None, t20=None, frm="improving", yc=0.3, yp=0.2):
    return (sid, date, ea, ea2, t5, t20, frm, yc, yp)


class TestNoDataPaths:
    def test_no_event(self, engine):
        eng, _ = engine
        r = eng.compute("600519", "2024-09-15")
        assert r.data_available is False
        assert r.confidence == 0.0
        assert r.gap_score is None

    def test_missing_ea(self, engine):
        eng, db = engine
        insert(db, [row("600519", "2024-08-30", None, 0.01)])
        r = eng.compute("600519", "2024-09-15")
        assert r.data_available is False
        assert r.confidence == 0.3
        assert r.gap_score is None

    def test_missing_pr(self, engine):
        eng, db = engine
        insert(db, [row("600519", "2024-08-30", 0.10, None)])
        r = eng.compute("600519", "2024-09-15")
        assert r.data_available is False
        assert r.confidence == 0.3

    def test_fields_populated_before_gap(self, engine):
        eng, db = engine
        insert(db, [row("600519", "2024-08-30", 0.10, None, ea2=0.15, frm="improving")])
        r = eng.compute("600519", "2024-09-15")
        assert r.earnings_acceleration == 0.10
        assert r.earnings_acceleration_2nd == 0.15
        assert r.available_date == "2024-08-30"
        assert r.frm_direction == "improving"


class TestDistribution:
    def test_fewer_than_5_rows_fallback_raw(self, engine):
        eng, db = engine
        # only the stock's own row -> distribution <5 -> raw difference, conf 0.5
        insert(db, [row("600519", "2024-08-30", 0.10, 0.03)])
        r = eng.compute("600519", "2024-09-15")
        assert r.data_available is True
        assert r.gap_score == pytest.approx(0.10 - 0.03)
        assert r.confidence == 0.5
        assert r.gap_percentile is None  # <5 rows

    def test_window_90d(self, engine):
        eng, db = engine
        # rows outside 90d window must be excluded from distribution
        rows = [row("600519", "2024-08-30", 0.10, 0.03)]
        rows += [row(f"s{i}", "2024-08-20", 0.05 * i, 0.01 * i) for i in range(1, 6)]
        rows.append(row("old", "2024-01-01", 99.0, 99.0))  # >90d before trade_date
        insert(db, rows)
        r = eng.compute("600519", "2024-09-15")
        dist = eng._get_distribution("2024-09-15")
        assert dist["n"] == 6  # the 99.0 outlier excluded
        assert r.confidence == 0.8

    def test_distribution_cached(self, engine):
        eng, db = engine
        rows = [row(f"s{i}", "2024-08-20", 0.05 * i, 0.01 * i) for i in range(1, 7)]
        insert(db, rows)
        d1 = eng._get_distribution("2024-09-15")
        # mutate db; cached value must not change
        insert(db, [row("sx", "2024-08-21", 100.0, 100.0)])
        d2 = eng._get_distribution("2024-09-15")
        assert d1 is d2


class TestFrozenFormula:
    def seed_universe(self, db):
        # 6 stocks with distinct EA/PR
        rows = [row(f"s{i}", "2024-08-20", 0.02 * i, 0.005 * i) for i in range(1, 7)]
        rows.append(row("600519", "2024-08-30", 0.20, -0.01))
        insert(db, rows)

    def test_z_gap(self, engine):
        eng, db = engine
        self.seed_universe(db)
        r = eng.compute("600519", "2024-09-15")
        dist = eng._get_distribution("2024-09-15")
        z_ea = (0.20 - dist["ea_mean"]) / dist["ea_std"]
        z_pr = (-0.01 - dist["pr_mean"]) / dist["pr_std"]
        assert r.gap_score == pytest.approx(z_ea - z_pr)
        assert r.confidence == 0.8
        # high EA + negative PR -> strongly positive gap
        assert r.gap_score > 1.0

    def test_std_zero_guard_dead_for_identical_floats(self, engine):
        # FINDING (pinned, not fixed): with all EA identical, np.std returns a
        # float residue (~7e-18), NOT 0.0 — so the `std == 0` guard does NOT
        # fire and z_ea explodes to ~O(1) instead of 0.0.
        eng, db = engine
        rows = [row(f"s{i}", "2024-08-20", 0.05, 0.01 * i) for i in range(1, 6)]
        rows.append(row("600519", "2024-08-30", 0.05, 0.02))
        insert(db, rows)
        r = eng.compute("600519", "2024-09-15")
        dist = eng._get_distribution("2024-09-15")
        assert dist["ea_std"] > 0  # residue, guard bypassed
        z_ea = (0.05 - dist["ea_mean"]) / dist["ea_std"]
        z_pr = (0.02 - dist["pr_mean"]) / dist["pr_std"]
        assert z_ea != 0.0  # the quirk: identical inputs, non-zero z
        assert r.gap_score == pytest.approx(z_ea - z_pr)

    def test_percentile(self, engine):
        eng, db = engine
        self.seed_universe(db)
        r = eng.compute("600519", "2024-09-15")
        # recompute all gaps and count <= ours
        dist = eng._get_distribution("2024-09-15")
        rows = [(0.02 * i, 0.005 * i) for i in range(1, 7)] + [(0.20, -0.01)]
        gaps = np.array([
            (ea - dist["ea_mean"]) / dist["ea_std"] - (pr - dist["pr_mean"]) / dist["pr_std"]
            for ea, pr in rows
        ])
        expected = float(np.mean(gaps <= r.gap_score))
        assert r.gap_percentile == pytest.approx(expected)
        # our stock has the highest EA and lowest PR -> top percentile
        assert r.gap_percentile == pytest.approx(1.0)

    def test_code_zfilled(self, engine):
        eng, db = engine
        insert(db, [row("000519", "2024-08-30", 0.10, 0.03)])
        r = eng.compute(519, "2024-09-15")
        assert r.security_id == "000519"

    def test_to_dict(self, engine):
        eng, _ = engine
        r = eng.compute("600519", "2024-09-15")
        d = r.to_dict()
        assert isinstance(d, dict)
        assert d["security_id"] == "600519"
        assert d["data_available"] is False
