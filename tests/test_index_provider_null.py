"""IndexDataProvider must not crash on NULL (unpopulated) adj_close rows.

Historical bug: ``get_latest_close`` did ``float(row[0]) if row else 0.0`` and
``get_return`` stored ``row[0]`` directly. When a cache row exists but its
``adj_close`` column is NULL (the index was synced without a price, e.g. a
failed Tencent snapshot), ``row[0]`` is ``None`` → ``float(None)`` /
``None - number`` crash.
"""

from unittest import mock


class _FakeMarketDataProvider:
    """Stand-in for the heavy real provider (no network / mootdx)."""

    def __init__(self, *a, **k):
        pass

    def get_daily_kline(self, *a, **k):
        return None


def _make_provider():
    # Patch the name IndexDataProvider looks up at construction time.
    from src.data import index_provider

    with mock.patch.object(index_provider, "MarketDataProvider", _FakeMarketDataProvider):
        return index_provider.IndexDataProvider(":memory:")


def test_get_latest_close_null_row_returns_zero_not_crash():
    idx = _make_provider()
    idx.db.execute(
        "INSERT INTO market_index_daily (index_code, trade_date, adj_close) VALUES (?,?,?)",
        ("000300", "2026-01-01", None),
    )
    idx.db.commit()
    # Previously: float(None) -> TypeError
    assert idx.get_latest_close("000300") == 0.0


def test_get_return_null_row_returns_zero_not_crash():
    idx = _make_provider()
    idx.db.execute(
        "INSERT INTO market_index_daily (index_code, trade_date, adj_close) VALUES (?,?,?)",
        ("000300", "2026-01-01", None),
    )
    idx.db.commit()
    # Previously: start_price = None -> None arithmetic crash
    assert idx.get_return("000300", "2026-01-01", "2026-01-01") == 0.0


def test_get_return_normal_values_still_correct():
    idx = _make_provider()
    idx.db.execute(
        "INSERT INTO market_index_daily (index_code, trade_date, adj_close) VALUES (?,?,?)",
        ("000905", "2026-01-01", 100.0),
    )
    idx.db.execute(
        "INSERT INTO market_index_daily (index_code, trade_date, adj_close) VALUES (?,?,?)",
        ("000905", "2026-02-01", 110.0),
    )
    idx.db.commit()
    # (110 - 100) / 100 == 0.10
    assert abs(idx.get_return("000905", "2026-01-01", "2026-02-01") - 0.10) < 1e-9


def test_get_latest_close_normal_value_still_correct():
    idx = _make_provider()
    idx.db.execute(
        "INSERT INTO market_index_daily (index_code, trade_date, adj_close) VALUES (?,?,?)",
        ("000852", "2026-01-01", 333.0),
    )
    idx.db.commit()
    assert idx.get_latest_close("000852") == 333.0
