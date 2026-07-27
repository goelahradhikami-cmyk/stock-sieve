"""Characterization tests for ThesisLedger + KillCriteria (6-S.3, FROZEN kills).

RECORD what the module currently does — not what it should do.
Kill thresholds are frozen doctrine logic; these tests pin them.

Covered surface:
  - KillCriteria: all 4 kills, thresholds, None guards, order priority
  - record_thesis: id format, verdict extraction, KILLED/REVIEWED
  - record_outcome: status mapping (None/>0/<=0)
  - classify_failure taxonomy order
  - get_stats aggregates + division guards
"""

import sqlite3

import pytest

from src.thesis.market_anomaly import MispricingObject
from src.thesis.market_recovery import MarketState
from src.thesis.thesis_ledger import KillCriteria, ThesisLedger

SCHEMA = """
CREATE TABLE thesis_ledger (
    thesis_id TEXT PRIMARY KEY, trade_date TEXT, eval_date TEXT, code TEXT,
    anomaly_type TEXT, price_drawdown_12m REAL, roe REAL, margin_change REAL,
    market_pessimism REAL, business_strength REAL, divergence_score REAL,
    recovery_probability REAL, market_regime TEXT,
    quality_verdict TEXT, quality_confidence REAL,
    contrarian_verdict TEXT, contrarian_confidence REAL,
    value_verdict TEXT, value_confidence REAL,
    consensus TEXT, kill_criteria_triggered TEXT,
    action TEXT, thesis_status TEXT,
    actual_return REAL, failure_type TEXT, failure_reason TEXT
)
"""


@pytest.fixture()
def ledger(tmp_path):
    db = tmp_path / "eval.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    return ThesisLedger(eval_db=str(db)), str(db)


def make_anomaly(**kw) -> MispricingObject:
    base = dict(code="600000", trade_date="2023-01-31")
    base.update(kw)
    return MispricingObject(**base)


class TestKillCriteria:
    def test_no_kill_default(self):
        r = KillCriteria.check(make_anomaly())
        assert r.killed is False
        assert r.kill_reason == "" and r.kill_doctrine == ""

    def test_kill1_quality_deterioration(self):
        a = make_anomaly(roe=0.04, margin_change=-0.06, debt_ratio=2.1)
        r = KillCriteria.check(a)
        assert r.killed and r.kill_doctrine == "quality_compounder"
        assert "quality_deterioration" in r.kill_reason
        # boundaries: roe < 0.05, margin < -0.05, debt > 2.0 (all strict)
        assert (
            KillCriteria.check(make_anomaly(roe=0.05, margin_change=-0.06, debt_ratio=2.1)).killed
            is False
        )
        assert (
            KillCriteria.check(make_anomaly(roe=0.04, margin_change=-0.05, debt_ratio=2.1)).killed
            is False
        )
        assert (
            KillCriteria.check(make_anomaly(roe=0.04, margin_change=-0.06, debt_ratio=2.0)).killed
            is False
        )

    def test_kill2_value_trap(self):
        a = make_anomaly(pe_compression=0.95, margin_change=-0.04)
        r = KillCriteria.check(a)
        assert r.killed and r.kill_doctrine == "value_purist"
        assert (
            KillCriteria.check(make_anomaly(pe_compression=0.9, margin_change=-0.04)).killed
            is False
        )  # > 0.9 strict

    def test_kill3_falling_knife(self):
        # roe=0.10 keeps kill1 (roe<0.05) and kill4 (roe>0.10 strict) out of play
        a = make_anomaly(price_drawdown_12m=-0.31, margin_change=-0.11, debt_ratio=2.6, roe=0.10)
        r = KillCriteria.check(a)
        assert r.killed and r.kill_doctrine == "contrarian"
        assert (
            KillCriteria.check(
                make_anomaly(
                    price_drawdown_12m=-0.30, margin_change=-0.11, debt_ratio=2.6, roe=0.10
                )
            ).killed
            is False
        )  # < -0.30 strict

    def test_kill4_cashflow_divergence(self):
        a = make_anomaly(roe=0.15, cashflow_trend=-0.35)
        r = KillCriteria.check(a)
        assert r.killed and r.kill_doctrine == "quality_compounder"
        assert "cashflow_divergence" in r.kill_reason
        assert (
            KillCriteria.check(make_anomaly(roe=0.10, cashflow_trend=-0.35)).killed is False
        )  # roe > 0.10 strict

    def test_none_fields_skip_kills(self):
        # margin/debt/pe/cashflow None -> kills 1-4 all guarded off
        a = make_anomaly(
            roe=0.01,
            margin_change=None,
            debt_ratio=None,
            pe_compression=None,
            cashflow_trend=None,
            price_drawdown_12m=-0.9,
        )
        assert KillCriteria.check(a).killed is False

    def test_order_priority(self):
        # matches kill1 AND kill3 -> kill1 wins (checked first)
        a = make_anomaly(roe=0.04, margin_change=-0.11, debt_ratio=2.6, price_drawdown_12m=-0.31)
        r = KillCriteria.check(a)
        assert "quality_deterioration" in r.kill_reason


class TestRecordThesis:
    def test_id_and_columns(self, ledger):
        lg, db = ledger
        from src.thesis.doctrine_underwriting import UnderwritingResult

        anomaly = make_anomaly(
            price_drawdown_12m=-0.3,
            roe=0.12,
            margin_change=0.02,
            market_pessimism=0.7,
            business_strength=0.6,
            divergence_score=0.3,
            divergence_type="cyclical_misjudgment",
        )
        state = MarketState(date="2023-01-31", recovery_probability=0.65, state_label="recovering")
        uw = {
            "quality_compounder": UnderwritingResult(
                "quality_compounder", "PASS", 0.75, [], [], {}
            ),
        }
        kill = KillCriteria.check(anomaly)
        tid = lg.record_thesis(anomaly, state, uw, kill, "BUY", eval_date="2023-02-28")
        assert tid == "T20230131_600000"
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT * FROM thesis_ledger WHERE thesis_id=?", (tid,)).fetchone()
        cols = [d[0] for d in conn.execute("SELECT * FROM thesis_ledger LIMIT 0").description]
        rec = dict(zip(cols, row, strict=False))
        conn.close()
        assert rec["quality_verdict"] == "PASS"
        assert rec["quality_confidence"] == 0.75
        assert rec["contrarian_verdict"] is None  # missing doctrine -> None
        assert rec["consensus"] == "REVIEWED"
        assert rec["kill_criteria_triggered"] is None
        assert rec["thesis_status"] == "pending"
        assert rec["action"] == "BUY"

    def test_killed_records_reason(self, ledger):
        lg, db = ledger
        anomaly = make_anomaly(roe=0.04, margin_change=-0.06, debt_ratio=2.1)
        state = MarketState(date="2023-01-31")
        kill = KillCriteria.check(anomaly)
        tid = lg.record_thesis(anomaly, state, {}, kill, "REJECT")
        conn = sqlite3.connect(db)
        rec = conn.execute(
            "SELECT consensus, kill_criteria_triggered FROM thesis_ledger WHERE thesis_id=?",
            (tid,),
        ).fetchone()
        conn.close()
        assert rec[0] == "KILLED"
        assert "quality_deterioration" in rec[1]


class TestRecordOutcome:
    def seed(self, lg):
        anomaly = make_anomaly()
        state = MarketState(date="2023-01-31")
        return lg.record_thesis(anomaly, state, {}, KillCriteria.check(anomaly), "BUY")

    def test_status_mapping(self, ledger):
        lg, db = ledger
        tid = self.seed(lg)
        conn = sqlite3.connect(db)
        lg.record_outcome(tid, 0.05)
        assert conn.execute("SELECT thesis_status FROM thesis_ledger").fetchone()[0] == "validated"
        lg.record_outcome(tid, -0.05)
        assert conn.execute("SELECT thesis_status FROM thesis_ledger").fetchone()[0] == "failed"
        lg.record_outcome(tid, None)
        assert conn.execute("SELECT thesis_status FROM thesis_ledger").fetchone()[0] == "no_data"
        lg.record_outcome(tid, 0.0)
        assert conn.execute("SELECT thesis_status FROM thesis_ledger").fetchone()[0] == "failed"
        conn.close()


class TestClassifyFailure:
    def seed(self, lg, strength, margin):
        anomaly = make_anomaly(business_strength=strength, margin_change=margin)
        state = MarketState(date="2023-01-31")
        return lg.record_thesis(anomaly, state, {}, KillCriteria.check(anomaly), "BUY")

    def test_taxonomy_order(self, ledger):
        lg, _ = ledger
        tid = self.seed(lg, strength=0.3, margin=-0.05)
        # macro_risk first regardless of other conditions
        assert lg.classify_failure(tid, -0.1, "panic", 0.3) == "macro_risk"
        # then value_trap (strength < 0.4)
        assert lg.classify_failure(tid, -0.1, "x", 0.45) == "value_trap"
        # strength ok -> earnings_deterioration (margin < -0.03)
        tid2 = self.seed(lg, strength=0.6, margin=-0.05)
        assert lg.classify_failure(tid2, -0.1, "x", 0.45) == "earnings_deterioration"
        # margin ok -> timing_error (0.4 <= prob <= 0.5)
        tid3 = self.seed(lg, strength=0.6, margin=0.0)
        assert lg.classify_failure(tid3, -0.1, "x", 0.45) == "timing_error"
        # high prob -> unknown
        assert lg.classify_failure(tid3, -0.1, "x", 0.6) == "unknown"

    def test_missing_row_unknown(self, ledger):
        lg, _ = ledger
        assert lg.classify_failure("T_none", -0.1, "x", 0.45) == "unknown"


class TestGetStats:
    def test_empty_db(self, ledger):
        lg, _ = ledger
        s = lg.get_stats()
        assert s["total"] == 0
        assert s["validation_rate"] == 0
        assert s["avg_buy_return"] is None
        assert s["doctrine_accuracy"]["quality"] == {"pass_count": 0, "pass_win_rate": 0}

    def test_aggregates(self, ledger):
        lg, db = ledger
        from src.thesis.doctrine_underwriting import UnderwritingResult

        state = MarketState(date="2023-01-31")
        uw = {
            "quality_compounder": UnderwritingResult("quality_compounder", "PASS", 1.0, [], [], {})
        }
        # two BUY theses: one validated, one failed with type
        for i, ret in [(0, 0.10), (1, -0.05)]:
            a = make_anomaly(code=f"60000{i}", trade_date="2023-01-31")
            tid = lg.record_thesis(a, state, uw, KillCriteria.check(a), "BUY")
            lg.record_outcome(tid, ret, failure_type="value_trap" if ret < 0 else None)
        # one killed REJECT
        a3 = make_anomaly(
            code="600002", trade_date="2023-01-31", roe=0.04, margin_change=-0.06, debt_ratio=2.1
        )
        lg.record_thesis(a3, state, {}, KillCriteria.check(a3), "REJECT")
        s = lg.get_stats()
        assert s["total"] == 3
        assert s["validated"] == 1
        assert s["failed"] == 1
        assert s["killed"] == 1
        assert s["validation_rate"] == pytest.approx(1 / 3)
        assert s["failure_types"] == {"value_trap": 1}
        assert s["avg_buy_return"] == pytest.approx(0.025)
        assert s["avg_reject_return"] is None  # killed one has no outcome
        assert s["doctrine_accuracy"]["quality"]["pass_count"] == 2
        assert s["doctrine_accuracy"]["quality"]["pass_win_rate"] == pytest.approx(0.5)
