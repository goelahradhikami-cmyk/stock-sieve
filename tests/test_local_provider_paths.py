"""LocalDataProvider should not be tied to a hardcoded drive letter.

Historical bug: ``TDX_PATHS`` was a hardcoded list of Windows drive paths
(``D:/new_tdx_mock/...``). On a machine without those exact paths it silently
returned empty data. Now the root is resolved via env vars
(``STOCK_SIEVE_TDX_VIPDOC`` / ``STOCK_SIEVE_TDX_ROOT``) with the hardcoded
paths kept only as fallbacks, and a clear warning is printed when none exist.

Tests are environment-independent: they override ``DEFAULT_TDX_PATHS`` and the
env vars explicitly, so they pass whether or not the real TDX dirs are present.
"""

import os
import tempfile
from unittest import mock

from src.data.local_provider import LocalDataProvider

# Env vars this provider reads. We only touch these, never the whole environ
# (which can contain very long values and break restore on some systems).
_TDX_ENV_VARS = ("STOCK_SIEVE_TDX_VIPDOC", "STOCK_SIEVE_TDX_ROOT")

# A path that cannot exist on any OS, used to force the "no default found" branch.
_GUARANTEED_ABSENT = "/nonexistent_tdx_vipdoc_xyz_8321"


def _clear_tdx_env():
    return {k: os.environ.pop(k) for k in _TDX_ENV_VARS if k in os.environ}


def _restore_tdx_env(saved):
    os.environ.update(saved)


def test_no_tdx_path_found_returns_none_without_crashing():
    """With no env override and no valid default dir, tdx_root is None (no crash)."""
    saved = _clear_tdx_env()
    try:
        with mock.patch.object(LocalDataProvider, "DEFAULT_TDX_PATHS", [_GUARANTEED_ABSENT]):
            provider = LocalDataProvider()
    finally:
        _restore_tdx_env(saved)
    assert provider.tdx_root is None
    # Empty DataFrame, never a crash.
    assert provider.get_daily_kline("600000") is not None


def test_env_vipdoc_override_is_used():
    saved = _clear_tdx_env()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["STOCK_SIEVE_TDX_VIPDOC"] = tmp
            provider = LocalDataProvider()
            assert provider.tdx_root == tmp
    finally:
        _restore_tdx_env(saved)


def test_env_root_override_appends_vipdoc():
    saved = _clear_tdx_env()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "vipdoc"))
            os.environ["STOCK_SIEVE_TDX_ROOT"] = tmp
            provider = LocalDataProvider()
            assert provider.tdx_root == os.path.join(tmp, "vipdoc")
    finally:
        _restore_tdx_env(saved)


def test_default_fallback_still_resolved_when_present():
    """If a default path exists on disk, it is still picked (backward compatible)."""
    saved = _clear_tdx_env()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            default = os.path.join(tmp, "D_drive_mock")
            os.makedirs(os.path.join(default, "vipdoc"))
            with mock.patch.object(
                LocalDataProvider, "DEFAULT_TDX_PATHS", [os.path.join(default, "vipdoc")]
            ):
                provider = LocalDataProvider()
            assert provider.tdx_root == os.path.join(default, "vipdoc")
    finally:
        _restore_tdx_env(saved)
