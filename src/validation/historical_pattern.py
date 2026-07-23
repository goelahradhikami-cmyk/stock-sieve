"""
Historical Pattern Analyzer — Score thesis pattern against historical outcomes.

Phase 5A-2 §4.3

Composite scoring formula:
  score = (0.4 × win_rate + 0.3 × alpha_quality + 0.2 × risk_adjusted_return
           + 0.1 × sample_confidence) × 100

Where:
  - alpha_quality = normalized avg_alpha_vs_market
  - risk_adjusted_return = avg_return / avg_max_drawdown
  - sample_confidence = min(1.0, sample_size / 50)
"""


class HistoricalPatternAnalyzer:
    """Analyzes thesis pattern performance from thesis_outcomes table."""

    MIN_SAMPLE_SIZE = 5

    def __init__(self, db):
        self.db = db

    def analyze(self, thesis_pattern: str, agent_id: str = None) -> tuple[float, dict]:
        """Analyze historical performance of a thesis pattern.

        Returns:
            (score 0-100, analog dict with raw data)
        """
        conn = self.db.connect()

        if agent_id:
            row = conn.execute(
                """
                SELECT * FROM thesis_outcomes
                WHERE thesis_pattern = ? AND agent_id = ?
                ORDER BY last_updated DESC LIMIT 1
            """,
                (thesis_pattern, agent_id),
            ).fetchone()
        else:
            # Aggregate across agents
            row = conn.execute(
                """
                SELECT
                    thesis_pattern,
                    SUM(sample_size) as sample_size,
                    AVG(win_rate) as win_rate,
                    AVG(avg_alpha_vs_market) as avg_alpha_vs_market,
                    AVG(avg_alpha_vs_sector) as avg_alpha_vs_sector,
                    AVG(max_drawdown_avg) as max_drawdown_avg
                FROM thesis_outcomes
                WHERE thesis_pattern = ?
                GROUP BY thesis_pattern
            """,
                (thesis_pattern,),
            ).fetchone()

        conn.close()

        analog = dict(row) if row else {}

        sample_size = analog.get("sample_size", 0) or 0

        if sample_size < self.MIN_SAMPLE_SIZE:
            return 50.0, {
                "status": "insufficient_data",
                "sample_size": sample_size,
                "message": f"Need {self.MIN_SAMPLE_SIZE} samples, have {sample_size}",
            }

        # ── Composite scoring ─────────────────────────────

        win_rate = analog.get("win_rate", 0.0) or 0.0

        # Alpha quality: normalize avg_alpha_vs_market to 0-1
        avg_alpha = analog.get("avg_alpha_vs_market", 0.0) or 0.0
        alpha_quality = min(1.0, max(0.0, (avg_alpha + 0.2) / 0.4))  # -0.2→0, 0.2→1.0

        # Risk-adjusted return
        avg_return = win_rate * 0.10  # approximate
        avg_dd = analog.get("max_drawdown_avg", 0.15) or 0.15
        risk_adj = min(2.0, max(0.0, avg_return / abs(avg_dd))) / 2.0 if avg_dd != 0 else 0.5

        # Sample confidence
        sample_conf = min(1.0, sample_size / 50.0)

        score = (0.4 * win_rate + 0.3 * alpha_quality + 0.2 * risk_adj + 0.1 * sample_conf) * 100

        return round(score, 1), {
            "status": "ok",
            "sample_size": sample_size,
            "win_rate": round(win_rate, 3),
            "avg_alpha_vs_market": round(avg_alpha, 4),
            "alpha_quality": round(alpha_quality, 3),
            "risk_adjusted": round(risk_adj, 3),
            "sample_confidence": round(sample_conf, 3),
        }
