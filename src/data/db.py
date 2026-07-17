"""Centralized SQLite connection governance.

Long-lived components used to open ``self.db = sqlite3.connect(...)`` in their
constructor and rely on CPython's (unreliable) ``__del__`` to close the handle.
That leaks handles in long-running pipelines and leaves WAL/shared locks behind.

``managed_connect`` replaces those raw calls:

* it still returns a live connection owned by ``owner``;
* it registers a ``weakref.finalize`` safety net so the handle is closed when
  ``owner`` is garbage-collected (robust against reference cycles that would
  otherwise delay/defeat ``__del__``); and
* it tracks every connection in a process-wide registry that ``close_all`` can
  flush at the end of a pipeline run.

This does NOT change connection semantics — it only guarantees closure.
"""
import sqlite3
import weakref

_REGISTRY: set = set()


def _safe_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass
    _REGISTRY.discard(conn)


def managed_connect(owner, db_path: str, timeout: float = 5.0):
    """Open a sqlite connection owned by ``owner`` with guaranteed cleanup.

    A ``weakref.finalize`` is registered on ``owner`` so the connection is closed
    when ``owner`` is collected, even if ``close_all`` is never called. The
    finalizer is kept alive on the connection itself for its lifetime.
    """
    conn = sqlite3.connect(db_path, timeout=timeout)
    _REGISTRY.add(conn)
    finalizer = weakref.finalize(owner, _safe_close, conn)
    try:
        conn._managed_finalizer = finalizer  # keep finalizer alive for conn's life
    except Exception:
        pass
    return conn


def close_all() -> None:
    """Close every managed connection currently tracked.

    Safe to call at the end of a pipeline run (daily_run / runner). Connections
    that are already closed are skipped. After this, the registry is empty.
    """
    for conn in list(_REGISTRY):
        _safe_close(conn)
