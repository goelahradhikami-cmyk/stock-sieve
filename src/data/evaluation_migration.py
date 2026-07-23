"""
Evaluation Database Migrations — schema evolution methods (mixin).

Extracted from evaluation_db.py. Provides the ``EvaluationMigrationMixin``
class with all ``migrate_*`` methods. The mixin uses ``@with_conn`` from
``evaluation_crud`` and ``DDL_V21`` from ``evaluation_schema``.
"""

from .evaluation_crud import with_conn
from .evaluation_schema import DDL_V21
# reconciliation.py imports ONLY the standard library at module load, so this
# cross-package import does not form a cycle (evaluation_db -> evaluation_migration
# -> reconciliation, with reconciliation importing EvaluationDB lazily).
from src.audit.reconciliation import DDL_RECONCILIATION

from src.utils.logger import get_logger

logger = get_logger(__name__)


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
    def migrate_v2_5_reconciliation(self, conn):
        """Run v2.4 -> v2.5: create the atomic decision_reconciliation table.

        Idempotent: uses CREATE TABLE IF NOT EXISTS via executescript. Safe to
        call on every pipeline run.
        """
        conn.executescript(DDL_RECONCILIATION)
        conn.commit()

    @with_conn
    def migrate_v2_6_doctrine(self, conn):
        """Run v2.5 -> v2.6: Commit 6-L Doctrine Engine tables + version columns.

        Creates:
          - doctrine_genome (evolvable doctrine DNA, 补丁1)
          - confidence_calibration (补丁5)
          - thesis_genome (6-L.4)
          - stock_factor_snapshot (6-L.6 data infrastructure, 补丁4 percentiles)
        Adds engine_version / doctrine_id columns to signal_snapshot,
        agent_genome_snapshots, research_decisions for v1/v2 dual-track.
        Idempotent: all CREATE TABLE IF NOT EXISTS, ALTER wrapped in try/except.
        """
        from src.agents.doctrine_engine import (
            DDL_DOCTRINE_GENOME, DDL_CONFIDENCE_CALIBRATION, DDL_THESIS_GENOME,
        )
        from src.factors.snapshot_schema import DDL_STOCK_FACTOR_SNAPSHOT

        conn.executescript(DDL_DOCTRINE_GENOME)
        conn.executescript(DDL_CONFIDENCE_CALIBRATION)
        conn.executescript(DDL_THESIS_GENOME)
        conn.executescript(DDL_STOCK_FACTOR_SNAPSHOT)

        # Add version columns to existing tables (idempotent)
        for col, col_type in [
            ("engine_version", "TEXT DEFAULT 'v1_legacy'"),
            ("doctrine_id", "TEXT"),
        ]:
            for table in ("signal_snapshot", "research_decisions"):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                except Exception:
                    pass  # idempotent - column may already exist
        try:
            conn.execute("ALTER TABLE agent_genome_snapshots ADD COLUMN doctrine_version TEXT DEFAULT 'v1'")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)

        conn.commit()

    @with_conn
    def migrate_v2_7_survival_arena(self, conn):
        """Run v2.6 -> v2.7: Commit 6-L.7 Doctrine Survival Arena tables.

        Creates:
          - doctrine_fitness_history (per doctrine per date: total/beta/sector/residual/fitness)
          - doctrine_regime_statistics (Bayesian smoothing: doctrine × regime samples)
        Adds:
          - evaluation_results.residual_alpha (the "true alpha" after stripping
            market beta + sector exposure; replaces alpha_vs_market in fitness)
        Idempotent: all CREATE TABLE IF NOT EXISTS, ALTER wrapped in try/except.
        """
        # 1. evaluation_results.residual_alpha column
        try:
            conn.execute("ALTER TABLE evaluation_results ADD COLUMN residual_alpha REAL")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)

        # 1b. doctrine_fitness_history: add pick_returns_json (for breadth calc)
        # and alpha_windows_json (for stability calc) - Commit 6-L.8
        try:
            conn.execute("ALTER TABLE doctrine_fitness_history ADD COLUMN pick_returns_json TEXT")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)
        try:
            conn.execute("ALTER TABLE doctrine_fitness_history ADD COLUMN alpha_quality REAL")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)
        # Commit 6-M: Alpha Origin Attribution fields
        try:
            conn.execute("ALTER TABLE doctrine_fitness_history ADD COLUMN factor_alpha REAL")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)
        try:
            conn.execute("ALTER TABLE doctrine_fitness_history ADD COLUMN selection_alpha REAL")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)
        try:
            conn.execute("ALTER TABLE doctrine_fitness_history ADD COLUMN factor_independence REAL")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)
        try:
            conn.execute("ALTER TABLE doctrine_fitness_history ADD COLUMN timing_quality REAL")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)
        try:
            conn.execute("ALTER TABLE doctrine_fitness_history ADD COLUMN luck_penalty REAL")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)
        try:
            conn.execute("ALTER TABLE doctrine_fitness_history ADD COLUMN origin_quality REAL")
        except Exception as exc:
            logger.debug("operation failed (was silently ignored): %s", exc)

        # 2. doctrine_fitness_history
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS doctrine_fitness_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                doctrine_id     TEXT NOT NULL,
                trade_date      DATE NOT NULL,
                market_regime   TEXT,
                total_return    REAL,
                market_beta     REAL,
                sector_return   REAL,
                residual_alpha  REAL,
                drawdown        REAL,
                fitness         REAL,
                rank            INTEGER,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_dfh_doctrine ON doctrine_fitness_history(doctrine_id);
            CREATE INDEX IF NOT EXISTS idx_dfh_regime ON doctrine_fitness_history(doctrine_id, market_regime);
            CREATE INDEX IF NOT EXISTS idx_dfh_date ON doctrine_fitness_history(trade_date);
        """)

        # 3. doctrine_regime_statistics (Bayesian smoothing source)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS doctrine_regime_statistics (
                doctrine_id         TEXT NOT NULL,
                regime              TEXT NOT NULL,
                sample_count        INTEGER DEFAULT 0,
                avg_residual_alpha  REAL DEFAULT 0.0,
                win_rate            REAL DEFAULT 0.0,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (doctrine_id, regime)
            );
            CREATE INDEX IF NOT EXISTS idx_drs_doctrine ON doctrine_regime_statistics(doctrine_id);
        """)

        # 4. doctrine_survival_history (Commit 6-L.9.1 - Survival Logging)
        # Records every doctrine's survival/breeding decision per generation,
        # so we can study how the investment ecosystem evolves over time.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS doctrine_survival_history (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                generation          INTEGER NOT NULL,
                doctrine_id         TEXT NOT NULL,
                parent_doctrine     TEXT,
                children_count      INTEGER DEFAULT 0,
                fitness             REAL,
                alpha_quality       REAL,
                regime_adaptation   REAL,
                survival_status     TEXT,
                survival_reason     TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_dsh_generation ON doctrine_survival_history(generation);
            CREATE INDEX IF NOT EXISTS idx_dsh_doctrine ON doctrine_survival_history(doctrine_id);
        """)

        # 5. alpha_decay_history (Commit 6-N.1 - Alpha Decay Tracking)
        # Tracks how a doctrine's alpha degrades over generations.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS alpha_decay_history (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                doctrine_id         TEXT NOT NULL,
                generation          INTEGER NOT NULL,
                factor_alpha        REAL,
                selection_alpha     REAL,
                origin_quality      REAL,
                crowding_score      REAL,
                decay_rate          REAL,
                alpha_half_life     INTEGER,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_adh_doctrine ON alpha_decay_history(doctrine_id);
            CREATE INDEX IF NOT EXISTS idx_adh_generation ON alpha_decay_history(generation);
        """)

        # 6. doctrine_memory (Commit 6-N.5 - Doctrine Memory Bank)
        # Records which doctrine types worked in which environments.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS doctrine_memory (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                doctrine_family     TEXT NOT NULL,
                market_regime       TEXT NOT NULL,
                avg_selection_alpha REAL,
                sample_count        INTEGER DEFAULT 0,
                last_seen           DATE,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (doctrine_family, market_regime)
            );
            CREATE INDEX IF NOT EXISTS idx_dm_family ON doctrine_memory(doctrine_family);
        """)

        # 7. doctrine_survival_memory (Commit 6-N.2b - Survival Memory)
        # Full lifecycle record: why born, why died, peak alpha, competitors.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS doctrine_survival_memory (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                doctrine_id         TEXT NOT NULL,
                family              TEXT,
                birth_generation    INTEGER,
                death_generation    INTEGER,
                birth_reason        TEXT,
                death_reason        TEXT,
                alpha_peak          REAL,
                alpha_final         REAL,
                alpha_half_life     INTEGER,
                avg_crowding        REAL,
                top_competitors     TEXT,
                lifespan            INTEGER,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_dsm_doctrine ON doctrine_survival_memory(doctrine_id);
            CREATE INDEX IF NOT EXISTS idx_dsm_family ON doctrine_survival_memory(family);
        """)

        # 8. thesis_ledger (Commit 6-S.3 - Investment Thesis Memory)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS thesis_ledger (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                thesis_id           TEXT NOT NULL,
                trade_date          DATE NOT NULL,
                eval_date           DATE,
                code                TEXT NOT NULL,
                anomaly_type        TEXT,
                price_drawdown_12m  REAL,
                roe                 REAL,
                margin_change       REAL,
                market_pessimism    REAL,
                business_strength   REAL,
                divergence_score    REAL,
                recovery_probability REAL,
                market_regime       TEXT,
                quality_verdict     TEXT,
                quality_confidence  REAL,
                contrarian_verdict  TEXT,
                contrarian_confidence REAL,
                value_verdict       TEXT,
                value_confidence    REAL,
                consensus           TEXT,
                kill_criteria_triggered TEXT,
                action              TEXT,
                actual_return       REAL,
                thesis_status       TEXT,
                failure_type        TEXT,
                failure_reason      TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tl_date ON thesis_ledger(trade_date);
            CREATE INDEX IF NOT EXISTS idx_tl_code ON thesis_ledger(code);
            CREATE INDEX IF NOT EXISTS idx_tl_status ON thesis_ledger(thesis_status);
        """)

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
