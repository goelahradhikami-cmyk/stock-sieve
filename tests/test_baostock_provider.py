"""
Tests for the baostock financial provider + factory.

baostock MAY or MAY NOT be installed in the test environment:
  - Degradation is verified deterministically by forcing _login to raise, so it
    does not depend on baostock being absent.
  - The live contract test runs only when baostock is importable (it makes a
    real network call); otherwise it is skipped.
"""

import os
import tempfile

import pytest

from src.data.baostock_provider import BaostockProvider
from src.data.financial_provider import get_financial_provider


def _forced_import_error(self):
    raise ImportError("forced: baostock missing")


def test_module_imports_without_baostock():
    assert BaostockProvider is not None


def test_factory_default_returns_provider():
    os.environ.pop("STOCK_SIEVE_USE_BAOSTOCK", None)
    p = get_financial_provider()
    assert hasattr(p, "get_financial_dict")
    assert callable(p.get_financial_dict)


def test_factory_optin_falls_back():
    os.environ["STOCK_SIEVE_USE_BAOSTOCK"] = "1"
    try:
        p = get_financial_provider()
        assert hasattr(p, "get_financial_dict")
    finally:
        os.environ.pop("STOCK_SIEVE_USE_BAOSTOCK", None)


def test_baostock_provider_degrades_gracefully(monkeypatch):
    # Force _login to fail -> get_financial_dict must return {} (never fake data)
    monkeypatch.setattr(BaostockProvider, "_login", _forced_import_error)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        pass
    try:
        prov = BaostockProvider(db_path=tmp.name)
        assert prov.get_financial_dict("600519") == {}
    finally:
        os.unlink(tmp.name)


def test_baostock_provider_contract():
    pytest.importorskip("baostock")  # skip if baostock not installed
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        pass
    try:
        p = BaostockProvider(db_path=tmp.name, n_years=5)
        d = p.get_financial_dict("600519")
        assert isinstance(d, dict)
        for k in (
            "roe",
            "gross_margin",
            "net_margin",
            "pe_ttm",
            "pb",
            "mcap",
            "revenue_growth_1y",
            "earnings_growth_1y",
            "current_ratio",
            "_date",
        ):
            assert k in d, f"missing key {k}"
        # Real, sane values must be present (not the old all-None neutralised set)
        assert isinstance(d.get("roe"), (int, float)) and d["roe"] > 0
        assert isinstance(d.get("current_ratio"), (int, float))
    finally:
        os.unlink(tmp.name)
