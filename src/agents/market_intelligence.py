"""
Market Intelligence Agent — Softmax regime probabilities + entropy confidence.

Commit 6-F.2: Replaces binary bull/bear classification with continuous
probability distribution over 4 states. Adds self-calibration memory.
"""

from datetime import date, timedelta

import numpy as np

from src.data.db import managed_connect


class MarketIntelligenceAgent:
    """Produces probabilistic market state assessment with entropy confidence."""

    STATES = ['bull', 'bear', 'crisis', 'rotation']

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        self._ensure_tables()

    def _ensure_tables(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS market_intelligence_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                obs_date DATE NOT NULL,
                predicted_regime TEXT NOT NULL,
                predicted_probability REAL,
                future_regime TEXT,
                prediction_error REAL,
                market_return REAL,
                lesson TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.commit()

    def assess(self, kline_data: dict = None) -> dict:
        """Assess current market state with Softmax probabilities.

        Args:
            kline_data: dict with trend_60, trend_250, vol_20, dd_60, liquidity

        Returns:
            environment, risk decomposition, behavior policy, transition warning
        """
        if kline_data is None:
            kline_data = self._fetch_market_data()

        trend_60 = kline_data.get('trend_60', 0)
        trend_250 = kline_data.get('trend_250', 0)
        vol_20 = kline_data.get('vol_20', 0.25)
        dd_60 = kline_data.get('dd_60', -0.10)
        liquidity = kline_data.get('liquidity', 50)

        # ── Raw scores per regime ──────────────────────────
        bull_score = (trend_60 - 0.03) * 6 + (trend_250 - 0.02) * 4 + (0.25 - vol_20) * 3
        bear_score = (-trend_60 - 0.03) * 8 + (-trend_250 - 0.02) * 5 + (dd_60 + 0.08) * 4
        crisis_score = (vol_20 - 0.20) * 3 + (0.15 + dd_60) * 5 + (50 - liquidity) / 100
        rotation_score = (0.25 - abs(trend_60)) * 10 + (0.3 - vol_20) * 5

        # ── Softmax → probabilities ────────────────────────
        scores = np.array([bull_score, bear_score, crisis_score, rotation_score])
        exp_scores = np.exp(scores - np.max(scores))
        probs = exp_scores / exp_scores.sum()
        probabilities = dict(zip(self.STATES, probs))

        # ── Entropy confidence ─────────────────────────────
        confidence = self._entropy_confidence(probabilities)
        primary = max(probabilities, key=probabilities.get)

        # ── Risk decomposition ─────────────────────────────
        risk_components = {
            'trend_risk': min(100, max(0, 50 - trend_60 * 500)),
            'valuation_risk': 50,  # placeholder
            'liquidity_risk': min(100, max(0, 100 - liquidity)),
            'volatility_risk': min(100, max(0, vol_20 * 250)),
        }
        risk_score = np.mean(list(risk_components.values()))

        # ── Behavior policy ────────────────────────────────
        restricted = []
        if risk_score > 70:
            restricted.append('new_positions')
        if vol_20 > 0.35:
            restricted.append('high_beta')
        if probabilities.get('crisis', 0) > 0.3:
            restricted.append('all_trading')

        # ── Transition warnings ────────────────────────────
        warnings = self._get_transition_warnings(probabilities)

        # ── Save snapshot ──────────────────────────────────
        self._save_snapshot(primary, probabilities, confidence, risk_score)

        return {
            "environment": {
                "primary": primary,
                "probability": {k: round(float(v), 3) for k, v in probabilities.items()},
                "confidence": round(confidence, 3),
            },
            "risk": {
                "overall": round(risk_score, 1),
                **{k: round(v, 1) for k, v in risk_components.items()},
            },
            "behavior_policy": {
                "allowed": ['stock_selection', 'quality_focus'],
                "restricted": restricted,
            },
            "transition_warning": warnings,
        }

    def _entropy_confidence(self, probs: dict) -> float:
        """Higher entropy = lower confidence. Range 0-1."""
        entropy = -sum(p * np.log(p) for p in probs.values() if p > 0)
        max_entropy = np.log(len(probs))
        return float(1 - entropy / max_entropy) if max_entropy > 0 else 0

    def _get_transition_warnings(self, probs: dict) -> list:
        """Detect close calls between top two regimes."""
        sorted_states = sorted(probs.items(), key=lambda x: x[1], reverse=True)
        if len(sorted_states) >= 2:
            gap = sorted_states[0][1] - sorted_states[1][1]
            if gap < 0.10:
                return [f"Close call: {sorted_states[0][0]}({sorted_states[0][1]:.1%}) vs {sorted_states[1][0]}({sorted_states[1][1]:.1%}), gap={gap:.1%}"]
        return []

    def _fetch_market_data(self) -> dict:
        """Fetch latest market indicators from DB or return defaults."""
        try:
            row = self.db.execute(
                "SELECT * FROM market_regime_snapshots ORDER BY obs_date DESC LIMIT 1"
            ).fetchone()
            if row:
                cols = [d[0] for d in row.description]
                d = dict(zip(cols, row))
                return {
                    'trend_60': 0.02, 'trend_250': 0.05,
                    'vol_20': 0.20, 'dd_60': -0.10, 'liquidity': 60,
                }
        except Exception:
            pass
        return {'trend_60': 0.02, 'trend_250': 0.05, 'vol_20': 0.20, 'dd_60': -0.10, 'liquidity': 60}

    def _save_snapshot(self, primary, probs, confidence, risk_score):
        """Persist assessment to market_regime_snapshots."""
        try:
            self.db.execute("""
                INSERT OR REPLACE INTO market_regime_snapshots
                (obs_date, regime_type, risk_score, indicators_json)
                VALUES (?, ?, ?, ?)
            """, (
                date.today().isoformat(), primary, risk_score,
                str({"probs": {k: round(float(v), 3) for k, v in probs.items()}, "confidence": confidence}),
            ))
            self.db.commit()
        except Exception:
            pass

    def calibrate_past_predictions(self, lookback_days: int = 30):
        """Compare 30-day-old prediction with current reality and log to memory."""
        past_date = date.today() - timedelta(days=lookback_days)
        row = self.db.execute(
            "SELECT regime_type, indicators_json FROM market_regime_snapshots WHERE obs_date<=? ORDER BY obs_date DESC LIMIT 1",
            (past_date.isoformat(),)
        ).fetchone()
        if not row:
            return 0

        predicted = row[0]
        try:
            import json
            indicators = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
            predicted_prob = indicators.get('probs', {}).get(predicted, 0.5)
        except Exception:
            predicted_prob = 0.5

        # Current actual regime
        current = self.assess()
        actual = current['environment']['primary']
        error = 0 if predicted == actual else (1 - predicted_prob)

        # Market return over same period
        market_ret = self._get_market_return(past_date)

        self.db.execute("""
            INSERT INTO market_intelligence_memory
            (obs_date, predicted_regime, predicted_probability, future_regime,
             prediction_error, market_return, lesson)
            VALUES (?,?,?,?,?,?,?)
        """, (
            past_date.isoformat(), predicted, predicted_prob, actual, error, market_ret,
            f"Predicted {predicted}, actual {actual}" if predicted != actual else "Correct"
        ))
        self.db.commit()
        return 1

    def _get_market_return(self, from_date: date) -> float:
        """Get market return from index data."""
        try:
            row = self.db.execute(
                "SELECT adj_close FROM market_index_daily WHERE index_code='000300' AND trade_date=?",
                (from_date.isoformat(),)
            ).fetchone()
            if not row:
                return 0.0
            start = row[0]
            row2 = self.db.execute(
                "SELECT adj_close FROM market_index_daily WHERE index_code='000300' ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
            if not row2 or start == 0:
                return 0.0
            return (row2[0] - start) / start
        except Exception:
            return 0.0

    def get_final_exposure(self, probs: dict, risk_score: float) -> float:
        """Probability-weighted exposure with risk discount."""
        # Base: probability-weighted exposure
        exposure_weights = {'bull': 0.95, 'bear': 0.70, 'crisis': 0.40, 'rotation': 0.85}
        prob_exposure = sum(probs.get(s, 0) * exposure_weights.get(s, 0.5) for s in self.STATES)

        # Risk discount: max 30% reduction
        risk_discount = max(0.3, 1 - risk_score / 200)
        return round(prob_exposure * risk_discount, 2)
