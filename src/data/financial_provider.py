"""
Financial provider factory - pick akshare (bulk, fast) / baostock (multi-period)
/ mootdx (legacy single-period), behind env switches.

Priority (first match wins):
  1. STOCK_SIEVE_USE_AKSHARE=1  -> AkshareProvider (recommended: 2min bulk, growth_1y from akshare directly)
  2. STOCK_SIEVE_USE_BAOSTOCK=1 -> BaostockProvider (multi-period, ~13h for full backfill)
  3. (default)                   -> FinancialDataProvider (mootdx single-period, growth=None)

Default behaviour (no env set): return the legacy FinancialDataProvider, so
there is ZERO behaviour change until the operator opts in.

After running `python scripts/backfill_akshare.py`, flip it on with:

    export STOCK_SIEVE_USE_AKSHARE=1
"""

import logging
import os

from src.utils.logger import get_logger

logger = get_logger(__name__)

logger = logging.getLogger(__name__)


def get_financial_provider():
    """Return a provider exposing get_financial_dict(code) -> dict.

    Honours STOCK_SIEVE_USE_AKSHARE > STOCK_SIEVE_USE_BAOSTOCK. Falls back to
    FinancialDataProvider on any failure so the pipeline never breaks.
    """
    use_akshare = os.environ.get("STOCK_SIEVE_USE_AKSHARE", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if use_akshare:
        try:
            from src.data.akshare_provider import AkshareProvider

            logger.info("financial_provider: using AkshareProvider (bulk, multi-period)")
            return AkshareProvider()
        except Exception as e:
            logger.warning(
                "financial_provider: akshare unavailable (%s); "
                "falling back to FinancialDataProvider",
                e,
            )

    use_baostock = os.environ.get("STOCK_SIEVE_USE_BAOSTOCK", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if use_baostock:
        try:
            from src.data.baostock_provider import BaostockProvider

            logger.info("financial_provider: using BaostockProvider (multi-period)")
            return BaostockProvider()
        except Exception as e:
            logger.warning(
                "financial_provider: baostock unavailable (%s); "
                "falling back to FinancialDataProvider",
                e,
            )
    from src.data.financials import FinancialDataProvider

    return FinancialDataProvider()
