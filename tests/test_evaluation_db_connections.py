"""Tests for EvaluationDB connection lifecycle (try/finally leak fix).

The previous code opened a connection per method and called ``conn.close()``
at the end WITHOUT a ``try/finally`` — any SQL error leaked a handle. Every
method now goes through the ``@with_conn`` decorator, which opens the
connection and guarantees ``conn.close()`` in ``finally``.

``evaluation_db.py`` only depends on the standard library, so these tests run
against the real ``sqlite3`` with no external stubs.
"""

import os
import sqlite3
import tempfile
from unittest import mock

from src.data.evaluation_db import EvaluationDB
import src.data.evaluation_crud as _crud_module


class _Cursor:
    lastrowid = 7

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, raise_on_execute=False):
        self.close_calls = 0
        self.commit_calls = 0
        self.raise_on_execute = raise_on_execute
        self.row_factory = None

    def execute(self, *a, **k):
        if self.raise_on_execute:
            raise sqlite3.OperationalError("boom")
        return _Cursor()

    def executescript(self, *a, **k):
        return None

    def commit(self):
        self.commit_calls += 1

    def close(self):
        self.close_calls += 1


def _patch(db, fake):
    """Replace sqlite3.connect inside evaluation_crud with a fake factory."""
    m = mock.MagicMock()
    m.connect.return_value = fake
    return mock.patch.object(_crud_module, "sqlite3", m)


def test_real_roundtrip_init_insert_read():
    """End-to-end on a real temp file: init + insert + read works."""
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "eval.db")
    db = EvaluationDB(db_path)
    db.init_db()
    sid = db.insert_genome_snapshot(
        agent_id="AG1",
        strategy_genus="g",
        strategy_species="s",
        generation=1,
        parent_agent_id="",
        genome_hash="h1",
        genome_yaml="yaml",
        birth_date="2026-01-01",
        status="active",
    )
    assert isinstance(sid, int) and sid >= 1
    snap = db.get_genome_snapshot("AG1", "active")
    assert snap is not None
    assert snap["agent_id"] == "AG1"
    assert snap["genome_hash"] == "h1"


def test_connection_closed_on_success():
    """A normal query closes its connection exactly once."""
    db = EvaluationDB(":memory:")
    fc = _FakeConn()
    with _patch(db, fc):
        db.get_genome_snapshot("X", "active")
    assert fc.close_calls == 1, "connection must be closed after a successful call"
    assert fc.commit_calls == 0


def test_insert_commits_and_closes():
    """An insert commits and closes its connection."""
    db = EvaluationDB(":memory:")
    fc = _FakeConn()
    with _patch(db, fc):
        sid = db.insert_genome_snapshot(
            agent_id="AG1",
            strategy_genus="g",
            strategy_species="s",
            generation=1,
            parent_agent_id="",
            genome_hash="h1",
            genome_yaml="yaml",
            birth_date="2026-01-01",
            status="active",
        )
    assert sid == 7
    assert fc.commit_calls == 1
    assert fc.close_calls == 1


def test_connection_closed_on_exception():
    """CRITICAL: if the query raises, the connection is STILL closed.

    This is the regression test for the original leak — without try/finally a
    raised sqlite3 error would skip ``conn.close()`` and leak the handle.
    """
    db = EvaluationDB(":memory:")
    fc = _FakeConn(raise_on_execute=True)
    raised = False
    with _patch(db, fc):
        try:
            db.get_genome_snapshot("X", "active")
        except sqlite3.OperationalError:
            raised = True
    assert raised, "the DB error must propagate to the caller"
    assert fc.close_calls == 1, "connection MUST be closed even when the query raises"


def test_insert_connection_closed_on_exception():
    """Same guarantee for the write path."""
    db = EvaluationDB(":memory:")
    fc = _FakeConn(raise_on_execute=True)
    raised = False
    with _patch(db, fc):
        try:
            db.insert_genome_snapshot(
                agent_id="AG1",
                strategy_genus="g",
                strategy_species="s",
                generation=1,
                parent_agent_id="",
                genome_hash="h1",
                genome_yaml="yaml",
                birth_date="2026-01-01",
                status="active",
            )
        except sqlite3.OperationalError:
            raised = True
    assert raised
    assert fc.close_calls == 1
