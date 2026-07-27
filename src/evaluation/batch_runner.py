"""
Batch Evaluation Runner — T+N backfill engine.

Commit 6-C: Evaluates historical research_decisions against realized returns,
computing alpha, max_drawdown, volatility, and prediction calibration errors.
"""

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.data.calendar import TradingCalendar
from src.data.db import managed_connect
from src.data.index_provider import IndexDataProvider
from src.data.provider import MarketDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


class BatchEvaluationRunner:
    """Backfill T+N evaluations for historical decisions."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self.provider = MarketDataProvider()
        self.index_provider = IndexDataProvider()
        self.calendar = TradingCalendar()
        self._ensure_tables()

    def _ensure_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS signal_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                research_decision_id INTEGER NOT NULL UNIQUE,
                signal_date DATE NOT NULL,
                security_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                genome_version TEXT,
                thesis_pattern TEXT,
                market_regime TEXT,
                factor_values TEXT NOT NULL,
                alpha_score REAL,
                confidence REAL,
                action TEXT DEFAULT 'BUY',
                signal_strength REAL,
                entry_date DATE,
                entry_price REAL,
                entry_method TEXT DEFAULT 'next_open',
                entry_status TEXT DEFAULT 'filled',
                agent_model_version TEXT,
                factor_engine_version TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (research_decision_id) REFERENCES research_decisions(id)
            );

            CREATE TABLE IF NOT EXISTS evaluation_task (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                research_decision_id INTEGER,
                horizon_days INTEGER,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Add missing columns to evaluation_results
        for col, col_type in [
            ("max_drawdown", "REAL"),
            ("volatility", "REAL"),
            ("predicted_alpha", "REAL"),
            ("alpha_error", "REAL"),
            ("predicted_confidence", "REAL"),
            ("confidence_error", "REAL"),
            ("entry_status", "TEXT DEFAULT 'filled'"),
        ]:
            try:
                self.db.execute(f"ALTER TABLE evaluation_results ADD COLUMN {col} {col_type}")
            except Exception as exc:
                logger.debug("operation failed (was silently ignored): %s", exc)
        self.db.commit()

    def backfill(self, start_date: str, end_date: str, horizons: list[int] | None = None):
        """Backfill evaluations for decisions in date range."""
        if horizons is None:
            horizons = [5, 20, 60, 120]
        decisions = pd.read_sql_query(
            """
            SELECT rd.id, rd.security_id, rd.agent_id, rd.created_at,
                   rd.thesis_pattern, rd.alpha_score, rd.confidence,
                   rd.entry_date, rd.entry_price, rd.factor_snapshot
            FROM research_decisions rd
            WHERE date(rd.created_at) BETWEEN ? AND ?
            ORDER BY rd.created_at
        """,
            self.db,
            params=(start_date, end_date),
        )

        if decisions.empty:
            logger.info(f"  No decisions found in {start_date} ~ {end_date}")
            return 0

        total = len(decisions)
        logger.info(f"  Backfilling {total} decisions × {len(horizons)} horizons...")
        count = 0

        for _, row in decisions.iterrows():
            for horizon in horizons:
                existing = self.db.execute(
                    "SELECT id FROM evaluation_results WHERE research_decision_id=? AND horizon_days=?",
                    (int(row["id"]), horizon),
                ).fetchone()
                if existing:
                    continue

                try:
                    entry_str = row["entry_date"] or row["created_at"]
                    entry_date = date.fromisoformat(str(entry_str)[:10])
                except (ValueError, TypeError):
                    continue

                eval_date = self.calendar.next_trade_day(entry_date, horizon)
                if eval_date is None:
                    eval_date = entry_date + timedelta(days=horizon)
                if eval_date > date.today():
                    continue

                self._evaluate_single(row, horizon, eval_date, entry_date)
                count += 1

        self.db.commit()
        logger.info(f"  Done: {count} evaluation records added")
        return count

    def _evaluate_single(self, row: pd.Series, horizon: int, eval_date: date, entry_date: date):
        code = str(row["security_id"]).split(".")[0]
        rid = int(row["id"])

        # Link to the portfolio decision that actually traded this research
        # decision. evaluation_results.portfolio_decision_id was declared in
        # the schema but never populated, which broke the eval->portfolio
        # link (flagged as eval_portfolio_link_broken=1 for all 200 historical
        # rows). Take the most recent portfolio_decisions row for this rid.
        pd_row = self.db.execute(
            "SELECT id FROM portfolio_decisions WHERE research_decision_id=? "
            "ORDER BY decision_date DESC, id DESC LIMIT 1",
            (rid,),
        ).fetchone()
        portfolio_decision_id = pd_row[0] if pd_row else None

        # Forward metrics
        stock_return, max_dd, volatility = self._get_forward_metrics(code, entry_date, eval_date)
        if stock_return is None:
            self.db.execute(
                """
                INSERT OR REPLACE INTO evaluation_results
                (research_decision_id, portfolio_decision_id, horizon_days, eval_date, status)
                VALUES (?,?,?,?,?)
            """,
                (rid, portfolio_decision_id, horizon, eval_date.isoformat(), "insufficient_data"),
            )
            return

        # Benchmark return
        bench_return = self.index_provider.get_return(
            "000300", entry_date.isoformat(), eval_date.isoformat()
        )

        # Alpha
        alpha_vs_market = stock_return - bench_return
        alpha_vs_sector = stock_return - bench_return  # Fallback

        # Prediction calibration
        predicted_alpha = row.get("alpha_score") or 0
        predicted_conf = row.get("confidence") or 5
        alpha_error = alpha_vs_market - (predicted_alpha / 10)  # Normalize to 0-1
        conf_error = (1.0 if alpha_vs_market > 0 else 0.0) - (predicted_conf / 10.0)

        self.db.execute(
            """
            INSERT OR REPLACE INTO evaluation_results
            (research_decision_id, portfolio_decision_id, horizon_days, eval_date,
             stock_return, market_return, sector_return,
             alpha_vs_market, alpha_vs_sector,
             max_drawdown_during, max_profit_during,
             max_drawdown, volatility,
             predicted_alpha, alpha_error,
             predicted_confidence, confidence_error,
             entry_status, is_profitable, alpha_positive,
             verdict, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                rid,
                portfolio_decision_id,
                horizon,
                eval_date.isoformat(),
                stock_return,
                bench_return,
                bench_return,
                alpha_vs_market,
                alpha_vs_sector,
                max_dd,
                max(0, float(stock_return)),
                max_dd,
                volatility,
                predicted_alpha,
                alpha_error,
                predicted_conf,
                conf_error,
                "filled",
                1 if stock_return > 0 else 0,
                1 if alpha_vs_market > 0 else 0,
                "market_alpha_positive" if alpha_vs_market > 0 else "market_alpha_negative",
                "completed",
            ),
        )

    def _get_forward_metrics(self, code: str, start: date, end: date):
        """Compute forward return, max drawdown, volatility."""
        kline = self.provider.get_daily_kline(code, start.isoformat(), end.isoformat())
        if kline.empty or len(kline) < 2:
            return None, None, None

        close_col = "adj_close" if "adj_close" in kline.columns else "close"
        prices = kline[close_col].values
        start_price = float(prices[0])
        end_price = float(prices[-1])

        forward_return = (end_price - start_price) / start_price if start_price > 0 else 0

        # Max drawdown
        peak = np.maximum.accumulate(prices)
        dd = (prices - peak) / peak
        max_dd = float(dd.min())

        # Volatility
        rets = np.diff(prices) / prices[:-1]
        volatility = float(np.std(rets) * np.sqrt(252)) if len(rets) > 1 else None

        return forward_return, max_dd, volatility

    def _get_peer_return(self, trade_date: date, start: date, end: date) -> float | None:
        """Average return of ~300 peer stocks from the same universe snapshot."""
        try:
            stocks = pd.read_sql_query(
                """
                SELECT security_id FROM universe_snapshot
                WHERE trade_date = ?
                ORDER BY RANDOM()
                LIMIT 100
            """,
                self.db,
                params=(trade_date.isoformat(),),
            )
        except Exception:
            return None

        if stocks.empty:
            return None

        returns = []
        for sid in stocks["security_id"].iloc[:50]:  # Cap at 50 for speed
            code = str(sid).split(".")[0]
            ret, _, _ = self._get_forward_metrics(code, start, end)
            if ret is not None:
                returns.append(ret)

        return float(np.mean(returns)) if returns else None

    def run_pending(self):
        """Evaluate all pending decisions whose T+N date has arrived."""
        today = date.today()
        conn = self.db

        rows = conn.execute(
            """
            SELECT rd.id FROM research_decisions rd
            LEFT JOIN evaluation_results er ON rd.id = er.research_decision_id
            WHERE er.id IS NULL
              AND date(rd.entry_date) <= ?
            ORDER BY rd.entry_date
        """,
            (today.isoformat(),),
        ).fetchall()

        if not rows:
            logger.info("  No pending evaluations")
            return 0

        count = 0
        for (rid,) in rows:
            try:
                row = conn.execute("SELECT * FROM research_decisions WHERE id=?", (rid,)).fetchone()
                if row:
                    rd = dict(zip([d[0] for d in row.description], row, strict=False))
                    entry_str = rd.get("entry_date", "")
                    entry_date = date.fromisoformat(str(entry_str)[:10])
                    days_passed = (today - entry_date).days

                    for horizon in [5, 20, 30, 60, 90, 120, 180, 365]:
                        if days_passed >= horizon:
                            eval_date = entry_date + timedelta(days=horizon)
                            if eval_date <= today:
                                self._evaluate_single(pd.Series(rd), horizon, eval_date, entry_date)
                                count += 1
            except Exception:
                continue

        self.db.commit()
        logger.info(f"  Pending evaluations: {count} records added")
        return count


# ═══════════════════════════════════════════════════════════
# Signal Snapshot Saver (for runner integration)
# ═══════════════════════════════════════════════════════════


def save_signal_snapshot(
    db, research_decision_id: int, analysis, factor_snapshot: dict, market_regime: str = ""
):
    """Persist a signal snapshot after Research Agent produces SecurityAnalysis."""
    from datetime import date

    try:
        db.execute(
            """
            INSERT OR REPLACE INTO signal_snapshot
            (research_decision_id, signal_date, security_id, agent_id, genome_version,
             thesis_pattern, market_regime, factor_values,
             alpha_score, confidence, action, signal_strength,
             entry_date, entry_price, entry_method,
             agent_model_version, factor_engine_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                research_decision_id,
                date.today().isoformat(),
                getattr(analysis, "stock_code", "?"),
                getattr(analysis, "agent_id", "?"),
                getattr(analysis, "genome_version", "v1.0"),
                analysis.thesis.pattern
                if hasattr(analysis, "thesis") and analysis.thesis
                else None,
                market_regime,
                json.dumps(factor_snapshot, default=str),
                getattr(analysis, "alpha_score", 0),
                getattr(analysis, "confidence", 0),
                "BUY",
                getattr(analysis, "alpha_score", 0) * getattr(analysis, "confidence", 0) / 10,
                date.today().isoformat(),
                None,  # entry_price
                "next_open",
                getattr(analysis, "model_version", "v1.0"),
                getattr(analysis, "factor_version", "v1.0"),
            ),
        )
    except Exception as e:
        logger.warning(f"  ⚠️ signal_snapshot save failed: {e}")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Batch Evaluation Runner")
    parser.add_argument("--start", default="2025-01-01", help="Start date")
    parser.add_argument("--end", default=None, help="End date (default: today)")
    parser.add_argument(
        "--horizons", nargs="*", type=int, default=[20, 60], help="Evaluation horizons in days"
    )
    parser.add_argument("--pending", action="store_true", help="Run pending only")
    args = parser.parse_args()

    runner = BatchEvaluationRunner()

    if args.pending:
        runner.run_pending()
    else:
        end = args.end or date.today().isoformat()
        runner.backfill(args.start, end, args.horizons)
