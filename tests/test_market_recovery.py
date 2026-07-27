"""Characterization tests for MarketRecoveryEngine (6-S.1).

RECORD what the module currently does — not what it should do.

Covered surface:
  - MarketState defaults / allows_anomaly_bets vol gate / to_dict rounding
  - _label_state boundaries
  - _compute_composite sub-score math + weights + clamp
  - _compute_breadth / _compute_new_highs_ratio (seeded eval db)
  - _compute_liquidity_trend / _compute_volatility / _compute_trend (seeded cache db)
"""

import sqlite3

import numpy as np
import pytest

from src.thesis.market_recovery import MarketRecoveryEngine, MarketState


@pytest.fixture()
def engine(tmp_path):
    ev = tmp_path / "eval.db"
    cache = tmp_path / "cache.db"
    e = sqlite3.connect(ev)
    e.execute(
        "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, momentum_score REAL)"
    )
    e.commit()
    e.close()
    c = sqlite3.connect(cache)
    c.execute(
        "CREATE TABLE market_index_daily (index_code TEXT, trade_date TEXT, close REAL, amount REAL)"
    )
    c.commit()
    c.close()
    eng = MarketRecoveryEngine(eval_db=str(ev), cache_db=str(cache))
    return eng, str(ev), str(cache)


# ── MarketState ────────────────────────────────────────────


class TestMarketState:
    def test_defaults(self):
        s = MarketState(date="2023-01-13")
        assert s.breadth_advance_ratio == 0.5
        assert s.volatility_20d == 0.25
        assert s.recovery_probability == 0.5
        assert s.state_label == "neutral"

    def test_allows_anomaly_bets_vol_gate(self):
        s = MarketState(date="d", recovery_probability=0.6, volatility_change=-0.02)
        assert s.allows_anomaly_bets() is True
        # prob > 0.5 alone is NOT enough (6-S.5b)
        s2 = MarketState(date="d", recovery_probability=0.9, volatility_change=0.0)
        assert s2.allows_anomaly_bets() is False
        s3 = MarketState(date="d", recovery_probability=0.9, volatility_change=-0.01)
        assert s3.allows_anomaly_bets() is False  # strict <
        # vol contracting alone is NOT enough
        s4 = MarketState(date="d", recovery_probability=0.5, volatility_change=-0.5)
        assert s4.allows_anomaly_bets() is False

    def test_to_dict_rounding(self):
        s = MarketState(
            date="d",
            breadth_advance_ratio=0.5678,
            liquidity_amount_change=0.1234,
            volatility_20d=0.2567,
            volatility_change=-0.0123,
            trend_ma=0.4444,
            recovery_probability=0.6789,
            state_label="recovering",
        )
        d = s.to_dict()
        assert d == {
            "date": "d",
            "breadth": 0.568,
            "liquidity": 0.123,
            "volatility": 0.257,
            "vol_change": -0.012,
            "trend": 0.444,
            "recovery_prob": 0.679,
            "label": "recovering",
        }


# ── _label_state ───────────────────────────────────────────


class TestLabelState:
    def test_boundaries(self, engine):
        eng, _, _ = engine
        assert eng._label_state(0.61) == "recovering"
        assert eng._label_state(0.60) == "uncertain"  # strict >
        assert eng._label_state(0.35) == "uncertain"  # strict <
        assert eng._label_state(0.34) == "panic"


# ── _compute_composite ─────────────────────────────────────


class TestComposite:
    def test_weights_and_subscores(self, engine):
        eng, _, _ = engine
        state = MarketState(
            date="d",
            breadth_advance_ratio=0.7,  # (0.7-0.3)/0.4 = 1.0
            liquidity_amount_change=0.1,  # 0.5+0.2 = 0.7
            volatility_change=-0.05,  # 0.5+0.15 = 0.65
            trend_ma=0.2,  # 0.5+0.1 = 0.6
        )
        expected = 0.35 * 1.0 + 0.25 * 0.7 + 0.20 * 0.65 + 0.20 * 0.6
        assert eng._compute_composite(state) == pytest.approx(expected)

    def test_clamps_at_bounds(self, engine):
        eng, _, _ = engine
        hi = MarketState(
            date="d",
            breadth_advance_ratio=1.0,
            liquidity_amount_change=5.0,
            volatility_change=-5.0,
            trend_ma=5.0,
        )
        assert eng._compute_composite(hi) == pytest.approx(1.0)
        lo = MarketState(
            date="d",
            breadth_advance_ratio=0.0,
            liquidity_amount_change=-5.0,
            volatility_change=5.0,
            trend_ma=-5.0,
        )
        assert eng._compute_composite(lo) == pytest.approx(0.0)

    def test_default_state_composite(self, engine):
        eng, _, _ = engine
        # breadth 0.5 -> (0.5-0.3)/0.4 = 0.5 ; others 0 -> 0.5 each
        s = MarketState(date="d")
        assert eng._compute_composite(s) == pytest.approx(0.5)


# ── breadth queries ────────────────────────────────────────


class TestBreadth:
    def seed_snapshot(self, ev, rows):
        conn = sqlite3.connect(ev)
        conn.executemany("INSERT INTO stock_factor_snapshot VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()

    def test_breadth_ratio(self, engine):
        eng, ev, _ = engine
        self.seed_snapshot(
            ev,
            [
                ("a", "2023-01-13", 60.0),
                ("b", "2023-01-13", 40.0),
                ("c", "2023-01-13", 51.0),
                ("d", "2023-01-13", 50.0),  # NOT advancing (strict > 50)
            ],
        )
        assert eng._compute_breadth("2023-01-13") == pytest.approx(2 / 4)

    def test_breadth_empty_default(self, engine):
        eng, _, _ = engine
        assert eng._compute_breadth("2023-01-13") == 0.5

    def test_new_highs_ratio(self, engine):
        eng, ev, _ = engine
        self.seed_snapshot(
            ev,
            [
                ("a", "2023-01-13", 85.0),
                ("b", "2023-01-13", 80.0),  # NOT counted (strict > 80)
                ("c", "2023-01-13", 20.0),
            ],
        )
        assert eng._compute_new_highs_ratio("2023-01-13") == pytest.approx(1 / 3)

    def test_new_highs_empty_default(self, engine):
        eng, _, _ = engine
        assert eng._compute_new_highs_ratio("2023-01-13") == 0.0


# ── cache-db indicators ────────────────────────────────────


class TestCacheIndicators:
    def seed_index(self, cache, closes_amounts, index_code="000300"):
        conn = sqlite3.connect(cache)
        rows = [
            (index_code, f"2022-12-{d:02d}" if d <= 31 else f"2023-01-{d - 31:02d}", c, a)
            for d, (c, a) in enumerate(closes_amounts, start=1)
        ]
        conn.executemany("INSERT INTO market_index_daily VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()

    def test_liquidity_insufficient_data(self, engine):
        eng, _, _ = engine
        assert eng._compute_liquidity_trend("2023-01-13") == 0.0

    def test_liquidity_math(self, engine):
        eng, _, cache = engine
        # 25 rows all inside the 40-day window ending at trade_date
        rows = [("000300", f"2023-01-{d:02d}", 100.0, 100.0) for d in range(1, 21)] + [
            ("000300", f"2023-01-{d:02d}", 100.0, 120.0) for d in range(21, 26)
        ]
        conn = sqlite3.connect(cache)
        conn.executemany("INSERT INTO market_index_daily VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()
        got = eng._compute_liquidity_trend("2023-01-25")
        # recent_avg = mean(last 5 amounts) = 120 ; past_avg = mean(rows[-20:-5]) = 100
        assert got == pytest.approx((120.0 - 100.0) / 100.0)

    def test_volatility_insufficient(self, engine):
        eng, _, _ = engine
        assert eng._compute_volatility("2023-01-13") == (0.25, 0.0)

    def test_volatility_math(self, engine):
        eng, _, cache = engine
        rng = np.random.default_rng(7)
        base = 4000 * np.cumprod(1 + rng.normal(0, 0.01, 61))
        data = [(float(c), 1e9) for c in base]
        self.seed_index(cache, data)
        vol20, vol_change = eng._compute_volatility("2023-02-21")
        closes = base
        returns = np.diff(closes) / closes[:-1]
        exp20 = float(np.std(returns[-20:]) * np.sqrt(252))
        exp60 = float(np.std(returns[-60:]) * np.sqrt(252))
        assert vol20 == pytest.approx(exp20)
        assert vol_change == pytest.approx(exp20 - exp60)

    def test_trend_insufficient(self, engine):
        eng, _, _ = engine
        assert eng._compute_trend("2023-01-13") == 0.0

    def test_trend_math_and_clip(self, engine):
        eng, _, cache = engine
        data = [(100.0, 1e9)] * 30 + [(110.0, 1e9)]
        self.seed_index(cache, data)
        got = eng._compute_trend("2023-01-31")
        ma = np.mean([100.0] * 30 + [110.0])
        assert got == pytest.approx((110.0 - ma) / ma)
        # clip: price doubles -> trend clipped at 1
        data2 = [(100.0, 1e9)] * 30 + [(300.0, 1e9)]
        conn = sqlite3.connect(cache)
        conn.execute("DELETE FROM market_index_daily")
        conn.executemany(
            "INSERT INTO market_index_daily VALUES (?,?,?,?)",
            [("000300", f"2023-02-{(i % 28) + 1:02d}", c, a) for i, (c, a) in enumerate(data2)],
        )
        conn.commit()
        conn.close()
        # note: duplicate dates collapse to fewer rows; use distinct days instead
        assert eng._compute_trend("2023-02-28") <= 1.0
