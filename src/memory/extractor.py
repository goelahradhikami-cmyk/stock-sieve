"""
Experience Extractor — Auto-extract investment memory from evaluation results.

Commit 6-E: Reads evaluation_results + signal_snapshot + post_mortems,
classifies success levels, applies half-life decay.
"""

import hashlib
import json

from src.data.db import managed_connect


class ExperienceExtractor:
    """Extract structured experience entries from completed evaluations."""

    # Dynamic thresholds by holding period (horizon_days)
    THRESHOLDS = {5: 0.03, 20: 0.05, 60: 0.10, 120: 0.15, 250: 0.20}
    FACTOR_KEYS = [
        "roe",
        "pe_percentile",
        "momentum_3m",
        "volatility_60d",
        "fcf_yield",
        "revenue_growth",
        "debt_to_equity",
    ]

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS investment_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                research_decision_id INTEGER,
                evaluation_result_id INTEGER,
                thesis_pattern TEXT,
                market_regime TEXT,
                horizon_days INTEGER,
                factor_snapshot_json TEXT,
                alpha REAL,
                success INTEGER DEFAULT 0,
                error_type TEXT,
                lesson_text TEXT,
                decay_weight REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (research_decision_id) REFERENCES research_decisions(id),
                FOREIGN KEY (evaluation_result_id) REFERENCES evaluation_results(id)
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_agent ON investment_memory(agent_id)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_pattern ON investment_memory(thesis_pattern)"
        )
        self.db.commit()

    def extract_daily(self, limit: int = 100) -> int:
        """Extract memories from unprocessed evaluations."""
        rows = self.db.execute(
            """
            SELECT
                er.id as eval_id, er.research_decision_id,
                er.alpha_vs_market, er.horizon_days, er.max_drawdown_during
            FROM evaluation_results er
            WHERE er.research_decision_id NOT IN (
                SELECT DISTINCT research_decision_id FROM investment_memory
            )
            ORDER BY er.id DESC
            LIMIT ?
        """,
            (limit,),
        ).fetchall()

        if not rows:
            return 0

        count = 0
        for row in rows:
            eval_id, decision_id, alpha, horizon, dd = row
            if alpha is None:
                continue

            # Get thesis/regime/factors from research_decisions
            rd = self.db.execute(
                "SELECT agent_id, thesis_pattern, factor_snapshot FROM research_decisions WHERE id=?",
                (decision_id,),
            ).fetchone()
            if not rd:
                continue

            agent_id, thesis_pattern, factors_raw = rd

            # Get error_type from post_mortems
            pm = self.db.execute(
                "SELECT error_type FROM post_mortems WHERE research_decision_id=?", (decision_id,)
            ).fetchone()
            error_type = pm[0] if pm else None

            # Classify success
            success = self._classify_success(alpha, horizon or 60)

            # Extract key factors
            factors = (
                json.loads(factors_raw) if isinstance(factors_raw, str) else (factors_raw or {})
            )
            key_factors = {k: factors.get(k, 0) for k in self.FACTOR_KEYS}

            # Get market regime
            regime = self._get_regime(decision_id)

            self.db.execute(
                """
                INSERT INTO investment_memory
                (agent_id, research_decision_id, evaluation_result_id,
                 thesis_pattern, market_regime, horizon_days,
                 factor_snapshot_json, alpha, success, error_type)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    agent_id,
                    decision_id,
                    eval_id,
                    thesis_pattern,
                    regime,
                    horizon,
                    json.dumps(key_factors),
                    alpha,
                    success,
                    error_type,
                ),
            )
            count += 1

        # Apply half-life decay
        self.db.execute("""
            UPDATE investment_memory
            SET decay_weight = POWER(0.5, 1.0 * (julianday('now') - julianday(created_at)) / 365)
            WHERE decay_weight = 1.0
        """)
        self.db.commit()
        return count

    def _classify_success(self, alpha: float, horizon: int) -> int:
        """Classify by dynamic horizon-based thresholds. 2=excellent, 1=good, 0=neutral, -1=failure."""
        t = self.THRESHOLDS.get(horizon, 0.05)
        if alpha > t * 2:
            return 2
        elif alpha > 0:
            return 1
        elif alpha > -t:
            return 0
        else:
            return -1

    def _get_regime(self, decision_id: int) -> str:
        """Get market regime at decision time."""
        rd = self.db.execute(
            "SELECT entry_date FROM research_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if not rd or not rd[0]:
            return ""
        try:
            row = self.db.execute(
                "SELECT regime_type FROM market_regime_snapshots WHERE obs_date<=? ORDER BY obs_date DESC LIMIT 1",
                (str(rd[0])[:10],),
            ).fetchone()
            return row[0] if row else ""
        except Exception:
            return ""

    def _stable_hash(self, text: str) -> float:
        """Cross-process stable hash value (0-1)."""
        h = hashlib.md5(text.encode()).hexdigest()
        return int(h[:8], 16) % 100 / 100.0
