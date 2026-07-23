"""
Market State Transition Engine - Commit 6-S.5.4.

Upgrades MarketStateMachine (6-S.5.3) from a daily classifier to a true
state machine with transitions. States persist across days and can only
upgrade/downgrade through explicit rules.

Key improvement over 6-S.5.3:
  6-S.5.3: each day independently classified -> can flip-flop
  6-S.5.4: state persists, transitions require confirmation -> stable

Five states (per user design):
  PANIC -> STABILIZING -> EARLY_RECOVERY -> CONFIRMED_RECOVERY -> EUPHORIA

Transitions are bidirectional (can downgrade), but require:
  - Upgrade: N consecutive days of confirming signals
  - Downgrade: immediate on deteriorating signal

Data source: 1194 continuous trading days from market_regime_snapshots
+ market_index_daily (000300) for vol/trend/breadth computation.

Usage:
    from src.thesis.state_transition import StateTransitionEngine
    engine = StateTransitionEngine()
    engine.run("2021-08-11", "2026-07-17")  # build full history
    state = engine.get_state("2023-01-15")  # query specific date
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)

# State definitions and allowed transitions
STATES = ["PANIC", "STABILIZING", "EARLY_RECOVERY", "CONFIRMED_RECOVERY", "EUPHORIA"]
STATE_ORDER = {s: i for i, s in enumerate(STATES)}

# Anomaly permission per state
STATE_ANOMALY_WEIGHT = {
    "PANIC": 0.0,
    "STABILIZING": 0.2,
    "EARLY_RECOVERY": 0.6,
    "CONFIRMED_RECOVERY": 1.0,
    "EUPHORIA": 0.3,  # reduce anomaly in euphoria (overpriced)
}

# Transition confirmation periods
UPGRADE_CONFIRMATION_DAYS = 3  # need 3 consecutive days of confirming signal


@dataclass
class StateRecord:
    """One day's state record."""

    date: str
    state: str
    anomaly_weight: float
    # Raw signals
    vol_20d: float
    vol_change: float
    trend: float
    recovery_prob: float
    breadth: float
    # Transition info
    previous_state: str
    transition_reason: str
    confirmation_count: int  # how many consecutive days of upgrade signal

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "state": self.state,
            "anomaly_weight": round(self.anomaly_weight, 2),
            "vol_20d": round(self.vol_20d, 3),
            "vol_change": round(self.vol_change, 4),
            "trend": round(self.trend, 3),
            "recovery_prob": round(self.recovery_prob, 3),
            "breadth": round(self.breadth, 3),
            "previous_state": self.previous_state,
            "transition_reason": self.transition_reason,
            "confirmation_count": self.confirmation_count,
        }


class StateTransitionEngine:
    """Rule-based state machine with transition confirmation.

    Processes the full 1194-day market history day by day, maintaining
    a state variable that can only change through explicit transition rules.

    States:
      PANIC: vol high, breadth weak -> no anomaly
      STABILIZING: vol contracting, selling pressure fading -> small anomaly
      EARLY_RECOVERY: vol + breadth improving -> normal anomaly
      CONFIRMED_RECOVERY: sustained recovery -> full anomaly
      EUPHORIA: overbought -> reduce anomaly (protect from chasing)
    """

    def __init__(self, eval_db: str = "data/evaluation.db", cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db
        self._state_history: dict[str, StateRecord] = {}

    def run(self, start_date: str, end_date: str) -> int:
        """Build state history from start to end date.

        Processes each trading day sequentially, applying transition rules.

        Returns: number of days processed.
        """
        # Load 000300 daily data
        conn = sqlite3.connect(self.cache_db)
        try:
            rows = conn.execute(
                "SELECT trade_date, close, amount FROM market_index_daily "
                "WHERE index_code='000300' AND trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date",
                (start_date, end_date),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 60:
            logger.warning("state_transition: only %d rows (need >=60)", len(rows))
            return 0

        # Compute indicators for each day
        dates = [r[0] for r in rows]
        closes = [r[1] for r in rows if r[1]]

        returns = np.diff(closes) / closes[:-1]

        # Initial state
        current_state = "PANIC"
        confirmation_count = 0

        for i in range(60, len(closes)):
            d = dates[i]

            # Compute indicators
            vol_20d = float(np.std(returns[i - 20 : i]) * np.sqrt(252))
            vol_60d = float(np.std(returns[i - 60 : i]) * np.sqrt(252))
            vol_change = vol_20d - vol_60d

            ma60 = float(np.mean(closes[i - 60 : i]))
            trend = float(np.clip((closes[i] - ma60) / ma60, -1, 1)) if ma60 > 0 else 0.0

            # Recovery probability (simplified: vol_score + trend_score)
            vol_score = max(0, min(1, 0.5 - vol_change * 5))
            trend_score = max(0, min(1, 0.5 + trend * 0.5))
            recovery_prob = 0.5 * vol_score + 0.3 * trend_score + 0.2 * 0.5  # breadth placeholder

            # Breadth from regime snapshot (if available)
            breadth = self._get_breadth(d)

            # Determine target state from indicators
            target_state, reason = self._classify_day(
                vol_20d, vol_change, trend, recovery_prob, breadth, current_state
            )

            # Apply transition rules
            if target_state == current_state:
                confirmation_count = 0
                transition_reason = f"maintained {current_state}"
            elif STATE_ORDER.get(target_state, 0) > STATE_ORDER.get(current_state, 0):
                # Upgrade attempt
                confirmation_count += 1
                if confirmation_count >= UPGRADE_CONFIRMATION_DAYS:
                    current_state = target_state
                    transition_reason = (
                        f"UPGRADED to {target_state}: {reason} (confirmed {confirmation_count}d)"
                    )
                    confirmation_count = 0
                else:
                    transition_reason = f"upgrade signal to {target_state} ({confirmation_count}/{UPGRADE_CONFIRMATION_DAYS}d): {reason}"
            else:
                # Downgrade: immediate
                current_state = target_state
                transition_reason = f"DOWNGRADED to {target_state}: {reason}"
                confirmation_count = 0

            anomaly_weight = STATE_ANOMALY_WEIGHT.get(current_state, 0.0)

            record = StateRecord(
                date=d,
                state=current_state,
                anomaly_weight=anomaly_weight,
                vol_20d=vol_20d,
                vol_change=vol_change,
                trend=trend,
                recovery_prob=recovery_prob,
                breadth=breadth,
                previous_state=current_state,
                transition_reason=transition_reason,
                confirmation_count=confirmation_count,
            )
            self._state_history[d] = record

        return len(self._state_history)

    def get_state(self, trade_date: str) -> StateRecord | None:
        """Get market state for a specific date."""
        # Exact match
        if trade_date in self._state_history:
            return self._state_history[trade_date]
        # Nearest prior date
        prior_dates = [d for d in self._state_history if d <= trade_date]
        if prior_dates:
            nearest = max(prior_dates)
            return self._state_history[nearest]
        return None

    def allows_anomaly(self, trade_date: str) -> bool:
        """Should anomaly bets be allowed on this date?"""
        state = self.get_state(trade_date)
        if state:
            return state.anomaly_weight >= 0.5
        return False

    def _classify_day(
        self,
        vol_20d: float,
        vol_change: float,
        trend: float,
        recovery_prob: float,
        breadth: float,
        current_state: str,
    ) -> tuple[str, str]:
        """Classify target state from daily indicators.

        Returns: (target_state, reason)
        """
        # EUPHORIA: extreme overbought
        if trend > 0.15 and vol_20d < 0.12 and recovery_prob > 0.75:
            return "EUPHORIA", f"trend={trend:+.2f} overbought + low vol"

        # CONFIRMED_RECOVERY: strong vol contraction + positive trend + decent breadth
        if vol_change < -0.03 and trend > 0.02 and breadth > 0.45:
            return (
                "CONFIRMED_RECOVERY",
                f"vol_chg={vol_change:+.4f} strong + trend={trend:+.3f} + breadth={breadth:.2f}",
            )

        # EARLY_RECOVERY: vol contracting + recovery improving
        if vol_change < -0.01 and recovery_prob > 0.48 and breadth > 0.40:
            return (
                "EARLY_RECOVERY",
                f"vol_chg={vol_change:+.4f} + recovery={recovery_prob:.3f} + breadth={breadth:.2f}",
            )

        # STABILIZING: vol not expanding, selling pressure fading
        if vol_change < 0.0 and breadth > 0.35:
            return "STABILIZING", f"vol_chg={vol_change:+.4f} contracting + breadth={breadth:.2f}"

        # PANIC: high vol, weak breadth
        if vol_20d > 0.25 or (vol_change > 0.02 and breadth < 0.40):
            return "PANIC", f"vol_20d={vol_20d:.3f} high / vol expanding + breadth weak"

        # Default: maintain current state if none of above
        return current_state, "no clear signal, maintaining"

    def _get_breadth(self, trade_date: str) -> float:
        """Get breadth (momentum > 50 ratio) from snapshot if available."""
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN momentum_score > 50 THEN 1 ELSE 0 END) "
                "FROM stock_factor_snapshot WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
            if row and row[0] > 0:
                return float(row[1] / row[0])
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)
        finally:
            conn.close()
        return 0.5  # neutral when no snapshot

    def get_state_distribution(self) -> dict[str, int]:
        """Count days in each state."""
        from collections import Counter

        counts = Counter(r.state for r in self._state_history.values())
        return dict(counts)

    def get_transitions(self) -> list[StateRecord]:
        """Get all state transition events."""
        transitions = []
        prev_state = None
        for d in sorted(self._state_history.keys()):
            r = self._state_history[d]
            if prev_state and r.state != prev_state:
                transitions.append(r)
            prev_state = r.state
        return transitions
