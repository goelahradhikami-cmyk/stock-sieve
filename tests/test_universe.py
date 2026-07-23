"""Tests for Commit 1: Stock Identity Infrastructure."""

import pytest
import pandas as pd
import tempfile
import os

from src.data.universe import fetch_eastmoney_stock_list
from src.data.universe_filter import UniverseFilter
from src.data.security_master import SecurityMaster


def test_fetch_stock_list():
    """Fetch real data from Eastmoney — skip if network unavailable."""

    try:
        df = fetch_eastmoney_stock_list()
    except Exception:
        pytest.skip("Network unavailable")
    if df.empty:
        pytest.skip("Eastmoney returned empty (network issue)")
    assert "600519.SH" in df["security_id"].values, "Should include Moutai"
    assert all(df["code"].str.len() == 6)
    # Check market distribution
    exchanges = df["exchange"].unique()
    assert "SH" in exchanges
    assert "SZ" in exchanges


def test_filter_exclude_st():
    sample = pd.DataFrame(
        [
            {
                "security_id": "000001.SZ",
                "code": "000001",
                "exchange": "SZ",
                "status": "active",
                "is_st": 0,
                "avg_amount_20d": 10000,
            },
            {
                "security_id": "000002.SZ",
                "code": "000002",
                "exchange": "SZ",
                "status": "active",
                "is_st": 1,
                "avg_amount_20d": 20000,
            },
        ]
    )
    f = UniverseFilter(exclude_st=True, min_avg_amount_20d=0, use_dynamic_liquidity=False)
    result = f.apply(sample)
    assert len(result) == 1
    assert result.iloc[0]["code"] == "000001"


def test_filter_liquidity():
    sample = pd.DataFrame(
        [
            {
                "security_id": "000001.SZ",
                "code": "000001",
                "exchange": "SZ",
                "status": "active",
                "is_st": 0,
                "avg_amount_20d": 10000,
            },
            {
                "security_id": "000003.SZ",
                "code": "000003",
                "exchange": "SZ",
                "status": "active",
                "is_st": 0,
                "avg_amount_20d": 3000,
            },
        ]
    )
    f = UniverseFilter(min_avg_amount_20d=5000)
    result = f.apply(sample)
    assert len(result) == 1
    assert result.iloc[0]["code"] == "000001"


def test_filter_exclude_bj():
    sample = pd.DataFrame(
        [
            {
                "security_id": "000001.SZ",
                "code": "000001",
                "exchange": "SZ",
                "status": "active",
                "is_st": 0,
                "avg_amount_20d": 10000,
            },
            {
                "security_id": "830001.BJ",
                "code": "830001",
                "exchange": "BJ",
                "status": "active",
                "is_st": 0,
                "avg_amount_20d": 10000,
            },
        ]
    )
    f = UniverseFilter(exclude_bj=True, min_avg_amount_20d=0)
    result = f.apply(sample)
    assert len(result) == 1


def test_security_master():
    """Test SecurityMaster CRUD with temp DB."""

    db_path = os.path.join(tempfile.gettempdir(), "test_sieve_security.db")

    try:
        master = SecurityMaster(db_path)
        master.upsert(
            [
                {
                    "security_id": "600519.SH",
                    "code": "600519",
                    "exchange": "SH",
                    "name": "贵州茅台",
                    "status": "active",
                    "is_st": 0,
                    "total_mv": 25000,
                }
            ]
        )
        df = master.get_active_universe()
        assert len(df) == 1
        assert df.iloc[0]["code"] == "600519"
        assert df.iloc[0]["total_mv"] == 25000

        row = master.get_by_code("600519")
        assert row is not None
        assert row["name"] == "贵州茅台"

        assert master.count() == 1
    finally:
        # Close connection before cleanup
        master.db.close()
        try:
            os.remove(db_path)
        except PermissionError:
            pass  # Windows file lock


def test_universe_filter_stats():
    sample = pd.DataFrame(
        [
            {
                "security_id": f"{i:06d}.SZ",
                "code": f"{i:06d}",
                "exchange": "SZ",
                "status": "active",
                "is_st": 0,
                "avg_amount_20d": 10000,
            }
            for i in range(10)
        ]
    )
    f = UniverseFilter()
    result = f.apply(sample)
    stats = f.stats(sample, result)
    assert stats["original"] == 10
    assert stats["filtered"] == 10
    assert "100" in stats["retention"]
