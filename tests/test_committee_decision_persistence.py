"""Tests for committee_decisions persistence — chairman_score column mapping.

Guards against the earlier defect where the `chairman_score` column received a
hardcoded `weighted_score` (mislabeled "兼容列"), so a real, distinct chairman
score could never be persisted and the column's semantics were confusing.

In this system the chairman's synthesized score IS `weighted_score`
(`chairman_decision()` returns it), so `chairman_score` legitimately mirrors it
today. The fix makes the mapping explicit and forward-compatible: if a decision
carries its own `chairman_score`, that value is persisted; otherwise it falls
back to `weighted_score`.
"""

import os
import tempfile

from src.data.evaluation_db import EvaluationDB
import contextlib

_INSERT_COLUMNS = (
    "committee_id",
    "research_decision_id",
    "valuation_score",
    "industry_score",
    "risk_score",
    "quant_score",
    "devil_advocate_score",
    "chairman_score",
    "weighted_score",
    "verdict",
    "verdict_reason",
    "position_cap_modifier",
    "confidence_modifier",
    "monitoring_flags",
    "required_conditions_json",
    "member_statements_json",
    "devil_advocate_attack",
    "debate_transcript",
)

_CREATE = """
CREATE TABLE committee_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    committee_id TEXT NOT NULL UNIQUE,
    research_decision_id INTEGER NOT NULL,
    valuation_score REAL,
    industry_score REAL,
    risk_score REAL,
    quant_score REAL,
    devil_advocate_score REAL,
    chairman_score REAL,
    weighted_score REAL,
    verdict TEXT,
    verdict_reason TEXT,
    position_cap_modifier REAL,
    confidence_modifier REAL,
    monitoring_flags TEXT,
    required_conditions_json TEXT,
    member_statements_json TEXT,
    devil_advocate_attack TEXT,
    debate_transcript TEXT
)
"""


def _make_db(path):
    db = EvaluationDB(path)
    conn = db.connect()
    conn.execute("DROP TABLE IF EXISTS committee_decisions")
    conn.execute(_CREATE)
    conn.commit()
    conn.close()
    return db


def _base_decision(**overrides):
    d = {
        "committee_id": "cm-001",
        "research_decision_id": 1,
        "valuation_score": 70.0,
        "industry_score": 65.0,
        "risk_score": 80.0,
        "quant_score": 60.0,
        "devil_advocate_score": 55.0,
        "weighted_score": 72.3,
        "verdict": "APPROVE_WITH_CONDITIONS",
        "verdict_reason": "ok",
    }
    d.update(overrides)
    return d


def test_chairman_score_mirrors_weighted_when_absent():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        pass
    try:
        db = _make_db(tmp.name)
        db.insert_committee_decision(_base_decision())
        conn = db.connect()
        row = conn.execute(
            "SELECT chairman_score, weighted_score FROM committee_decisions "
            "WHERE committee_id='cm-001'"
        ).fetchone()
        conn.close()
        # chairman_score column must hold the chairman's score (== weighted today)
        assert row["chairman_score"] == 72.3, dict(row)
        assert row["weighted_score"] == 72.3, dict(row)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)


def test_chairman_score_uses_distinct_value_when_present():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        pass
    try:
        db = _make_db(tmp.name)
        db.insert_committee_decision(
            _base_decision(
                committee_id="cm-002",
                research_decision_id=2,
                weighted_score=70.0,
                chairman_score=88.0,  # distinct chairman override
                verdict="APPROVE",
            )
        )
        conn = db.connect()
        row = conn.execute(
            "SELECT chairman_score, weighted_score FROM committee_decisions "
            "WHERE committee_id='cm-002'"
        ).fetchone()
        conn.close()
        # forward-compat: a real chairman_score is persisted as-is, not overwritten
        assert row["chairman_score"] == 88.0, dict(row)
        assert row["weighted_score"] == 70.0, dict(row)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)
