"""
Rule Miner — Automatically generate candidate rules from failure patterns.

Commit 5.1 Fix 3: Mines high-frequency failure patterns and generates
candidate rules for the Thesis Validator.
"""

import json


class RuleMiner:
    """Mine rules from the failure_events table (per-evaluation event log)."""

    def __init__(self, db):
        self.db = db

    def mine(self, min_occurrences: int = 5, min_confidence: float = 0.7) -> int:
        """Mine high-frequency failure patterns → candidate rules.

        Returns number of rules generated.
        """
        conn = self.db.connect()

        rows = conn.execute(
            """
            SELECT failure_type, COUNT(*) as cnt, AVG(severity) as avg_sev
            FROM failure_events
            GROUP BY failure_type
            HAVING cnt >= ? AND avg_sev >= ?
        """,
            (min_occurrences, min_confidence),
        ).fetchall()

        generated = 0
        for row in rows:
            failure_type, cnt, avg_sev = row

            rule = self._build_rule(failure_type, cnt, avg_sev)
            if rule:
                self._save_rule(conn, rule)
                generated += 1

        conn.commit()
        conn.close()
        return generated

    def _build_rule(self, failure_type: str, count: int, avg_sev: float) -> dict:
        """Build a candidate rule from a failure type."""
        rules = {
            "stock_selection_failure": {
                "rule_type": "risk_control",
                "condition_json": json.dumps({"pe_percentile": ">80", "roe_5y_avg": "<0.10"}),
                "action_json": json.dumps({"position_cap": 0.5, "require_committee": True}),
                "confidence": min(0.9, avg_sev + count * 0.02),
            },
            "market_regime_failure": {
                "rule_type": "risk_control",
                "condition_json": json.dumps({"market_risk_score": ">60"}),
                "action_json": json.dumps({"reduce_exposure": 0.3, "cash_target": 0.25}),
                "confidence": min(0.9, avg_sev + count * 0.02),
            },
            "sector_selection_failure": {
                "rule_type": "thesis_filter",
                "condition_json": json.dumps({"sector_momentum": "<0"}),
                "action_json": json.dumps({"require_sector_confirmation": True}),
                "confidence": min(0.9, avg_sev + count * 0.02),
            },
            "timing_failure_early": {
                "rule_type": "thesis_filter",
                "condition_json": json.dumps({"momentum_3m": "<0"}),
                "action_json": json.dumps({"require_momentum_confirmation": True}),
                "confidence": min(0.9, avg_sev + count * 0.02),
            },
            "timing_failure_late_exit": {
                "rule_type": "thesis_filter",
                "condition_json": json.dumps({"exit_opportunity_cost": ">0.10"}),
                "action_json": json.dumps({"trailing_stop": 0.15, "auto_exit": True}),
                "confidence": min(0.9, avg_sev + count * 0.02),
            },
        }

        base = rules.get(failure_type)
        if base is None:
            return None

        return {
            "rule_type": base["rule_type"],
            "condition_json": base["condition_json"],
            "action_json": base["action_json"],
            "confidence": round(base["confidence"], 2),
            "source": f"mined_from_{failure_type}_{count}",
            "status": "pending",
        }

    def _save_rule(self, conn, rule: dict):
        """Persist a candidate rule to the LOCAL candidate_rules_v2 table
        (PostMortemAnalyzer's postmortem DB) — NOT the central EvaluationDB's
        candidate_rules table."""
        conn.execute(
            """
            INSERT INTO candidate_rules_v2
            (rule_type, condition_json, action_json, confidence, source, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                rule["rule_type"],
                rule["condition_json"],
                rule["action_json"],
                rule["confidence"],
                rule["source"],
                rule["status"],
            ),
        )

    def get_promotable_rules(self, min_confidence: float = 0.7) -> list[dict]:
        """Get rules ready for promotion to validated status."""
        conn = self.db.connect()
        rows = conn.execute(
            """
            SELECT * FROM candidate_rules_v2
            WHERE status = 'pending' AND confidence >= ?
            ORDER BY confidence DESC
        """,
            (min_confidence,),
        ).fetchall()
        conn.close()

        cols = [
            "id",
            "rule_type",
            "condition_json",
            "action_json",
            "confidence",
            "source",
            "status",
        ]
        return [dict(zip(cols, r, strict=False)) for r in rows]

    def promote_rules(self, min_confidence: float = 0.7) -> int:
        """Promote high-confidence pending rules to approved."""
        conn = self.db.connect()
        c = conn.execute(
            """
            UPDATE candidate_rules_v2
            SET status = 'approved'
            WHERE status = 'pending' AND confidence >= ?
        """,
            (min_confidence,),
        )
        count = c.rowcount
        conn.commit()
        conn.close()
        return count
