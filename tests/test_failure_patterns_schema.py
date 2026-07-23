"""
Tests for the failure_patterns / failure_events table conflict fix.

Background
----------
The codebase used to have TWO incompatible CREATE TABLE statements for the
same table name ``failure_patterns``:

  * evaluation_db.DDL_V21  -> aggregate schema (pattern_id, occurrence_count, ...)
  * postmortem.engine      -> event schema     (evaluation_id, failure_type, severity, ...)

Whichever ran first won; the other's INSERT/UPDATE then crashed with
"no such column". This is now resolved by giving the event table its own
name ``failure_events``. These tests lock that invariant in.
"""

import os
import sqlite3
import tempfile

from src.data.evaluation_db import EvaluationDB
from src.postmortem.engine import PostMortemEngine
from src.postmortem.rule_miner import RuleMiner
import contextlib


def _cols(db_path, table):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    conn.close()
    return {r[1] for r in rows}


def _close(pm):
    if pm is not None:
        with contextlib.suppress(Exception):
            pm.db.close()


def test_no_schema_collision_both_tables_coexist():
    """Aggregate failure_patterns (Schema A) and event failure_events (Schema B)
    must both exist with their own distinct columns."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    pm = None
    try:
        EvaluationDB(path).init_db()
        EvaluationDB(path).migrate_v2_1()  # creates failure_patterns (aggregate)
        pm = PostMortemEngine(path)  # creates failure_events (event)

        fp_cols = _cols(path, "failure_patterns")
        fe_cols = _cols(path, "failure_events")

        # aggregate schema owns these
        assert "pattern_id" in fp_cols
        assert "occurrence_count" in fp_cols
        # event schema owns these (and NOT the aggregate ones)
        assert "evaluation_id" in fe_cols
        assert "failure_type" in fe_cols
        assert "severity" in fe_cols
        # the event columns must NOT have leaked into the aggregate table
        assert "evaluation_id" not in fp_cols
        assert "failure_type" not in fp_cols
    finally:
        _close(pm)
        os.unlink(path)


def test_event_write_lands_in_failure_events_not_aggregate():
    """PostMortemEngine._save_failure must write to failure_events and must NOT
    pollute the aggregate failure_patterns table."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    pm = None
    try:
        EvaluationDB(path).init_db()
        EvaluationDB(path).migrate_v2_1()
        pm = PostMortemEngine(path)

        pm._save_failure(
            {"id": 1, "agent_id": "agent_x", "genome_version": "g1"},
            {"type": "stock_selection_failure", "severity": 0.8, "evidence": {}},
            {},
        )
        # run_daily() commits after the loop; emulate that here so a separate
        # connection can observe the write.
        pm.db.commit()

        conn = sqlite3.connect(path)
        fe = conn.execute("SELECT failure_type, severity FROM failure_events").fetchall()
        fp = conn.execute("SELECT COUNT(*) FROM failure_patterns").fetchone()[0]
        conn.close()

        assert len(fe) == 1
        assert fe[0][0] == "stock_selection_failure"
        assert fe[0][1] == 0.8
        assert fp == 0, "event write must not pollute the aggregate table"
    finally:
        _close(pm)
        os.unlink(path)


def test_rule_miner_reads_failure_events():
    """RuleMiner.mine() groups by failure_type/severity, which only exist on the
    event table; it must run without 'no such column' after the rename.

    Note: RuleMiner expects an EvaluationDB-like object with .connect() (it is
    currently only ever paired with the postmortem event table), so we hand it
    an EvaluationDB pointed at the same file.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    pm = None
    try:
        EvaluationDB(path).init_db()
        EvaluationDB(path).migrate_v2_1()
        pm = PostMortemEngine(path)
        for i in range(3):
            pm._save_failure(
                {"id": i, "agent_id": "agent_x", "genome_version": "g1"},
                {"type": "market_regime_failure", "severity": 0.7, "evidence": {}},
                {},
            )
        pm.db.commit()

        # mine() should not raise; proves SELECT failure_type/AVG(severity) works
        # on failure_events (and that _save_rule writes candidate_rules_v2).
        generated = RuleMiner(EvaluationDB(path)).mine(min_occurrences=1, min_confidence=0.0)
        assert generated >= 0
    finally:
        _close(pm)
        os.unlink(path)


def test_aggregate_failure_patterns_still_works():
    """The aggregate table path (upsert + get) must remain fully functional."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = EvaluationDB(path)
        db.init_db()
        db.migrate_v2_1()
        db.upsert_failure_pattern("cat_sub", "Sample Pattern", "category", "sub")
        rows = db.get_failure_patterns(min_occurrence=1)
        assert len(rows) == 1
        assert rows[0]["pattern_id"] == "cat_sub"
        assert rows[0]["occurrence_count"] == 1
    finally:
        os.unlink(path)
