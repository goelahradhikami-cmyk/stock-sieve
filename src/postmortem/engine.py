"""
Post-Mortem Engine — Consume evaluation data, generate failure patterns and rules.

Commit 4: Post-Mortem Engine v1.0

Data flow:
  evaluation_results → classifier → failure_events → candidate_rules_v2 (local postmortem DB)
                                                     → agent_learning_events
  evaluation_results → PostMortemAnalyzer (deep) → post_mortem_analysis (mutation_candidates)
                                                     → collected & fed to EvolutionEngineV1.run_cycle

  NOTE: `post_mortem_analysis` is OWNED BY THIS ENGINE and is distinct from the
  canonical `post_mortems` table in src/data/evaluation_db.py (keyed by
  research_decision_id, used by memory/extractor + evolution/engine.py). We use a
  different name on purpose to avoid clobbering that schema.
"""

import json

import pandas as pd

from src.data.db import managed_connect

from .classifier import FailureClassifier


class PostMortemEngine:
    """Daily post-mortem: classifies failures, generates rules, logs learning."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db_path = db_path
        self.db = managed_connect(self, db_path)
        self.classifier = FailureClassifier()
        self._eval_db = None  # lazily created EvaluationDB for PostMortemAnalyzer
        self._ensure_tables()

    def _ensure_tables(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS failure_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id       INTEGER NOT NULL,
                agent_id            TEXT NOT NULL,
                genome_hash         TEXT,
                failure_type        TEXT NOT NULL,
                severity            REAL DEFAULT 0.0,
                evidence_json       TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (evaluation_id) REFERENCES evaluation_results(id)
            );

            CREATE TABLE IF NOT EXISTS agent_learning_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id            TEXT NOT NULL,
                genome_hash         TEXT,
                event_type          TEXT NOT NULL,
                source_failure_id   INTEGER,
                adjustment_json     TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- PostMortemAnalyzer's LOCAL postmortem DB (separate sqlite file from
            -- EvaluationDB). DISTINCT from evaluation_db.candidate_rules: different
            -- schema (rule_type/condition_json/action_json/confidence/source, status
            -- default 'pending') and different database. The '_v2' suffix is legacy
            -- naming, NOT a versioned upgrade of candidate_rules. Do not confuse them.
            CREATE TABLE IF NOT EXISTS candidate_rules_v2 (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type           TEXT NOT NULL,
                condition_json      TEXT NOT NULL,
                action_json         TEXT NOT NULL,
                confidence          REAL DEFAULT 0.0,
                source              TEXT,
                status              TEXT DEFAULT 'pending'
            );

            -- Persisted output of PostMortemAnalyzer (single-decision deep
            -- analysis). Holds the generated mutation_candidates that feed the
            -- evolution loop. DISTINCT FROM the canonical `post_mortems` table in
            -- src/data/evaluation_db.py (which is keyed by research_decision_id and
            -- consumed by memory/extractor + evolution/engine.py). We deliberately
            -- use a separate table name so the two schemas never collide.
            CREATE TABLE IF NOT EXISTS post_mortem_analysis (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id       INTEGER,
                agent_id            TEXT,
                genome_hash         TEXT,
                error_category      TEXT,
                error_subtype       TEXT,
                mutation_candidates TEXT,
                lessons             TEXT,
                primary_cause       TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                applied             INTEGER DEFAULT 0
            );
        """)
        self.db.commit()

    def run_daily(self):
        """Scan unprocessed evaluation results, classify, and generate patterns."""
        evaluations = self._load_unprocessed()
        if evaluations.empty:
            print("   No new evaluations to post-mortem")
            return 0

        count = 0
        for _, row in evaluations.iterrows():
            eval_dict = row.to_dict()
            attribution = self._get_attribution(row["id"])

            failures = self.classifier.classify(eval_dict, attribution)

            if not failures:
                # Profitable: log reward
                self._log_learning_event(eval_dict, "reward", "positive_performance")
                continue

            for f in failures:
                self._save_failure(eval_dict, f, attribution)

                rule = self._generate_rule(eval_dict, f)
                if rule:
                    self._save_candidate_rule(rule)

                self._log_learning_event(eval_dict, "penalty", f["type"])
                count += 1

            # Deep post-mortem via PostMortemAnalyzer: produces a richer
            # classification + mutation_candidates, persisted to `post_mortems`
            # for the evolution loop. The FailureClassifier above stays the
            # lightweight path for failure_events / candidate_rules_v2.
            self._run_analyzer(eval_dict)

        self.db.commit()
        print(f"   Post-Mortem: {count} failure patterns generated")
        return count

    def _load_unprocessed(self) -> pd.DataFrame:
        """Load evaluation_results that haven't been post-mortemed."""
        try:
            return pd.read_sql_query("""
                SELECT e.* FROM evaluation_results e
                WHERE e.id NOT IN (SELECT fp.evaluation_id FROM failure_events fp WHERE fp.evaluation_id IS NOT NULL)
                ORDER BY e.eval_date DESC
            """, self.db)
        except Exception:
            return pd.read_sql_query(
                "SELECT * FROM evaluation_results ORDER BY eval_date DESC",
                self.db
            )

    def _get_attribution(self, evaluation_id: int) -> dict:
        """Load attribution data for an evaluation."""
        try:
            row = self.db.execute(
                "SELECT * FROM evaluation_attribution WHERE evaluation_id=?",
                (evaluation_id,)
            ).fetchone()
            if row:
                cols = [d[0] for d in self.db.execute(
                    "PRAGMA table_info(evaluation_attribution)"
                ).fetchall()]
                return dict(zip(cols, row))
        except Exception:
            pass
        return {}

    def _save_failure(self, eval_dict: dict, failure: dict, attribution: dict):
        """Persist a failure pattern."""
        evidence = {
            "alpha_jensen": eval_dict.get("alpha_jensen"),
            "max_drawdown": eval_dict.get("max_drawdown_during"),
            "attribution": attribution,
        }
        self.db.execute("""
            INSERT INTO failure_events
            (evaluation_id, agent_id, genome_hash, failure_type, severity, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            eval_dict["id"],
            eval_dict.get("agent_id", "unknown"),
            eval_dict.get("genome_version"),
            failure["type"],
            failure["severity"],
            json.dumps(evidence, default=str),
        ))

    def _generate_rule(self, eval_dict: dict, failure: dict) -> dict | None:
        """Generate a candidate rule from a failure pattern."""
        if failure["type"] == "stock_selection_failure":
            return {
                "rule_type": "risk_control",
                "condition_json": json.dumps({"pe_percentile": ">90"}),
                "action_json": json.dumps({"position_cap": 0.5}),
                "confidence": 0.6,
                "source": str(eval_dict["id"]),
            }
        if failure["type"] == "timing_failure_early":
            return {
                "rule_type": "thesis_filter",
                "condition_json": json.dumps({"momentum_3m": "<0"}),
                "action_json": json.dumps({"require_momentum_confirmation": True}),
                "confidence": 0.65,
                "source": str(eval_dict["id"]),
            }
        if failure["type"] == "market_regime_failure":
            return {
                "rule_type": "risk_control",
                "condition_json": json.dumps({"market_risk_score": ">60"}),
                "action_json": json.dumps({"reduce_exposure": 0.3}),
                "confidence": 0.5,
                "source": str(eval_dict["id"]),
            }
        if failure["type"] == "timing_failure_late_exit":
            return {
                "rule_type": "thesis_filter",
                "condition_json": json.dumps({"exit_opportunity_cost": ">0.10"}),
                "action_json": json.dumps({"trailing_stop": 0.15}),
                "confidence": 0.55,
                "source": str(eval_dict["id"]),
            }
        return None

    def _save_candidate_rule(self, rule: dict):
        """Persist a candidate rule."""
        self.db.execute("""
            INSERT INTO candidate_rules_v2
            (rule_type, condition_json, action_json, confidence, source, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (
            rule["rule_type"], rule["condition_json"],
            rule["action_json"], rule["confidence"], rule["source"],
        ))

    def _log_learning_event(self, eval_dict: dict, event_type: str, detail: str):
        """Record an agent learning event."""
        self.db.execute("""
            INSERT INTO agent_learning_events
            (agent_id, genome_hash, event_type, source_failure_id, adjustment_json)
            VALUES (?, ?, ?, ?, ?)
        """, (
            eval_dict.get("agent_id", "unknown"),
            eval_dict.get("genome_version"),
            event_type,
            eval_dict["id"],
            json.dumps({"detail": detail}),
        ))

    # ── Deep post-mortem (PostMortemAnalyzer) ─────────────

    def _run_analyzer(self, eval_dict: dict):
        """Run the rich PostMortemAnalyzer on one evaluation and persist its
        result (including mutation_candidates) to the ``post_mortem_analysis`` table.

        Failures here are non-fatal: the lightweight FailureClassifier path
        above already did its job; this is purely additive learning.
        """
        from src.data.evaluation_db import EvaluationDB
        from src.evaluation.post_mortem import PostMortemAnalyzer

        if self._eval_db is None:
            self._eval_db = EvaluationDB(self.db_path)
        try:
            analyzer = PostMortemAnalyzer(self._eval_db)
            result = analyzer.run(int(eval_dict["id"]))
            self._save_post_mortem(int(eval_dict["id"]), result)
        except Exception as e:
            print(f"   [post-mortem analyzer skipped] eval_id={eval_dict.get('id')}: {e}")

    def _save_post_mortem(self, evaluation_id: int, result) -> int:
        """Persist a PostMortemResult to the ``post_mortems`` table.

        ``result`` is a ``PostMortemResult`` from ``src.evaluation.post_mortem``.
        Returns the inserted row id.
        """
        cursor = self.db.execute("""
            INSERT INTO post_mortem_analysis
            (evaluation_id, agent_id, genome_hash, error_category, error_subtype,
             mutation_candidates, lessons, primary_cause, applied)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            evaluation_id,
            result.agent_id,
            None,
            result.error_category.value if hasattr(result.error_category, "value") else str(result.error_category),
            result.error_subtype.value if hasattr(result.error_subtype, "value") else str(result.error_subtype),
            json.dumps(result.mutation_candidates, default=str),
            json.dumps(result.lessons, default=str),
            result.primary_cause,
        ))
        self.db.commit()
        return cursor.lastrowid

    def collect_recent_mutations(self, lookback_months: int = 6) -> list[dict]:
        """Collect distinct recent mutation_candidates from persisted post-mortems.

        Used by the evolution loop to bias child genomes toward fixing past
        failures. Deduplicated by (type, target, filter). Returns [] if the
        analyzer has never run or the DB has no post_mortem_analysis table yet.
        """
        from src.data.evaluation_db import EvaluationDB

        if self._eval_db is None:
            self._eval_db = EvaluationDB(self.db_path)
        conn = self._eval_db.connect()
        try:
            rows = conn.execute(
                f"SELECT mutation_candidates FROM post_mortem_analysis "
                f"WHERE created_at > date('now', '-{int(lookback_months)} months')"
            ).fetchall()
        except Exception:
            return []
        finally:
            conn.close()

        all_m = []
        for (mc,) in rows:
            if not mc:
                continue
            try:
                candidates = json.loads(mc) if isinstance(mc, str) else mc
                if isinstance(candidates, list):
                    all_m.extend(candidates)
            except (json.JSONDecodeError, TypeError):
                pass

        seen = {}
        for m in all_m:
            if not isinstance(m, dict):
                continue
            seen[(m.get("type"), m.get("target"), m.get("filter"))] = m
        return list(seen.values())

    def get_pending_rules(self, min_confidence: float = 0.5) -> list[dict]:
        """Get pending candidate rules above confidence threshold."""
        rows = self.db.execute("""
            SELECT * FROM candidate_rules_v2
            WHERE status = 'pending' AND confidence >= ?
        """, (min_confidence,)).fetchall()
        cols = ["id", "rule_type", "condition_json", "action_json",
                "confidence", "source", "status"]
        return [dict(zip(cols, r)) for r in rows]

    def approve_rule(self, rule_id: int):
        """Approve a candidate rule for use in Thesis Validator."""
        self.db.execute(
            "UPDATE candidate_rules_v2 SET status='approved' WHERE id=?",
            (rule_id,)
        )
        self.db.commit()

    def retire_rule(self, rule_id: int):
        """Retire a rule that's no longer effective."""
        self.db.execute(
            "UPDATE candidate_rules_v2 SET status='retired' WHERE id=?",
            (rule_id,)
        )
        self.db.commit()
