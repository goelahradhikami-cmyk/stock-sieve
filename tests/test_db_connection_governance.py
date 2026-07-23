"""Connection governance: managed_connect guarantees closure on owner GC,
and close_all() flushes the tracked registry. No heavy deps required.
"""

import gc
import importlib.util
import os
import sqlite3
import sys
import tempfile


def _load_db_module():
    # Load db.py directly (it imports only stdlib) to avoid triggering the
    # heavier src.data package __init__ (which needs pandas).
    path = os.path.join(os.path.dirname(__file__), "..", "src", "data", "db.py")
    spec = importlib.util.spec_from_file_location("stock_sieve_db_gov", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["stock_sieve_db_gov"] = m
    spec.loader.exec_module(m)
    return m


_db = _load_db_module()
managed_connect = _db.managed_connect
close_all = _db.close_all


class _Owner:
    """A stand-in for any component that holds a long-lived connection."""


def _tmp_db():
    d = tempfile.mkdtemp()
    return os.path.join(d, "t.db")


def _assert_closed(conn):
    assert conn not in _db._REGISTRY, "closed connection must be dropped from registry"
    try:
        conn.execute("SELECT 1")
        raise AssertionError("expected closed-database error")
    except sqlite3.ProgrammingError:
        pass  # "Cannot operate on a closed database" — correctly closed


def test_managed_connect_basic_usage_works():
    owner = _Owner()  # owner must stay referenced for the connection to live
    conn = managed_connect(owner, _tmp_db())
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    conn.close()


def test_connection_closed_when_owner_collected():
    """The core guarantee: owner GC'd -> connection closed (no __del__ reliance)."""
    owner = _Owner()
    conn = managed_connect(owner, _tmp_db())
    assert conn in _db._REGISTRY
    del owner
    gc.collect()
    _assert_closed(conn)


def test_close_all_flushes_registry():
    owner = _Owner()
    conn = managed_connect(owner, _tmp_db())
    assert conn in _db._REGISTRY
    close_all()
    _assert_closed(conn)
    assert len(_db._REGISTRY) == 0


def test_double_close_is_safe():
    """close_all then owner GC must not raise on the already-closed handle."""
    owner = _Owner()
    conn = managed_connect(owner, _tmp_db())
    close_all()
    _assert_closed(conn)
    del owner
    gc.collect()  # finalize fires _safe_close again on the closed connection
    # reached here without a hard crash => safe


def test_timeout_arg_passthrough():
    owner = _Owner()
    conn = managed_connect(owner, _tmp_db(), timeout=10)
    assert conn in _db._REGISTRY
    conn.close()
