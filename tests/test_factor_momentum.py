"""Characterization tests for FactorMomentumEngine (6-Q.5a).

RECORD what the module currently does — not what it should do.

Covered surface:
  - constants, FactorClimate strongest/weakest
  - _get_past_snapshot_dates / _get_eval_date / _read_regime fallbacks
  - compute_factor_climate: <3 dates empty, rolling windows, win_rate, no-data zeros
  - _compute_ls_returns_for_date: <20 rows None, L-S sign with fake kline
"""

import sqlite3

import pytest

from src.thesis.factor_momentum import FactorClimate, FactorMomentumEngine


@pytest.fixture()
def engine(tmp_path):
    ev = tmp_path / "eval.db"
    cache = tmp_path / "cache.db"
    e = sqlite3.connect(ev)
    e.execute(
        "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, "
        "quality_score REAL, value_score REAL, growth_score REAL, "
        "momentum_score REAL, risk_score REAL, sentiment_score REAL)"
    )
    e.execute("CREATE TABLE market_regime_snapshots (obs_date TEXT, regime_type TEXT)")
    e.commit()
    e.close()
    c = sqlite3.connect(cache)
    c.execute("CREATE TABLE trading_calendar (trade_date TEXT, is_trading INTEGER)")
    c.commit()
    c.close()
    eng = FactorMomentumEngine(eval_db=str(ev), cache_db=str(cache))
    return eng, str(ev), str(cache)


def seed_snapshots(ev, date, scores):
    """scores: list of (sid, quality) tuples; other factors = quality too."""
    conn = sqlite3.connect(ev)
    conn.executemany(
        "INSERT INTO stock_factor_snapshot VALUES (?,?,?,?,?,?,?,?)",
        [(sid, date, q, q, q, q, q, q) for sid, q in scores],
    )
    conn.commit()
    conn.close()


class TestConstants:
    def test_values(self, engine):
        eng, _, _ = engine
        assert eng.FACTOR_FAMILIES == ["quality", "value", "growth", "momentum", "risk", "sentiment"]
        assert eng.LS_PERCENTILE == 0.20
        assert eng.HORIZON == 20


class TestFactorClimate:
    def test_strongest_weakest(self):
        fc = FactorClimate(date="d", factors={
            "value": {"momentum_60d": 0.08},
            "growth": {"momentum_60d": -0.03},
        })
        assert fc.get_strongest_factor() == "value"
        assert fc.get_weakest_factor() == "growth"
        assert fc.get_strongest_factor("missing_window") == "value"  # .get default 0 tie

    def test_empty_neutral(self):
        fc = FactorClimate(date="d")
        assert fc.get_strongest_factor() == "neutral"
        assert fc.get_weakest_factor() == "neutral"


class TestHelpers:
    def test_past_snapshot_dates(self, engine):
        eng, ev, _ = engine
        seed_snapshots(ev, "2026-01-05", [("a", 50)])
        seed_snapshots(ev, "2026-01-10", [("a", 50)])
        seed_snapshots(ev, "2026-01-15", [("a", 50)])
        got = eng._get_past_snapshot_dates("2026-01-15", 120)  # limit 120//5=24
        assert got == ["2026-01-10", "2026-01-05"]  # strict <, DESC

    def test_get_eval_date(self, engine):
        eng, _, cache = engine
        conn = sqlite3.connect(cache)
        conn.executemany(
            "INSERT INTO trading_calendar VALUES (?,1)",
            [(f"2026-01-{d:02d}",) for d in range(1, 26)],
        )
        conn.commit()
        conn.close()
        # OFFSET horizon-1 = 19 -> 20th trading day AFTER 01-01 (strict >)
        assert eng._get_eval_date("2026-01-01", 20) == "2026-01-21"
        assert eng._get_eval_date("2026-01-10", 20) is None  # not enough days

    def test_read_regime_fallbacks(self, engine):
        eng, ev, _ = engine
        assert eng._read_regime("2026-01-15") == "sideway"  # no rows
        conn = sqlite3.connect(ev)
        conn.execute("INSERT INTO market_regime_snapshots VALUES ('2026-01-10','bull')")
        conn.commit()
        conn.close()
        assert eng._read_regime("2026-01-10") == "bull"  # exact
        assert eng._read_regime("2026-01-15") == "bull"  # nearest prior
        assert eng._read_regime("2026-01-05") == "sideway"  # nothing before


class TestClimateComputation:
    def test_fewer_than_3_dates_empty(self, engine):
        eng, ev, _ = engine
        seed_snapshots(ev, "2026-01-05", [("a", 50)])
        fc = eng.compute_factor_climate("2026-02-01")
        assert fc.factors == {}
        assert fc.market_regime == "sideway"

    def seed_full(self, engine, ev, cache):
        eng = engine
        # 4 past dates, 25 stocks each (>= 20), plus calendar
        for di, d in enumerate(["2025-12-01", "2025-12-08", "2025-12-15", "2025-12-22"]):
            scores = [(f"s{i:02d}", float(i * 4)) for i in range(25)]
            seed_snapshots(ev, d, scores)
        conn = sqlite3.connect(cache)
        conn.executemany(
            "INSERT INTO trading_calendar VALUES (?,1)",
            [(f"2025-12-{d:02d}",) for d in range(1, 32)]
            + [(f"2026-01-{d:02d}",) for d in range(1, 32)],
        )
        conn.commit()
        conn.close()
        # fake kline: return depends on code index -> top scores positive, bottom negative
        def fake_kline(code, start, end):
            idx = int(code[1:])
            ret = 0.01 * (idx - 12)  # s00: -0.12 ... s24: +0.12
            import pandas as pd

            return pd.DataFrame({"close": [100.0, 100.0 * (1 + ret)]})

        eng.local = type("L", (), {"get_daily_kline": staticmethod(fake_kline)})()
        return eng

    def test_ls_sign_and_climate(self, engine):
        eng, ev, cache = engine
        eng = self.seed_full(eng, ev, cache)
        ls = eng._compute_ls_returns_for_date("2025-12-01")
        # QUIRK (pinned): s00 has quality=0.0 -> `row[i+1] or 50` treats 0.0 as
        # missing -> score 50 -> s00 jumps OUT of the bottom group.
        # bottom5 = s01..s05, top5 = s20..s24:
        top_mean = sum(0.01 * (i - 12) for i in range(20, 25)) / 5
        bottom_mean = sum(0.01 * (i - 12) for i in range(1, 6)) / 5
        expected = top_mean - bottom_mean
        for f in eng.FACTOR_FAMILIES:
            assert ls[f] == pytest.approx(expected)
        assert expected > 0  # high scores outperform

    def test_climate_rolling_windows(self, engine):
        eng, ev, cache = engine
        eng = self.seed_full(eng, ev, cache)
        fc = eng.compute_factor_climate("2026-01-15")
        q = fc.factors["quality"]
        # 4 past dates all produce the same L-S -> all windows equal
        ls = eng._compute_ls_returns_for_date("2025-12-01")["quality"]
        assert q["momentum_20d"] == pytest.approx(ls)
        assert q["momentum_60d"] == pytest.approx(ls)
        assert q["momentum_120d"] == pytest.approx(ls)
        assert q["n_samples"] == 4
        assert q["win_rate"] == 1.0

    def test_fewer_than_20_rows_none(self, engine):
        eng, ev, cache = engine
        seed_snapshots(ev, "2025-12-01", [(f"s{i}", 50.0) for i in range(10)])
        ls = eng._compute_ls_returns_for_date("2025-12-01")
        assert all(v is None for v in ls.values())
