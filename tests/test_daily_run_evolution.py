"""Tests for the evolution <-> decision-loop fixes (2026-07-16 audit closure).

Covers the three items that previously had no test protection:
  1. load_active_agents budget — exploitation (best alpha) + exploration
     (fresh children reserved slots so they are never permanently squeezed out)
  2. KillSwitch.can_evolve() / current_state() gate used by daily_run / cli
  3. _compute_market_regime offline fallback (no index data -> rotation/50)

These guard the "evolution -> decision" closed loop and the real regime wiring
(P1) so a future regression turns the suite red instead of silently passing.
"""

import os
import sqlite3
import tempfile

from src.daily_run import (
    load_active_agents,
    _compute_market_regime,
    _skip_due_to_missing_data,
    MAX_ACTIVE_AGENTS,
)
from src.evolution.risk_genome import KillSwitch
import contextlib


# ── helpers ──────────────────────────────────────────────
class FakeEvalDB:
    """Self-contained stand-in for EvaluationDB.

    load_active_agents only needs `connect()` to return a sqlite connection
    that supports execute()/fetchall()/close(). We pre-create the three tables
    it queries so the test does not depend on EvaluationDB's internal schema.
    """

    def __init__(self, path):
        self.path = path
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE agent_genome_snapshots ("
            "agent_id TEXT, genome_yaml TEXT, birth_date TEXT, status TEXT)"
        )
        conn.execute("CREATE TABLE research_decisions (id INTEGER PRIMARY KEY, agent_id TEXT)")
        conn.execute(
            "CREATE TABLE evaluation_results (research_decision_id INTEGER, alpha_vs_market REAL)"
        )
        conn.commit()
        conn.close()

    def connect(self):
        return sqlite3.connect(self.path)


def _seed_agents(db, n_eval=10, n_fresh=10):
    conn = db.connect()
    try:
        # 10 veterans with a track record; alpha descending E00(best)..E09(worst)
        for i in range(n_eval):
            aid = f"E{i:02d}"
            conn.execute(
                "INSERT INTO agent_genome_snapshots VALUES (?,?,?,?)",
                (aid, "yaml", f"2024-01-{i + 1:02d}", "active"),
            )
            conn.execute("INSERT INTO research_decisions (agent_id) VALUES (?)", (aid,))
            rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO evaluation_results VALUES (?,?)",
                (rid, round(0.9 - i * 0.08, 3)),
            )
        # 10 fresh children, no track record, newest birth_dates
        for j in range(n_fresh):
            cid = f"C{j:02d}"
            conn.execute(
                "INSERT INTO agent_genome_snapshots VALUES (?,?,?,?)",
                (cid, "yaml", f"2026-07-{j + 7:02d}", "active"),
            )
        conn.commit()
    finally:
        conn.close()


# ── 1. load_active_agents budget ────────────────────────
def test_load_active_agents_budget_reserves_exploration():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db = FakeEvalDB(tmp.name)
        _seed_agents(db)
        result = load_active_agents(db, max_agents=MAX_ACTIVE_AGENTS)
        names = [r["name"] for r in result]

        # cap respected
        assert len(result) == MAX_ACTIVE_AGENTS, names
        # top-9 alpha veterans selected (exploitation)
        for e in [f"E{i:02d}" for i in range(9)]:
            assert e in names, f"{e} missing from {names}"
        # lowest-alpha veteran squeezed out by the exploration reservation
        assert "E09" not in names, f"E09 should be squeezed out, got {names}"
        # at least one fresh child gets a slot (exploration reservation)
        fresh = [c for c in names if c.startswith("C")]
        assert fresh, f"no fresh child selected: {names}"
        assert "C09" in names, f"newest child C09 should win an exploration slot: {names}"
    finally:
        os.unlink(tmp.name)


def test_load_active_agents_falls_back_to_yaml_when_empty():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db = FakeEvalDB(tmp.name)  # no rows seeded
        result = load_active_agents(db)
        # DB empty -> falls back to the static YAML founders
        assert len(result) >= 8, f"expected founders fallback, got {len(result)}"
    finally:
        os.unlink(tmp.name)


# ── 2. KillSwitch gate ──────────────────────────────────
def test_killswitch_can_evolve_normal_and_emergency():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        ks = KillSwitch(db_path=tmp.name)
        assert ks.current_state() == "NORMAL"
        assert ks.can_evolve() is True

        ks.trigger_emergency("unit-test")
        assert ks.current_state() == "EMERGENCY"
        assert ks.can_evolve() is False

        ks.resume_normal()
        assert ks.current_state() == "NORMAL"
        assert ks.can_evolve() is True
    finally:
        with contextlib.suppress(Exception):
            ks.db.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)


# ── 3. market regime fallback ───────────────────────────
class _FakeDF:
    def __init__(self, empty):
        self.empty = empty


class _FakeRegime:
    def __init__(self, rt, risk, pe):
        self.regime_type = rt
        self.risk_score = risk
        self.market_pe_percentile = pe


class _FakeMB:
    def __init__(self, regime):
        self._regime = regime

    def classify(self, df):
        return self._regime


def test_compute_market_regime_falls_back_offline():
    empty = _FakeDF(empty=True)
    mb = _FakeMB(_FakeRegime("bull", 20, 0.3))
    rt, risk, pe = _compute_market_regime(empty, empty, mb)
    assert (rt, risk, pe) == ("rotation", 50.0, 0.55)


def test_compute_market_regime_uses_real_classification():
    real = _FakeDF(empty=False)
    empty = _FakeDF(empty=True)
    mb = _FakeMB(_FakeRegime("bull", 20, 0.3))
    rt, risk, pe = _compute_market_regime(real, empty, mb)
    assert (rt, risk, pe) == ("bull", 20.0, 0.3)


# ── 4. no silent mock fallback (P2) ──────────────────────
def test_skip_stock_when_data_missing():
    """Real price data is the only *hard* requirement — never fabricate.

    Contract (post local-data integration): the daily_run loop reads real
    OHLCV from the local TDX .day files, so a stock is skipped ONLY when its
    price series is missing. Missing financials are NOT skipped and NOT
    fabricated — the factor engine simply leaves the value/quality/growth
    families at a neutral 50, so the stock is still ranked honestly on the
    technical factors we actually have. This still guards against the old
    behaviour of injecting hardcoded roe=0.15 / random-walk K-lines.
    """
    empty = _FakeDF(empty=True)
    full = _FakeDF(empty=False)

    # price series missing -> skip (hard requirement), regardless of financials
    assert _skip_due_to_missing_data({"roe": 0.15}, empty) is True
    assert _skip_due_to_missing_data(None, empty) is True
    # price present but financials missing -> do NOT skip (neutralised to 50)
    assert _skip_due_to_missing_data(None, full) is False
    assert _skip_due_to_missing_data({"pe_ttm": 12}, full) is False
    # both present -> do NOT skip (real factors will be computed)
    assert _skip_due_to_missing_data({"roe": 0.15}, full) is False
