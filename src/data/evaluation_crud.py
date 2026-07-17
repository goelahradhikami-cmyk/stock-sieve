"""
Evaluation Database CRUD — insert and query methods (mixin).

Extracted from evaluation_db.py. Provides the ``with_conn`` decorator and
the ``EvaluationCRUDMixin`` class that houses all insert / query / committee /
quant-auditor methods. The mixin expects the concrete class to define
``self.db_path`` (a path string) in its ``__init__``.
"""

import sqlite3
import json
import functools
from datetime import datetime
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)


def with_conn(func):
    """Open a SQLite connection for the duration of ``func`` and guarantee it
    is closed — even if the function raises.

    The decorated method receives ``conn`` as its second positional argument
    (right after ``self``). It should call ``conn.commit()`` itself but must
    NOT call ``conn.close()``; that is handled here in ``finally``.

    This eliminates the connection-leak-on-exception pattern that previously
    existed because every method did ``conn = self.connect()`` ...
    ``conn.close()`` without a ``try/finally``.
    """

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return func(self, conn, *args, **kwargs)
        finally:
            conn.close()

    return wrapper


class EvaluationCRUDMixin:
    """Insert / query / committee / quant-auditor methods for EvaluationDB.

    All methods use the ``@with_conn`` decorator which opens a fresh
    connection from ``self.db_path`` and closes it in ``finally``.
    """

    # ═══════════════════════════════════════════════════════
    # Insert helpers
    # ═══════════════════════════════════════════════════════

    @with_conn
    def insert_research_decision(self, conn, agent_id: str, genome_hash: str,
                                  security_id: str, thesis: dict,
                                  alpha_score: float, confidence: float,
                                  factor_snapshot: dict, risk_assessment: dict,
                                  entry_price: float, entry_date: str,
                                  decision_hash: str, input_hash: str,
                                  model_version: str = None) -> int:
        c = conn.execute("""
            INSERT INTO research_decisions
            (agent_id, genome_hash, security_id, thesis_id, thesis_family,
             thesis_pattern, thesis_claim, thesis_evidence, thesis_catalyst,
             thesis_invalidation, thesis_horizon,
             alpha_score, confidence, factor_snapshot, risk_assessment,
             decision_hash, input_hash, model_version,
             entry_price, entry_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, genome_hash, security_id,
            thesis.get("thesis_id"), thesis.get("family"), thesis.get("pattern"),
            thesis.get("claim"), str(thesis.get("evidence", [])),
            thesis.get("catalyst"), str(thesis.get("invalidation", [])),
            thesis.get("horizon"),
            alpha_score, confidence, str(factor_snapshot), str(risk_assessment),
            decision_hash, input_hash, model_version,
            entry_price, entry_date,
        ))
        conn.commit()
        rid = c.lastrowid
        return rid

    @with_conn
    def insert_evaluation_result(self, conn, research_decision_id: int,
                                  horizon_days: int, eval_date: str,
                                  stock_return: float, market_return: float,
                                  sector_return: float, agent_top10_return: float,
                                  max_drawdown: float, max_profit: float,
                                  verdict: str) -> int:
        alpha_mkt = stock_return - market_return
        alpha_sec = stock_return - sector_return
        alpha_peer = stock_return - agent_top10_return
        c = conn.execute("""
            INSERT INTO evaluation_results
            (research_decision_id, horizon_days, eval_date,
             stock_return, market_return, sector_return, agent_top10_ew_return,
             alpha_vs_market, alpha_vs_sector, alpha_vs_peer,
             max_drawdown_during, max_profit_during,
             is_profitable, alpha_positive, verdict)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            research_decision_id, horizon_days, eval_date,
            stock_return, market_return, sector_return, agent_top10_return,
            alpha_mkt, alpha_sec, alpha_peer,
            max_drawdown, max_profit,
            1 if stock_return > 0 else 0,
            1 if alpha_mkt > 0 else 0,
            verdict,
        ))
        conn.commit()
        eid = c.lastrowid
        return eid

    @with_conn
    def insert_post_mortem(self, conn, research_decision_id: int, agent_id: str,
                            failure_date: str, error_type: str, primary_cause: str,
                            wrong_assumption: str = None, missed_signal: str = None,
                            suggested_actions: list = None) -> int:
        c = conn.execute("""
            INSERT INTO post_mortems
            (research_decision_id, agent_id, failure_date, error_type,
             primary_cause, wrong_assumption, missed_signal, suggested_actions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            research_decision_id, agent_id, failure_date, error_type,
            primary_cause, wrong_assumption, missed_signal,
            str(suggested_actions) if suggested_actions else None,
        ))
        conn.commit()
        pid = c.lastrowid
        return pid

    @with_conn
    def insert_decision_event(self, conn, agent_id: str, event_type: str,
                               reference_id: int = None, reference_type: str = None,
                               event_data: dict = None, event_summary: str = None):
        conn.execute("""
            INSERT INTO decision_events
            (agent_id, event_type, reference_id, reference_type,
             event_data, event_summary, event_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, event_type, reference_id, reference_type,
            str(event_data) if event_data else None,
            event_summary, datetime.now().isoformat(),
        ))
        conn.commit()

    @with_conn
    def insert_genome_snapshot(self, conn, agent_id: str, strategy_genus: str,
                                strategy_species: str, generation: int,
                                parent_agent_id: str, genome_hash: str,
                                genome_yaml: str, birth_date: str,
                                mutation_reason: str = None,
                                mutation_detail: str = None,
                                status: str = "active") -> int:
        c = conn.execute("""
            INSERT INTO agent_genome_snapshots
            (agent_id, strategy_genus, strategy_species, generation,
             parent_agent_id, genome_hash, genome_yaml,
             mutation_reason, mutation_detail,
             birth_date, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, strategy_genus, strategy_species, generation,
            parent_agent_id, genome_hash, genome_yaml,
            mutation_reason, mutation_detail,
            birth_date, status,
        ))
        conn.commit()
        sid = c.lastrowid
        return sid

    @with_conn
    def insert_market_regime(self, conn, obs_date: str, regime_type: str,
                              risk_score: float, growth_env: float,
                              value_env: float, momentum_env: float,
                              defensive_env: float, liquidity: float,
                              pe_pct: float = None, pb_pct: float = None,
                              indicators: dict = None):
        conn.execute("""
            INSERT OR REPLACE INTO market_regime_snapshots
            (obs_date, regime_type, risk_score,
             growth_env_score, value_env_score, momentum_env_score,
             defensive_env_score, liquidity_score,
             market_pe_percentile, market_pb_percentile, indicators_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            obs_date, regime_type, risk_score,
            growth_env, value_env, momentum_env,
            defensive_env, liquidity,
            pe_pct, pb_pct,
            str(indicators) if indicators else None,
        ))
        conn.commit()

    @with_conn
    def update_agent_performance(self, conn, agent_id: str, period_type: str,
                                  period_start: str, period_end: str,
                                  total_return: float, sharpe: float,
                                  max_drawdown: float, win_rate: float,
                                  alpha_vs_market: float, personality_score: float):
        conn.execute("""
            INSERT OR REPLACE INTO agent_performance
            (agent_id, period_type, period_start, period_end,
             total_return, sharpe_ratio, max_drawdown, win_rate,
             alpha_vs_market, personality_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, period_type, period_start, period_end,
            total_return, sharpe, max_drawdown, win_rate,
            alpha_vs_market, personality_score,
        ))
        conn.commit()

    # ═══════════════════════════════════════════════════════
    # Query helpers
    # ═══════════════════════════════════════════════════════

    @with_conn
    def get_agent_performance(self, conn, agent_id: str, period_type: str = "quarterly",
                               limit: int = 8) -> list[dict]:
        rows = conn.execute("""
            SELECT * FROM agent_performance
            WHERE agent_id = ? AND period_type = ?
            ORDER BY period_end DESC LIMIT ?
        """, (agent_id, period_type, limit)).fetchall()
        return [dict(r) for r in rows]

    @with_conn
    def get_genome_snapshot(self, conn, agent_id: str, status: str = "active") -> Optional[dict]:
        row = conn.execute("""
            SELECT * FROM agent_genome_snapshots
            WHERE agent_id = ? AND status = ?
            ORDER BY birth_date DESC LIMIT 1
        """, (agent_id, status)).fetchone()
        return dict(row) if row else None

    @with_conn
    def get_recent_decisions(self, conn, agent_id: str, limit: int = 20) -> list[dict]:
        rows = conn.execute("""
            SELECT * FROM research_decisions
            WHERE agent_id = ?
            ORDER BY entry_date DESC LIMIT ?
        """, (agent_id, limit)).fetchall()
        return [dict(r) for r in rows]

    @with_conn
    def get_post_mortems_since(self, conn, agent_id: str, since_date: str) -> list[dict]:
        rows = conn.execute("""
            SELECT * FROM post_mortems
            WHERE agent_id = ? AND failure_date >= ? AND applied = 0
            ORDER BY failure_date DESC
        """, (agent_id, since_date)).fetchall()
        return [dict(r) for r in rows]

    @with_conn
    def get_factor_memory(self, conn, factor_name: str, regime: str = None,
                           limit: int = 20) -> list[dict]:
        if regime:
            rows = conn.execute("""
                SELECT * FROM factor_memory
                WHERE factor_name = ? AND market_regime = ?
                ORDER BY period_end DESC LIMIT ?
            """, (factor_name, regime, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM factor_memory
                WHERE factor_name = ?
                ORDER BY period_end DESC LIMIT ?
            """, (factor_name, limit)).fetchall()
        return [dict(r) for r in rows]

    @with_conn
    def get_survival_check_data(self, conn, agent_ids: list[str]) -> list[dict]:
        """Get last 2 quarters of personality_score for survival check."""
        placeholders = ",".join("?" * len(agent_ids))
        rows = conn.execute(f"""
            SELECT agent_id, period_end, personality_score
            FROM agent_performance
            WHERE agent_id IN ({placeholders})
              AND period_type = 'quarterly'
            ORDER BY agent_id, period_end DESC
        """, agent_ids).fetchall()
        return [dict(r) for r in rows]

    @with_conn
    def get_diversity_distance(self, conn, agent_id: str) -> Optional[float]:
        """Get min investment_identity cosine distance to other active agents.

        Parses each agent's genome_yaml, extracts the investment_identity
        dimensions vector, and returns the smallest cosine distance between
        ``agent_id`` and any other active agent.  Returns None when there are
        fewer than two active agents with usable vectors.
        """
        import yaml
        import math

        rows = conn.execute("""
            SELECT agent_id, genome_yaml FROM agent_genome_snapshots
            WHERE status = 'active'
            ORDER BY birth_date DESC
        """).fetchall()

        # latest vector per agent (DESC ordering -> first hit wins)
        vectors: dict[str, dict] = {}
        for r in rows:
            aid = r["agent_id"]
            if aid in vectors:
                continue
            try:
                genome = yaml.safe_load(r["genome_yaml"]) or {}
            except Exception as e:
                logger.warning("evaluation_db: parse genome_yaml failed for agent %s: %s", aid, e)
                continue
            dims = genome.get("investment_identity", {}).get("dimensions", {})
            if dims:
                vectors[aid] = dims

        target = vectors.get(agent_id)
        if not target:
            return None

        others = [v for aid, v in vectors.items() if aid != agent_id]
        if not others:
            return None

        def _cosine_distance(a: dict, b: dict) -> float:
            keys = set(a) | set(b)
            va = [a.get(k, 50) for k in keys]
            vb = [b.get(k, 50) for k in keys]
            dot = sum(x * y for x, y in zip(va, vb))
            na = math.sqrt(sum(x * x for x in va))
            nb = math.sqrt(sum(y * y for y in vb))
            if na == 0 or nb == 0:
                return 1.0
            return 1.0 - dot / (na * nb)

        return min(_cosine_distance(target, v) for v in others)

    @with_conn
    def get_post_mortems_with_mutations(self, conn, agent_id: str,
                                          lookback_months: int = 6) -> list[dict]:
        """Get post-mortems with mutation_candidates for evolution engine."""
        rows = conn.execute(f"""
            SELECT * FROM post_mortems
            WHERE agent_id = ?
              AND created_at > date('now', '-{lookback_months} months')
              AND applied = 0
              AND mutation_candidates IS NOT NULL
            ORDER BY created_at DESC
        """, (agent_id,)).fetchall()
        return [dict(r) for r in rows]

    @with_conn
    def get_failure_patterns(self, conn, min_occurrence: int = 3) -> list[dict]:
        """Get failure patterns exceeding occurrence threshold."""
        rows = conn.execute("""
            SELECT * FROM failure_patterns
            WHERE occurrence_count >= ?
            ORDER BY occurrence_count DESC
        """, (min_occurrence,)).fetchall()
        return [dict(r) for r in rows]

    @with_conn
    def upsert_failure_pattern(self, conn, pattern_id: str, pattern_name: str,
                                error_category: str, error_subtype: str = None):
        """Create or increment a failure pattern."""
        existing = conn.execute(
            "SELECT id, occurrence_count FROM failure_patterns WHERE pattern_id = ?",
            (pattern_id,)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE failure_patterns
                SET occurrence_count = occurrence_count + 1,
                    last_occurrence = date('now'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE pattern_id = ?
            """, (pattern_id,))
        else:
            conn.execute("""
                INSERT INTO failure_patterns
                (pattern_id, pattern_name, error_category, error_subtype,
                 occurrence_count, last_occurrence, pattern_confidence)
            VALUES (?, ?, ?, ?, 1, date('now'), 0.0)
        """, (pattern_id, pattern_name, error_category, error_subtype))
        conn.commit()

    # ═══════════════════════════════════════════════════════
    # Committee Decisions (Phase 5A-3) — v2.1.1 upgrade
    # ═══════════════════════════════════════════════════════

    @with_conn
    def insert_committee_decision(self, conn, decision) -> int:
        """Persist a CommitteeDecision.

        Input can be a CommitteeDecision object or a plain dict (use getattr /
        .get to avoid reverse-importing the agents layer and causing a circular
        dependency).
        """
        if isinstance(decision, dict):
            g = lambda attr, default=None: decision.get(attr, default)
        else:
            g = lambda attr, default=None: getattr(decision, attr, default)

        c = conn.execute("""
            INSERT INTO committee_decisions
            (committee_id, research_decision_id,
             valuation_score, industry_score, risk_score, quant_score,
             devil_advocate_score, chairman_score, weighted_score,
             verdict, verdict_reason,
             position_cap_modifier, confidence_modifier,
             monitoring_flags, required_conditions_json,
             member_statements_json, devil_advocate_attack, debate_transcript)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            g("committee_id"),
            g("research_decision_id"),
            g("valuation_score"),
            g("industry_score"),
            g("risk_score"),
            g("quant_score"),
            g("devil_advocate_score"),
            g("chairman_score", g("weighted_score")),  # chairman score; system chairman composite = weighted_score, use independent chairman score if available in future
            g("weighted_score"),
            g("verdict"),
            g("verdict_reason"),
            g("position_cap_modifier", 1.0),
            g("confidence_modifier", 0.0),
            json.dumps(g("monitoring_flags", []) or [], ensure_ascii=False),
            json.dumps(g("required_conditions", []) or [], ensure_ascii=False),
            json.dumps(g("member_statements", {}) or {}, ensure_ascii=False),
            g("devil_advocate_attack", ""),
            g("debate_transcript", ""),
        ))
        conn.commit()
        rid = c.lastrowid
        return rid

    @with_conn
    def get_committee_decision(self, conn, committee_id: str) -> Optional[dict]:
        """Query a persisted committee decision by committee_id."""
        row = conn.execute(
            "SELECT * FROM committee_decisions WHERE committee_id = ?",
            (committee_id,),
        ).fetchone()
        return dict(row) if row else None

    # ═══════════════════════════════════════════════════════
    # Quant Auditor queries (spec §5.4)
    # ═══════════════════════════════════════════════════════

    @with_conn
    def get_alpha_persistence(self, conn, agent_id: str, months: int = 12) -> float:
        """Researcher alpha persistence over recent months = fraction of positive-alpha evaluations.
        Returns 0.5 (neutral, no penalty for new researchers) when no data."""
        rows = conn.execute("""
                SELECT er.alpha_vs_market
                FROM evaluation_results er
                JOIN research_decisions rd ON er.research_decision_id = rd.id
                WHERE rd.agent_id = ?
                  AND er.evaluated_at >= date('now', ?)
            """, (agent_id, f"-{months} months")).fetchall()
        if not rows:
            return 0.5
        positive = sum(1 for r in rows if (r[0] or 0) > 0)
        return positive / len(rows)

    @with_conn
    def get_pattern_ic(self, conn, thesis_pattern: str, months: int = 6) -> float:
        """Average alpha_vs_market (IC proxy) for this pattern over recent months.
        Returns 0.0 when no data."""
        row = conn.execute("""
                SELECT AVG(er.alpha_vs_market)
                FROM evaluation_results er
                JOIN research_decisions rd ON er.research_decision_id = rd.id
                WHERE rd.thesis_pattern = ?
                  AND er.evaluated_at >= date('now', ?)
            """, (thesis_pattern, f"-{months} months")).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    @with_conn
    def get_pattern_sample_size(self, conn, thesis_pattern: str) -> int:
        """Evaluation sample size for this pattern (overfit risk check). Returns 0 when no data."""
        row = conn.execute("""
                SELECT COUNT(*)
                FROM evaluation_results er
                JOIN research_decisions rd ON er.research_decision_id = rd.id
                WHERE rd.thesis_pattern = ?
            """, (thesis_pattern,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
