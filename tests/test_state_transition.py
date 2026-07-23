"""Characterization tests for StateTransitionEngine (Market Guardian core, FROZEN v1.1).

These tests RECORD what the frozen engine currently does — they do not prescribe
what it should do. If an assertion fails after a code change, the change altered
frozen behavior and must be justified explicitly.

Covered surface (all pure-logic; DB access via seeded tmp sqlite):
  - State constants & anomaly weights
  - _classify_day: all 6 branches + branch priority
  - Transition rules: upgrade needs 3 consecutive confirmations, downgrade immediate
  - get_state nearest-prior fallback, allows_anomaly threshold
  - _get_breadth from snapshot / 0.5 default
  - get_state_distribution / get_transitions on injected history
"""

import sqlite3

import pytest

from src.thesis.state_transition import (
    STATE_ANOMALY_WEIGHT,
    STATE_ORDER,
    STATES,
    UPGRADE_CONFIRMATION_DAYS,
    StateRecord,
    StateTransitionEngine,
)


@pytest.fixture()
def engine(tmp_path):
    return StateTransitionEngine(
        eval_db=str(tmp_path / "eval.db"), cache_db=str(tmp_path / "cache.db")
    )


def make_record(date: str, state: str) -> StateRecord:
    return StateRecord(
        date=date,
        state=state,
        anomaly_weight=STATE_ANOMALY_WEIGHT[state],
        vol_20d=0.2,
        vol_change=0.0,
        trend=0.0,
        recovery_prob=0.5,
        breadth=0.5,
        previous_state=state,
        transition_reason="test",
        confirmation_count=0,
    )


# ── Constants ──────────────────────────────────────────────


class TestConstants:
    def test_state_order(self):
        assert STATES == [
            "PANIC",
            "STABILIZING",
            "EARLY_RECOVERY",
            "CONFIRMED_RECOVERY",
            "EUPHORIA",
        ]
        assert STATE_ORDER["PANIC"] == 0
        assert STATE_ORDER["EUPHORIA"] == 4

    def test_anomaly_weights_pinned(self):
        assert STATE_ANOMALY_WEIGHT == {
            "PANIC": 0.0,
            "STABILIZING": 0.2,
            "EARLY_RECOVERY": 0.6,
            "CONFIRMED_RECOVERY": 1.0,
            "EUPHORIA": 0.3,
        }

    def test_upgrade_confirmation_days(self):
        assert UPGRADE_CONFIRMATION_DAYS == 3


# ── _classify_day (pure classification) ────────────────────


class TestClassifyDay:
    def test_euphoria(self, engine):
        state, reason = engine._classify_day(
            vol_20d=0.10, vol_change=-0.05, trend=0.20,
            recovery_prob=0.80, breadth=0.50, current_state="PANIC",
        )
        assert state == "EUPHORIA"
        assert "overbought" in reason

    def test_confirmed_recovery(self, engine):
        state, _ = engine._classify_day(
            vol_20d=0.20, vol_change=-0.05, trend=0.05,
            recovery_prob=0.60, breadth=0.50, current_state="PANIC",
        )
        assert state == "CONFIRMED_RECOVERY"

    def test_early_recovery(self, engine):
        state, _ = engine._classify_day(
            vol_20d=0.20, vol_change=-0.02, trend=0.01,
            recovery_prob=0.50, breadth=0.42, current_state="PANIC",
        )
        assert state == "EARLY_RECOVERY"

    def test_stabilizing(self, engine):
        state, _ = engine._classify_day(
            vol_20d=0.20, vol_change=-0.005, trend=0.0,
            recovery_prob=0.40, breadth=0.38, current_state="PANIC",
        )
        assert state == "STABILIZING"

    def test_panic_high_vol(self, engine):
        state, _ = engine._classify_day(
            vol_20d=0.30, vol_change=0.01, trend=-0.05,
            recovery_prob=0.30, breadth=0.30, current_state="EUPHORIA",
        )
        assert state == "PANIC"

    def test_panic_vol_expanding_weak_breadth(self, engine):
        state, _ = engine._classify_day(
            vol_20d=0.20, vol_change=0.03, trend=0.0,
            recovery_prob=0.40, breadth=0.30, current_state="EUPHORIA",
        )
        assert state == "PANIC"

    def test_default_maintains_current(self, engine):
        state, reason = engine._classify_day(
            vol_20d=0.20, vol_change=0.0, trend=0.0,
            recovery_prob=0.45, breadth=0.30, current_state="EARLY_RECOVERY",
        )
        assert state == "EARLY_RECOVERY"
        assert "maintaining" in reason

    def test_euphoria_checked_before_confirmed_recovery(self, engine):
        # Indicators satisfy BOTH EUPHORIA and CONFIRMED_RECOVERY;
        # EUPHORIA wins because it is checked first.
        state, _ = engine._classify_day(
            vol_20d=0.10, vol_change=-0.05, trend=0.20,
            recovery_prob=0.80, breadth=0.50, current_state="PANIC",
        )
        assert state == "EUPHORIA"


# ── get_state / allows_anomaly ─────────────────────────────


class TestStateQuery:
    def test_exact_match(self, engine):
        engine._state_history["2026-07-01"] = make_record("2026-07-01", "EUPHORIA")
        assert engine.get_state("2026-07-01").state == "EUPHORIA"

    def test_nearest_prior_date_fallback(self, engine):
        engine._state_history["2026-07-01"] = make_record("2026-07-01", "PANIC")
        engine._state_history["2026-07-10"] = make_record("2026-07-10", "STABILIZING")
        # 07-05 not in history -> falls back to 07-01
        assert engine.get_state("2026-07-05").state == "PANIC"
        assert engine.get_state("2026-07-15").state == "STABILIZING"

    def test_before_history_returns_none(self, engine):
        engine._state_history["2026-07-01"] = make_record("2026-07-01", "PANIC")
        assert engine.get_state("2026-06-01") is None

    def test_allows_anomaly_threshold(self, engine):
        engine._state_history["d1"] = make_record("d1", "PANIC")  # 0.0
        engine._state_history["d2"] = make_record("d2", "STABILIZING")  # 0.2
        engine._state_history["d3"] = make_record("d3", "EARLY_RECOVERY")  # 0.6
        engine._state_history["d4"] = make_record("d4", "CONFIRMED_RECOVERY")  # 1.0
        engine._state_history["d5"] = make_record("d5", "EUPHORIA")  # 0.3
        assert engine.allows_anomaly("d1") is False
        assert engine.allows_anomaly("d2") is False
        assert engine.allows_anomaly("d3") is True   # 0.6 >= 0.5
        assert engine.allows_anomaly("d4") is True
        assert engine.allows_anomaly("d5") is False  # euphoria blocked
        assert engine.allows_anomaly("unknown") is False


# ── run() transition rules (synthetic price series) ────────

CACHE_SCHEMA = """
CREATE TABLE market_index_daily (
    index_code TEXT, trade_date TEXT, close REAL, amount REAL
);
"""


def seed_index(cache_db: str, closes: list[float]):
    conn = sqlite3.connect(cache_db)
    conn.executescript(CACHE_SCHEMA)
    for i, c in enumerate(closes, 1):
        conn.execute(
            "INSERT INTO market_index_daily VALUES ('000300', ?, ?, 1e9)",
            (f"2026-01-{i:02d}" if i <= 31 else f"2026-02-{i - 31:02d}", c),
        )
    conn.commit()
    conn.close()


def synth_series() -> list[float]:
    """40 volatile days (±3% alternating) -> 80 calm days (+0.2%/day) -> 10 crash days."""
    closes = []
    price = 100.0
    for i in range(40):  # volatile, mild upward drift
        price *= 1.03 if i % 2 == 0 else 0.972
        closes.append(price)
    for _ in range(80):  # calm steady rise
        price *= 1.002
        closes.append(price)
    for i in range(10):  # high-vol crash
        price *= 0.96 if i % 2 == 0 else 1.01
        closes.append(price)
    return closes


class TestTransitionRules:
    def test_run_requires_60_rows(self, engine, tmp_path):
        seed_index(str(tmp_path / "cache.db"), [100.0 + i for i in range(50)])
        assert engine.run("2026-01-01", "2026-12-31") == 0
        assert engine._state_history == {}

    def test_upgrade_requires_3_confirmations(self, engine, tmp_path):
        seed_index(str(tmp_path / "cache.db"), synth_series())
        n = engine.run("2026-01-01", "2026-12-31")
        assert n == len(synth_series()) - 60

        records = [engine._state_history[d] for d in sorted(engine._state_history)]
        # Initial state is PANIC; the first two calm days only accumulate
        # confirmation, the third day executes the upgrade.
        assert records[0].state == "PANIC"
        assert "upgrade signal" in records[0].transition_reason
        assert records[0].confirmation_count == 1
        assert records[1].state == "PANIC"
        assert records[1].confirmation_count == 2
        assert records[2].state != "PANIC"
        assert "UPGRADED" in records[2].transition_reason
        assert records[2].confirmation_count == 0  # reset after upgrade

    def test_downgrade_is_immediate(self, engine, tmp_path):
        seed_index(str(tmp_path / "cache.db"), synth_series())
        engine.run("2026-01-01", "2026-12-31")
        records = [engine._state_history[d] for d in sorted(engine._state_history)]

        # Downgrades require no confirmation: the first record whose target
        # deteriorates changes state immediately. Observed frozen behavior:
        # the crash phase first downgrades to STABILIZING (vol_20d has not yet
        # crossed 0.25 on day 1), then escalates to PANIC as crash days
        # accumulate and vol_20d exceeds 0.25.
        downgraded = [r for r in records if "DOWNGRADED" in r.transition_reason]
        assert downgraded, "expected at least one downgrade during crash phase"
        first = downgraded[0]
        assert first.state == "STABILIZING"
        assert first.confirmation_count == 0
        # Escalation: PANIC is reached within the 10-day crash phase
        assert any(r.state == "PANIC" for r in records[-10:])

    def test_invariants_hold_across_full_run(self, engine, tmp_path):
        seed_index(str(tmp_path / "cache.db"), synth_series())
        engine.run("2026-01-01", "2026-12-31")
        for r in engine._state_history.values():
            assert r.state in STATES
            assert r.anomaly_weight == STATE_ANOMALY_WEIGHT[r.state]
            assert r.to_dict()["state"] == r.state  # serialization round-trip


# ── _get_breadth ───────────────────────────────────────────


class TestBreadth:
    def test_breadth_from_snapshot(self, engine, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "eval.db"))
        conn.executescript(
            """
            CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, momentum_score REAL);
            INSERT INTO stock_factor_snapshot VALUES
                ('a', '2026-07-01', 60), ('b', '2026-07-01', 40),
                ('c', '2026-07-01', 70), ('d', '2026-07-01', 30);
            """
        )
        conn.commit()
        conn.close()
        # 2 of 4 above 50 -> 0.5
        assert engine._get_breadth("2026-07-01") == pytest.approx(0.5)

    def test_breadth_default_when_no_data(self, engine):
        assert engine._get_breadth("2099-01-01") == 0.5


# ── distribution / transitions ─────────────────────────────


class TestHistoryAnalysis:
    def test_state_distribution(self, engine):
        engine._state_history = {
            "d1": make_record("d1", "PANIC"),
            "d2": make_record("d2", "PANIC"),
            "d3": make_record("d3", "EUPHORIA"),
        }
        assert engine.get_state_distribution() == {"PANIC": 2, "EUPHORIA": 1}

    def test_get_transitions_only_on_change(self, engine):
        engine._state_history = {
            "d1": make_record("d1", "PANIC"),
            "d2": make_record("d2", "PANIC"),      # no change
            "d3": make_record("d3", "STABILIZING"),  # change
            "d4": make_record("d4", "EUPHORIA"),     # change
            "d5": make_record("d5", "EUPHORIA"),     # no change
        }
        transitions = engine.get_transitions()
        assert [t.date for t in transitions] == ["d3", "d4"]
