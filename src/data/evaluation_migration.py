"""
Evaluation Database Migrations — schema evolution methods (mixin).

Extracted from evaluation_db.py. Provides the ``EvaluationMigrationMixin``
class with all ``migrate_*`` methods. The mixin uses ``@with_conn`` from
``evaluation_crud`` and ``DDL_V21`` from ``evaluation_schema``.
"""

from .evaluation_crud import with_conn
from .evaluation_schema import DDL_V21


class EvaluationMigrationMixin:
    """Schema-migration methods for EvaluationDB.

    All methods use ``@with_conn`` (imported from evaluation_crud) which opens
    a fresh connection from ``self.db_path`` and closes it in ``finally``.
    Each migration is idempotent — ALTER TABLE calls that hit already-existing
    columns are caught and silently skipped.
    """

    @with_conn
    def migrate_v2_1(self, conn):
        """Run v2.0 -> v2.1 migration: add new tables and columns."""
        for col, col_type in [
            ("error_subtype", "TEXT"),
            ("rule_trigger", "TEXT"),
            ("mutation_candidates", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE post_mortems ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # idempotent migration — column may already exist

        # Add missing columns to committee_decisions (v1.0 -> v1.0.1)
        for col, col_type in [
            ("devil_advocate_score", "REAL DEFAULT 50.0"),
            ("weighted_score", "REAL DEFAULT 50.0"),
            ("verdict_reason", "TEXT DEFAULT ''"),
            ("revision_count", "INTEGER DEFAULT 0"),
            ("position_cap_modifier", "REAL DEFAULT 1.0"),
            ("confidence_modifier", "REAL DEFAULT 0.0"),
            ("monitoring_flags_json", "TEXT DEFAULT '[]'"),
            ("required_conditions_json", "TEXT DEFAULT '[]'"),
            ("member_statements_json", "TEXT DEFAULT '{}'"),
            ("devil_advocate_attack_points_json", "TEXT DEFAULT '[]'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE committee_decisions ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # idempotent migration — column may already exist

        # v2.2: evaluation engine boundary fixes
        for col, col_type in [
            ("exit_opportunity_cost", "REAL"),
            ("holding_opportunity_cost", "REAL"),
            ("evaluation_confidence", "REAL DEFAULT 1.0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE evaluation_results ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # idempotent migration — column may already exist
        for col, col_type in [
            ("gross_selection_alpha", "REAL"),
            ("net_selection_alpha", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE evaluation_attribution ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # idempotent migration — column may already exist

        conn.executescript(DDL_V21)
        conn.commit()

    @with_conn
    def migrate_v2_3(self, conn):
        """Run v2.2 -> v2.3: thesis_patterns + signal_snapshot + failure_patterns + evolution_events."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS thesis_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_name TEXT NOT NULL, pattern_family TEXT,
                sample_size INTEGER DEFAULT 0, win_rate REAL,
                avg_alpha REAL, avg_drawdown REAL, market_regime TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tp_name ON thesis_patterns(pattern_name);

            CREATE TABLE IF NOT EXISTS signal_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                research_decision_id INTEGER NOT NULL UNIQUE,
                signal_date DATE NOT NULL, security_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, genome_version TEXT,
                thesis_pattern TEXT, market_regime TEXT,
                factor_values TEXT NOT NULL,
                alpha_score REAL, confidence REAL,
                action TEXT DEFAULT 'BUY', signal_strength REAL,
                entry_date DATE, entry_price REAL,
                entry_method TEXT DEFAULT 'next_open',
                entry_status TEXT DEFAULT 'filled',
                agent_model_version TEXT, factor_engine_version TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS evolution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                agent_id TEXT, parent_id TEXT,
                description TEXT DEFAULT '',
                details_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ee_cycle ON evolution_events(cycle_id);
            CREATE INDEX IF NOT EXISTS idx_ee_agent ON evolution_events(agent_id);
        """)
        # Add PostMortem fields to existing failure_patterns
        for col, col_type in [
            ("evaluation_id", "INTEGER"),
            ("agent_id", "TEXT"),
            ("genome_hash", "TEXT"),
            ("failure_type", "TEXT"),
            ("severity", "REAL DEFAULT 0.0"),
            ("evidence_json", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE failure_patterns ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # idempotent migration — column may already exist
        conn.commit()

    @with_conn
    def migrate_committee_decisions_v2_1_1(self, conn):
        """Upgrade committee_decisions table from v2.1 to v2.1.1.

        v2.1 already has committee_id / *_score / chairman_score / verdict /
        monitoring_flags / debate_transcript; this migration backfills the
        remaining fields needed by spec §4.1 (devil_advocate_score,
        weighted_score, verdict_reason, position_cap_modifier,
        confidence_modifier, and JSON detail columns).
        Idempotent: existing columns are skipped.
        """
        columns = [
            ("devil_advocate_score", "REAL"),
            ("weighted_score", "REAL"),
            ("verdict_reason", "TEXT"),
            ("position_cap_modifier", "REAL DEFAULT 1.0"),
            ("confidence_modifier", "REAL DEFAULT 0.0"),
            ("required_conditions_json", "TEXT"),
            ("member_statements_json", "TEXT"),
            ("devil_advocate_attack", "TEXT"),
        ]
        for col, col_type in columns:
            try:
                conn.execute(
                    f"ALTER TABLE committee_decisions ADD COLUMN {col} {col_type}"
                )
            except Exception:
                pass  # idempotent migration — column may already exist
        conn.commit()
