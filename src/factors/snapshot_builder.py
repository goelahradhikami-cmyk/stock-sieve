"""
Stock Factor Snapshot Builder - Commit 6-L.6 data infrastructure.

Precomputes daily factor scores for the entire universe (~5328 stocks) into
the ``stock_factor_snapshot`` table so that backtesting a child agent only
requires reweighting (a SQL join), not recomputing factors from scratch.

补丁4: stores both absolute scores AND market percentiles.

Usage:
    from src.factors.snapshot_builder import FactorSnapshotBuilder
    builder = FactorSnapshotBuilder()
    builder.build_for_date('2026-07-17')

    # Or via CLI:
    python scripts/build_factor_snapshots.py --date 2026-07-17
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from src.data.financial_provider import get_financial_provider
from src.data.local_provider import LocalDataProvider
from src.data.provider import MarketDataProvider
from src.factors.engine import CompositeResult, FactorEngine
from src.factors.snapshot_schema import DDL_STOCK_FACTOR_SNAPSHOT
from src.utils.logger import get_logger

logger = get_logger(__name__)


class FactorSnapshotBuilder:
    """Builds stock_factor_snapshot rows for a given trade date.

    The factor engine is personality-agnostic: it produces the same 6 family
    scores for every agent. The personality enters only downstream when
    ResearchAgent re-weights these scores using doctrine.factor_bias. So one
    snapshot per (date, stock) serves ALL agents - they just apply different
    weights at query time.
    """

    def __init__(
        self, cache_db_path: str = "data/cache.db", eval_db_path: str = "data/evaluation.db"
    ):
        self.cache_db_path = cache_db_path
        self.eval_db_path = eval_db_path
        self.local = LocalDataProvider()
        self.provider = MarketDataProvider()
        # Commit 6-L.6: use the factory so STOCK_SIEVE_USE_BAOSTOCK=1 picks up
        # multi-period fundamentals (growth_1y/3y, margin_trend) that the
        # mootdx-only provider cannot provide.
        self.fin = get_financial_provider()
        self.engine = FactorEngine()
        self._ensure_eval_table()

    def _ensure_eval_table(self) -> None:
        """Ensure stock_factor_snapshot exists in evaluation.db."""
        conn = sqlite3.connect(self.eval_db_path)
        conn.executescript(DDL_STOCK_FACTOR_SNAPSHOT)
        conn.commit()
        conn.close()

    def get_universe(self, trade_date: str) -> list[str]:
        """Get the tradable universe for a date.

        Prefers universe_filter_log (if available), falls back to
        security_master active non-ST stocks.
        """
        conn = sqlite3.connect(self.cache_db_path)
        try:
            rows = conn.execute(
                "SELECT security_id FROM universe_filter_log WHERE trade_date=? AND pass_flag=1",
                (trade_date,),
            ).fetchall()
            if rows:
                return [r[0] for r in rows]
            # Fallback: all active non-ST
            rows = conn.execute(
                "SELECT code FROM security_master WHERE status='active' AND is_st=0"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def build_for_date(
        self, trade_date: str, lookback_days: int = 365, limit: int | None = None
    ) -> int:
        """Build factor snapshots for all stocks on a given trade date.

        Args:
            trade_date: ISO date string (e.g. '2026-07-17')
            lookback_days: how far back to fetch K-line for momentum/volatility
            limit: optional cap on number of stocks (for testing)

        Returns: number of rows written.
        """
        codes = self.get_universe(trade_date)
        if limit:
            codes = codes[:limit]

        logger.info("snapshot_builder: %d stocks for %s", len(codes), trade_date)

        start_date = (date.fromisoformat(trade_date) - timedelta(days=lookback_days)).isoformat()

        # Phase 1: compute per-stock factors
        results: list[tuple[str, CompositeResult]] = []
        for i, code in enumerate(codes):
            # Strip exchange suffix (.SZ/.SH/.BJ) - factor engine and K-line
            # providers expect the bare 6-digit code.
            bare_code = code.split(".")[0] if "." in code else code
            try:
                price_data = self.local.get_daily_kline(bare_code, start_date, trade_date)
                if price_data is None or price_data.empty or len(price_data) < 2:
                    # Fallback to mootdx (network) if local TDX file missing
                    price_data = self.provider.get_daily_kline(bare_code, start_date, trade_date)
                if price_data is None or price_data.empty or len(price_data) < 2:
                    continue

                fin_data = self.fin.get_financial_dict(bare_code)
                composite = self.engine.compute_single_stock(bare_code, fin_data, price_data)
                results.append((bare_code, composite))
            except Exception as e:
                if i < 5 or i % 200 == 0:
                    logger.warning("snapshot_builder: %s failed: %s", bare_code, e)
                continue

            if (i + 1) % 500 == 0:
                logger.info("snapshot_builder: %d/%d computed", i + 1, len(codes))

        if not results:
            logger.warning("snapshot_builder: no results for %s", trade_date)
            return 0

        # Phase 2: cross-sectional normalization (percentiles are relative)
        composites = [c for _, c in results]
        self.engine.compute_cross_sectional(composites)

        # Phase 3: compute percentiles and write to DB
        # After compute_cross_sectional, each composite.factors has percentile populated
        conn = sqlite3.connect(self.eval_db_path)
        written = 0
        try:
            for code, comp in results:
                # Extract per-family percentiles from the factor list
                fam_percentiles = self._family_percentiles(comp)
                factor_values = {
                    f.name: f.raw_value for f in comp.factors if f.raw_value is not None
                }

                conn.execute(
                    """
                    INSERT OR REPLACE INTO stock_factor_snapshot
                    (trade_date, security_id,
                     quality_score, value_score, growth_score,
                     momentum_score, risk_score, sentiment_score,
                     quality_percentile, value_percentile, growth_percentile,
                     momentum_percentile, risk_percentile, sentiment_percentile,
                     factor_values_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                    (
                        trade_date,
                        code,
                        comp.quality_score,
                        comp.value_score,
                        comp.growth_score,
                        comp.momentum_score,
                        comp.risk_score,
                        comp.sentiment_score,
                        fam_percentiles.get("quality"),
                        fam_percentiles.get("value"),
                        fam_percentiles.get("growth"),
                        fam_percentiles.get("momentum"),
                        fam_percentiles.get("risk"),
                        fam_percentiles.get("sentiment"),
                        json.dumps(factor_values, default=str),
                    ),
                )
                written += 1
            conn.commit()
        finally:
            conn.close()

        logger.info("snapshot_builder: wrote %d rows for %s", written, trade_date)
        return written

    def _family_percentiles(self, composite) -> dict[str, float]:
        """Extract per-family average percentile from a CompositeResult.

        After compute_cross_sectional, each FactorResult has a `percentile`
        field. We average within each family.
        """
        from collections import defaultdict

        sums: defaultdict[str, float] = defaultdict(float)
        counts: defaultdict[str, int] = defaultdict(int)
        for f in composite.factors:
            if f.percentile is not None and f.family:
                sums[f.family] += f.percentile
                counts[f.family] += 1
        return {fam: round(sums[fam] / counts[fam], 4) for fam in sums if counts[fam] > 0}

    def get_snapshot(self, trade_date: str, security_id: str) -> dict | None:
        """Load a single stock's factor snapshot."""
        conn = sqlite3.connect(self.eval_db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM stock_factor_snapshot WHERE trade_date=? AND security_id=?",
                (trade_date, security_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def score_universe(self, trade_date: str, factor_bias: dict, top_n: int = 20) -> list[dict]:
        """Score all stocks for a date using given factor_bias, return top N.

        This is the hot path for sandbox backtesting: a child agent's
        doctrine.factor_bias is applied via a single SQL weighted-sum query
        over the precomputed snapshot - no factor recomputation needed.
        """
        conn = sqlite3.connect(self.eval_db_path)
        conn.row_factory = sqlite3.Row
        try:
            q, v, g, m, r, s = (
                factor_bias.get("quality", 0),
                factor_bias.get("value", 0),
                factor_bias.get("growth", 0),
                factor_bias.get("momentum", 0),
                factor_bias.get("risk", 0),
                factor_bias.get("sentiment", 0),
            )
            rows = conn.execute(
                """
                SELECT security_id,
                       quality_score * ? + value_score * ? + growth_score * ?
                       + momentum_score * ? + risk_score * ? + sentiment_score * ?
                       AS alpha,
                       quality_score, value_score, growth_score,
                       momentum_score, risk_score, sentiment_score
                FROM stock_factor_snapshot
                WHERE trade_date=?
                ORDER BY alpha DESC
                LIMIT ?
            """,
                (q, v, g, m, r, s, trade_date, top_n),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
