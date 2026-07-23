"""Characterization tests for RecoveryConfidence (Market Guardian core, FROZEN v1.1).

These tests RECORD what the frozen overlay currently does — they do not prescribe
what it should do. Pinned behavior is the CODE (6-S.5.5b), not the stale module
docstring (which still describes the superseded 0.4/0.3/0.3 weights and 30/50/70
thresholds — see REMEDIATION.md findings).

Pinned surface:
  - Composite formula: confidence = 0.10*breadth + 0.50*vol_repair + 0.40*trend
  - Band edges: <55 blocked(0.0) / 55-65 small(0.3) / 65-75 normal(0.6) / >=75 full(1.0)
  - allows_anomaly True only for normal/full bands
  - Sub-score formulas (breadth / vol_repair / trend_confirm) incl. neutral defaults
"""

import sqlite3
from datetime import date, timedelta

import pytest

from src.thesis.confidence_overlay import RecoveryConfidence

TRADE_DATE = "2026-07-01"


@pytest.fixture()
def rc(tmp_path):
    return RecoveryConfidence(
        eval_db=str(tmp_path / "eval.db"), cache_db=str(tmp_path / "cache.db")
    )


def patch_subscores(monkeypatch, rc, breadth=50.0, vol=50.0, trend=50.0):
    monkeypatch.setattr(rc, "_compute_breadth_recovery", lambda d: breadth)
    monkeypatch.setattr(rc, "_compute_vol_repair", lambda d: vol)
    monkeypatch.setattr(rc, "_compute_trend_confirm", lambda d: trend)


def seed_index(cache_db: str, closes: list[float], end: str = TRADE_DATE):
    """Seed consecutive daily closes ending at `end`."""
    conn = sqlite3.connect(cache_db)
    conn.executescript(
        "CREATE TABLE market_index_daily (index_code TEXT, trade_date TEXT, close REAL, amount REAL);"
    )
    end_d = date.fromisoformat(end)
    for i, c in enumerate(closes):
        d = end_d - timedelta(days=len(closes) - 1 - i)
        conn.execute(
            "INSERT INTO market_index_daily VALUES ('000300', ?, ?, 1e9)",
            (d.isoformat(), c),
        )
    conn.commit()
    conn.close()


# ── Composite formula & band edges ─────────────────────────


class TestCompositeAndBands:
    def test_composite_weights(self, monkeypatch, rc):
        # confidence = 0.10*b + 0.50*v + 0.40*t
        patch_subscores(monkeypatch, rc, breadth=80.0, vol=60.0, trend=40.0)
        result = rc.compute(TRADE_DATE)
        assert result.confidence == pytest.approx(0.10 * 80 + 0.50 * 60 + 0.40 * 40)

    @pytest.mark.parametrize(
        "vol,expected_conf,band,weight,allows",
        [
            (59.8, 54.9, "blocked", 0.0, False),
            (60.0, 55.0, "small", 0.3, False),
            (79.8, 64.9, "small", 0.3, False),
            (80.0, 65.0, "normal", 0.6, True),
            (99.8, 74.9, "normal", 0.6, True),
            (100.0, 75.0, "full", 1.0, True),
        ]
    )
    def test_band_edges(self, monkeypatch, rc, vol, expected_conf, band, weight, allows):
        # breadth=trend=50 -> base 25, confidence = 25 + 0.5*vol
        patch_subscores(monkeypatch, rc, breadth=50.0, vol=vol, trend=50.0)
        result = rc.compute(TRADE_DATE)
        assert result.confidence == pytest.approx(expected_conf, abs=1e-6)
        assert result.confidence_band == band
        assert result.anomaly_weight == weight
        assert result.allows_anomaly is allows
        assert band in result.reason

    def test_to_dict_roundtrip(self, monkeypatch, rc):
        patch_subscores(monkeypatch, rc, breadth=50.0, vol=80.0, trend=50.0)
        d = rc.compute(TRADE_DATE).to_dict()
        assert d["date"] == TRADE_DATE
        assert d["confidence"] == 65.0
        assert d["band"] == "normal"
        assert d["allows_anomaly"] is True


# ── Breadth recovery sub-score ─────────────────────────────


class TestBreadthRecovery:
    def test_formula_with_snapshot(self, rc, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "eval.db"))
        conn.executescript(
            """
            CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, momentum_score REAL);
            INSERT INTO stock_factor_snapshot VALUES
                ('a', '2026-07-01', 90),  -- >50 and >80
                ('b', '2026-07-01', 60),  -- >50
                ('c', '2026-07-01', 40),
                ('d', '2026-07-01', 30);
            """
        )
        conn.commit()
        conn.close()
        # advance_ratio=0.5 -> base 50; high_ratio=0.25 -> bonus min(20, 50)=20
        assert rc._compute_breadth_recovery(TRADE_DATE) == pytest.approx(70.0)

    def test_neutral_when_date_has_no_rows(self, rc, tmp_path):
        # Table exists but no rows for the date -> neutral 50.0
        conn = sqlite3.connect(str(tmp_path / "eval.db"))
        conn.executescript(
            "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, momentum_score REAL);"
        )
        conn.commit()
        conn.close()
        assert rc._compute_breadth_recovery(TRADE_DATE) == 50.0

    def test_missing_table_raises_operational_error(self, rc):
        # Observed frozen behavior: unlike state_transition._get_breadth, this
        # method has try/finally but NO except — a missing table propagates
        # sqlite3.OperationalError instead of falling back to neutral.
        with pytest.raises(sqlite3.OperationalError):
            rc._compute_breadth_recovery("2099-01-01")

    def test_bonus_capped_at_20(self, rc, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "eval.db"))
        conn.executescript(
            """
            CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, momentum_score REAL);
            INSERT INTO stock_factor_snapshot VALUES
                ('a', '2026-07-01', 90), ('b', '2026-07-01', 85);
            """
        )
        conn.commit()
        conn.close()
        # base 100 + bonus min(20, 100*200)=20 -> clamp 100
        assert rc._compute_breadth_recovery(TRADE_DATE) == 100.0


# ── Vol repair sub-score ───────────────────────────────────


class TestVolRepair:
    def test_neutral_when_insufficient_history(self, rc, tmp_path):
        seed_index(str(tmp_path / "cache.db"), [100.0 + i for i in range(20)])
        assert rc._compute_vol_repair(TRADE_DATE) == 50.0

    def test_calm_market_scores_100(self, rc, tmp_path):
        # 60 days of identical +0.1% returns -> vol=0 -> level 100, change 0
        closes = []
        price = 100.0
        for _ in range(60):
            price *= 1.001
            closes.append(price)
        seed_index(str(tmp_path / "cache.db"), closes)
        assert rc._compute_vol_repair(TRADE_DATE) == pytest.approx(100.0)

    def test_volatile_market_scores_0(self, rc, tmp_path):
        # 60 days of ±3% alternating -> vol_20d ~0.476 -> level clamped 0, change ~0
        closes = []
        price = 100.0
        for i in range(60):
            price *= 1.03 if i % 2 == 0 else 0.97
            closes.append(price)
        seed_index(str(tmp_path / "cache.db"), closes)
        assert rc._compute_vol_repair(TRADE_DATE) == pytest.approx(0.0, abs=1.0)


# ── Trend confirm sub-score ────────────────────────────────


class TestTrendConfirm:
    def test_neutral_when_insufficient_history(self, rc, tmp_path):
        seed_index(str(tmp_path / "cache.db"), [100.0 + i for i in range(10)])
        assert rc._compute_trend_confirm(TRADE_DATE) == 50.0

    def test_linear_uptrend_clamps_at_100(self, rc, tmp_path):
        # 60 days linear +1/day: trend=(159-129.5)/129.5≈0.228 -> 50+45.6+10(ma20>ma60) >100
        closes = [100.0 + i for i in range(60)]
        seed_index(str(tmp_path / "cache.db"), closes)
        assert rc._compute_trend_confirm(TRADE_DATE) == 100.0

    def test_linear_downtrend(self, rc, tmp_path):
        # 60 days linear -1/day: trend=(100-129.5)/129.5≈-0.228 -> 50-45.6≈4.4, no bonus
        closes = [159.0 - i for i in range(60)]
        seed_index(str(tmp_path / "cache.db"), closes)
        assert rc._compute_trend_confirm(TRADE_DATE) == pytest.approx(4.4, abs=0.1)

    def test_ma20_above_ma60_bonus(self, rc, tmp_path):
        # Flat then recent rise: ma20 > ma60 adds +10
        closes = [100.0] * 40 + [100.0 + i * 0.5 for i in range(20)]
        seed_index(str(tmp_path / "cache.db"), closes)
        score = rc._compute_trend_confirm(TRADE_DATE)
        # current=109.5, ma60=mean(all 60)≈101.58 -> trend≈0.0779
        # score = 50 + 0.0779*200 + 10 ≈ 75.59
        assert score == pytest.approx(75.59, abs=0.01)
