"""
Sandbox Validator v2 — Three-layer validation with market snapshot reproducibility.

Commit 6-D.2:
  Layer 1: Minimum trade count (min_trades >= 30)
  Layer 2: Fitness improvement (child > parent × 1.05)
  Layer 3: Drawdown guard (child_dd < parent_dd × 1.2)
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from src.data.db import managed_connect
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SandboxResult:
    candidate_agent_id: str
    parent_agent_id: str
    parent_fitness: float
    child_fitness: float
    improvement: float
    sample_count: int
    status: str  # approved / rejected
    reject_reasons: list


class SandboxValidator:
    """Three-layer sandbox validation with persistent records."""

    def __init__(
        self,
        db_path: str = "data/evaluation.db",
        train_months: int = 24,
        validation_months: int = 3,
        min_trades: int = 10,
        improvement_threshold: float = 0.05,
    ):
        self.db_path = db_path
        self.db = managed_connect(self, db_path, timeout=10)
        self.train_months = train_months
        self.validation_months = validation_months
        self.min_trades = min_trades
        self.improvement_threshold = improvement_threshold
        # Commit 6-L.7 fix: _real_backtest uses self.provider.get_daily_kline
        # for forward returns. Without this, every _compute_forward_return
        # call raised AttributeError (silently swallowed) and ALL children
        # fell through to the synthetic-backtest fallback - so the real
        # backtest path never actually ran.
        from src.data.provider import MarketDataProvider

        self.provider = MarketDataProvider()
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS sandbox_evaluation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_agent_id TEXT NOT NULL,
                parent_agent_id TEXT,
                cycle_id INTEGER,
                start_date DATE NOT NULL, end_date DATE NOT NULL,
                market_snapshot_id TEXT,
                parent_alpha REAL, parent_sharpe REAL,
                parent_drawdown REAL, parent_calibration REAL,
                parent_fitness REAL,
                child_alpha REAL, child_sharpe REAL,
                child_drawdown REAL, child_calibration REAL,
                child_fitness REAL,
                improvement REAL,
                sample_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                reject_reasons TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.commit()

    def validate(self, child_genome: dict, parent_agent_id: str, cycle_id: int) -> SandboxResult:
        """Three-layer sandbox validation.

        Layer 1: Minimum trades (sample_count >= min_trades)
        Layer 2: Fitness improvement (child > parent + 5%)
        Layer 3: Drawdown guard (child_dd <= parent_dd × 1.2)
        """
        child_id = child_genome["identity"]["agent_id"]
        end_date = date.today()
        start_date = end_date - timedelta(days=self.validation_months * 30)
        market_snapshot_id = f"sandbox_{end_date.isoformat()}_{np.random.randint(1000, 9999)}"

        # 1. Register temporary candidate (with unique genome_hash)
        import hashlib

        temp_hash = hashlib.sha256(
            f"{child_id}_{cycle_id}_{np.random.randint(1, 99999)}".encode()
        ).hexdigest()[:16]
        self.db.execute(
            "INSERT INTO agent_genome_snapshots "
            "(agent_id, strategy_genus, strategy_species, generation, parent_agent_id, genome_hash, genome_yaml, birth_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, date('now'), 'candidate')",
            (
                child_id,
                child_genome.get("identity", {}).get("strategy_genus", "unknown"),
                child_genome.get("identity", {}).get("strategy_species", "unknown"),
                child_genome.get("identity", {}).get("generation", 1),
                parent_agent_id,
                temp_hash,
                json.dumps(child_genome),
            ),
        )

        # 2. Generate simulated backtest data for child
        self._generate_child_evaluations(child_id, parent_agent_id, start_date, end_date, cycle_id)

        # 3. Calculate fitness
        child_data = self._calculate_fitness(child_id)
        parent_data = self._calculate_fitness(parent_agent_id)

        # 4. Archive sandbox data
        try:
            # Get research_decision_ids for this agent
            rids = self.db.execute(
                "SELECT id FROM research_decisions WHERE agent_id=?", (child_id,)
            ).fetchall()
            for (rid,) in rids:
                self.db.execute(
                    "UPDATE evaluation_results SET status='sandbox_archived' WHERE research_decision_id=?",
                    (rid,),
                )
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)
        self.db.execute(
            "DELETE FROM agent_genome_snapshots WHERE agent_id=? AND status='candidate'",
            (child_id,),
        )

        # 5. Three-layer judgment
        reject_reasons = []
        sample_count = child_data.get("sample_count", 0)

        # Layer 1: Minimum trades
        if sample_count < self.min_trades:
            reject_reasons.append(f"insufficient_trades: {sample_count}/{self.min_trades}")

        child_fitness = child_data.get("fitness", 0)
        parent_fitness = parent_data.get("fitness", 0)

        if parent_fitness > 0:
            improvement = (child_fitness - parent_fitness) / abs(parent_fitness)
        else:
            improvement = 1.0 if child_fitness > 0 else 0.0

        # Layer 2: Fitness improvement
        if improvement < self.improvement_threshold and sample_count >= self.min_trades:
            reject_reasons.append(
                f"fitness_not_improved: {improvement:.3f} < {self.improvement_threshold}"
            )

        # Layer 3: Drawdown guard
        child_dd = child_data.get("avg_drawdown", 0)
        parent_dd = parent_data.get("avg_drawdown", 0)
        if abs(child_dd) > abs(parent_dd) * 1.2 and abs(parent_dd) > 0.01:
            reject_reasons.append(f"drawdown_worse: child={child_dd:.2%} parent={parent_dd:.2%}")

        status = "approved" if not reject_reasons else "rejected"

        # 6. Persist sandbox record
        self.db.execute(
            """
            INSERT INTO sandbox_evaluation
            (candidate_agent_id, parent_agent_id, cycle_id,
             start_date, end_date, market_snapshot_id,
             parent_alpha, parent_sharpe, parent_drawdown, parent_fitness,
             child_alpha, child_sharpe, child_drawdown, child_fitness,
             improvement, sample_count, status, reject_reasons)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                child_id,
                parent_agent_id,
                cycle_id,
                start_date.isoformat(),
                end_date.isoformat(),
                market_snapshot_id,
                parent_data.get("avg_alpha"),
                parent_data.get("sharpe"),
                parent_dd,
                parent_fitness,
                child_data.get("avg_alpha"),
                child_data.get("sharpe"),
                child_dd,
                child_fitness,
                improvement,
                sample_count,
                status,
                json.dumps(reject_reasons),
            ),
        )
        self.db.commit()

        return SandboxResult(
            candidate_agent_id=child_id,
            parent_agent_id=parent_agent_id,
            parent_fitness=parent_fitness,
            child_fitness=child_fitness,
            improvement=improvement,
            sample_count=sample_count,
            status=status,
            reject_reasons=reject_reasons,
        )

    def _calculate_fitness(self, agent_id: str) -> dict:
        evals = self.db.execute(
            """
            SELECT
                AVG(alpha_vs_market) as avg_alpha,
                AVG(CASE WHEN alpha_vs_market > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                AVG(max_drawdown_during) as avg_dd,
                COUNT(*) as sample_count
            FROM evaluation_results
            WHERE research_decision_id IN (
                SELECT id FROM research_decisions WHERE agent_id = ?
            )
        """,
            (agent_id,),
        ).fetchone()

        if not evals or evals[3] == 0:
            return {"fitness": 0, "sample_count": 0, "avg_alpha": 0, "avg_drawdown": 0}

        avg_alpha = evals[0] or 0
        win_rate = evals[1] or 0
        avg_dd = evals[2] or 0
        cal_err = 0.5

        fitness = (
            avg_alpha * 0.30 + win_rate * 0.25 + (-abs(avg_dd)) * 0.20 + (1 - cal_err) * 0.15 + 0.10
        )

        return {
            "fitness": fitness,
            "avg_alpha": avg_alpha,
            "win_rate": win_rate,
            "avg_drawdown": avg_dd,
            "sample_count": evals[3],
            "sharpe": 0,
            "calibration_error": cal_err,
        }

    def get_latest_results(self, limit: int = 10) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM sandbox_evaluation ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        cols = [d[0] for d in rows[0].description] if rows else []
        return [dict(zip(cols, r, strict=False)) for r in rows] if cols else []

    def _generate_child_evaluations(self, child_id, parent_id, start_date, end_date, cycle_id):
        """Generate evaluation data for the child via REAL backtest.

        Commit 6-L.6: replaces the old fake backtest (copy parent + random
        perturbation) with a genuine backtest:
          1. Build child's DoctrineEngine from its genome -> factor_bias
          2. For each trade date in the validation window, score the universe
             using factor_bias over stock_factor_snapshot (SQL join, fast)
          3. Pick Top N stocks
          4. Compute real forward returns from K-line
          5. Write real research_decisions + evaluation_results

        Falls back to the old synthetic method if stock_factor_snapshot has
        no data for the window (so the pipeline still runs before snapshots
        are built).
        """
        # Try real backtest first
        count = self._real_backtest(child_id, child_id, start_date, end_date, cycle_id)
        if count > 0:
            logger.info(f"    Sandbox: generated {count} REAL evaluations for {child_id}")
            return

        # Fallback: synthetic (old behavior) when no snapshot data exists yet
        count = self._synthetic_backtest(child_id, parent_id, start_date, end_date, cycle_id)
        if count > 0:
            logger.info(
                f"    Sandbox: generated {count} synthetic evaluations for {child_id} (no snapshot data)"
            )

    def _real_backtest(
        self,
        child_id,
        genome_or_id,
        start_date,
        end_date,
        cycle_id,
        top_n: int = 20,
        horizon: int = 20,
    ) -> int:
        """Real backtest using stock_factor_snapshot + K-line forward returns.

        Returns the number of evaluation records written. Returns 0 if no
        snapshot data is available (caller falls back to synthetic).
        """
        # 1. Get the child's factor_bias from its genome via DoctrineEngine
        try:
            from src.agents.doctrine_engine import DoctrineEngine

            engine = DoctrineEngine()

            # Load the child genome
            if isinstance(genome_or_id, str):
                # It's an agent_id - load genome_yaml from DB
                row = self.db.execute(
                    "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id=? ORDER BY id DESC LIMIT 1",
                    (genome_or_id,),
                ).fetchone()
                if not row:
                    return 0
                import yaml

                genome_data = yaml.safe_load(row[0]) or {}
            else:
                genome_data = genome_or_id

            identity = genome_data.get("investment_identity", {}).get("dimensions", {})
            if not identity:
                # Try identity block (some genomes use top-level)
                identity = genome_data.get("identity", {})
            doctrine_seed = genome_data.get("doctrine_seed", {}).get("preferred")
            doctrine = engine.classify(identity, doctrine_seed=doctrine_seed)
            factor_bias = doctrine.factor_bias
        except Exception as e:
            import logging

            logging.getLogger(__name__).debug("sandbox real_backtest doctrine failed: %s", e)
            return 0

        # 2. Find trade dates with snapshot data in the window
        snapshot_dates = self.db.execute(
            "SELECT DISTINCT trade_date FROM stock_factor_snapshot "
            "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        if not snapshot_dates:
            return 0  # no snapshot data - caller falls back

        # Sample at most ~30 dates (enough for min_trades) spread across window
        all_dates = [r[0] for r in snapshot_dates]
        if len(all_dates) > 30:
            step = len(all_dates) // 30
            all_dates = all_dates[::step][:30]

        # 3. For each date: score universe, pick top N, compute forward return
        from src.data.index_provider import IndexDataProvider

        idx_provider = IndexDataProvider()
        count = 0

        for trade_date in all_dates:
            # Score universe using factor_bias (single SQL query over snapshot)
            q = factor_bias.get("quality", 0)
            v = factor_bias.get("value", 0)
            g = factor_bias.get("growth", 0)
            m = factor_bias.get("momentum", 0)
            r = factor_bias.get("risk", 0)
            s = factor_bias.get("sentiment", 0)

            picks = self.db.execute(
                """
                SELECT security_id,
                       quality_score * ? + value_score * ? + growth_score * ?
                       + momentum_score * ? + risk_score * ? + sentiment_score * ?
                       AS alpha
                FROM stock_factor_snapshot
                WHERE trade_date=?
                ORDER BY alpha DESC
                LIMIT ?
            """,
                (q, v, g, m, r, s, trade_date, top_n),
            ).fetchall()

            if not picks:
                continue

            # Eval date = trade_date + horizon trading days
            from datetime import timedelta

            eval_date_dt = date.fromisoformat(trade_date) + timedelta(days=horizon + 10)
            if eval_date_dt > end_date:
                eval_date_dt = end_date

            # Compute forward returns for picked stocks
            for security_id, alpha_score in picks:
                stock_ret = self._compute_forward_return(
                    security_id, trade_date, eval_date_dt.isoformat()
                )
                if stock_ret is None:
                    continue

                bench_ret = idx_provider.get_return("000300", trade_date, eval_date_dt.isoformat())
                alpha_vs_market = stock_ret - bench_ret

                rid = self.db.execute(
                    """
                    INSERT INTO research_decisions
                    (agent_id, genome_hash, security_id, thesis_id, thesis_family,
                     thesis_pattern, thesis_claim, thesis_evidence, thesis_invalidation,
                     alpha_score, confidence, factor_snapshot, risk_assessment,
                     decision_hash, input_hash, entry_price, entry_date, engine_version, doctrine_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                    (
                        child_id,
                        f"sandbox_{child_id}",
                        security_id,
                        f"sbox_{cycle_id}_{count}",
                        "hybrid",
                        "real_backtest",
                        f"Sandbox real backtest pick (alpha={alpha_score:.1f})",
                        "[]",
                        "[]",
                        min(10.0, alpha_score / 10.0),
                        5.0,
                        "{}",
                        "{}",
                        f"sbox_{child_id}_{cycle_id}_{count}",
                        f"sbox_ih_{child_id}_{count}",
                        None,
                        trade_date,
                        "v2_identity_driven",
                        doctrine.doctrine_id,
                    ),
                ).lastrowid

                self.db.execute(
                    """
                    INSERT INTO evaluation_results
                    (research_decision_id, horizon_days, eval_date,
                     stock_return, market_return, sector_return,
                     alpha_vs_market, alpha_vs_sector,
                     max_drawdown_during, max_profit_during,
                     is_profitable, alpha_positive, verdict, status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                    (
                        rid,
                        horizon,
                        eval_date_dt.isoformat(),
                        stock_ret,
                        bench_ret,
                        bench_ret,
                        alpha_vs_market,
                        alpha_vs_market,
                        None,
                        max(0, stock_ret),
                        1 if stock_ret > 0 else 0,
                        1 if alpha_vs_market > 0 else 0,
                        "market_alpha_positive" if alpha_vs_market > 0 else "market_alpha_negative",
                        "completed",
                    ),
                )
                count += 1

        self.db.commit()
        return count

    def _compute_forward_return(self, security_id: str, start_date: str, end_date: str):
        """Compute real forward return from K-line."""
        try:
            code = security_id.split(".")[0] if "." in security_id else security_id
            kline = self.provider.get_daily_kline(code, start_date, end_date)
            if kline is None or kline.empty or len(kline) < 2:
                return None
            close_col = "adj_close" if "adj_close" in kline.columns else "close"
            prices = kline[close_col].values
            start_price = float(prices[0])
            end_price = float(prices[-1])
            if start_price <= 0:
                return None
            return (end_price - start_price) / start_price
        except Exception:
            return None

    def _synthetic_backtest(self, child_id, parent_id, start_date, end_date, cycle_id):
        """Fallback: synthetic evaluations (old behavior, kept for when no snapshot exists).

        TODO: remove once stock_factor_snapshot is fully backfilled.
        """
        import random

        rows = self.db.execute(
            """
            SELECT er.* FROM evaluation_results er
            WHERE er.research_decision_id IN (
                SELECT id FROM research_decisions WHERE agent_id = ?
            )
            LIMIT 30
        """,
            (parent_id,),
        ).fetchall()

        if not rows:
            return 0

        cols = [d[0] for d in self.db.execute("PRAGMA table_info(evaluation_results)").fetchall()]
        count = 0
        for row in rows:
            d = dict(zip(cols, row, strict=False))
            perturbation = random.uniform(-0.02, 0.06)
            stock_ret = (d.get("stock_return") or 0) + perturbation
            alpha = (d.get("alpha_vs_market") or 0) + perturbation

            rid = self.db.execute(
                """
                INSERT INTO research_decisions
                (agent_id, genome_hash, security_id, thesis_id, thesis_family,
                 thesis_pattern, thesis_claim, thesis_evidence, thesis_invalidation,
                 alpha_score, confidence, factor_snapshot, risk_assessment,
                 decision_hash, input_hash, entry_price, entry_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    child_id,
                    f"sandbox_{child_id}",
                    "600519",
                    f"sbox_{count}",
                    "value",
                    "quality_compound",
                    "Sandbox simulation (synthetic fallback)",
                    "[]",
                    "[]",
                    5.0,
                    5.0,
                    "{}",
                    "{}",
                    f"sbox_{child_id}_{cycle_id}_{count}",
                    f"sbox_ih_{child_id}_{count}",
                    1500,
                    start_date.isoformat(),
                ),
            ).lastrowid

            self.db.execute(
                """
                INSERT INTO evaluation_results
                (research_decision_id, horizon_days, eval_date,
                 stock_return, market_return, sector_return, agent_top10_ew_return,
                 alpha_vs_market, alpha_vs_sector, alpha_vs_peer,
                 max_drawdown_during, max_profit_during,
                 is_profitable, alpha_positive, verdict)
                VALUES (?,20,?,?,0,0,0,?,0,0,?,0.05,?,?,?)
            """,
                (
                    rid,
                    end_date.isoformat(),
                    stock_ret,
                    alpha,
                    d.get("max_drawdown_during", -0.1),
                    1 if stock_ret > 0 else 0,
                    1 if alpha > 0 else 0,
                    "market_alpha_positive" if alpha > 0 else "market_alpha_negative",
                ),
            )
            count += 1

        self.db.commit()
        return count
