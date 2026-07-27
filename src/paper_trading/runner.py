"""
Paper Trading Runner - Phase 4.

The operational pipeline for Stock Sieve v4.0 paper trading.

Flow (frozen 6-S.22.0):
  Market Data -> Guardian (timing) -> FRM Gate (recovery filter)
  -> MUS (diagnostic ONLY, NOT in capital path)
  -> Static Bayesian Portfolio -> shadow_trading.db

CRITICAL: MUS has READ-ONLY access to the capital path.
MUS is computed and recorded but NEVER influences position sizing.
This is enforced by architecture: portfolio allocation uses ONLY
guardian_state + confidence_band, never MUS.

Usage:
    python -m src.paper_trading.runner              # today
    python -m src.paper_trading.runner --date 2026-07-22  # specific date
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.paper_trading.fingerprint import create_fingerprint
from src.paper_trading.recorder import record_episode
from src.thesis.bayesian_allocation import BayesianAllocationEngine
from src.thesis.confidence_overlay import RecoveryConfidence
from src.thesis.state_transition import StateTransitionEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)

CACHE_DB = "data/cache.db"
EVAL_DB = "data/evaluation.db"
SHADOW_DB = "data/shadow_trading.db"

# Guardian v1.1 frozen parameters (from investment_brain_v1_freeze.yaml)
BAND_POSITION_MAP = {
    "blocked": 0.0,
    "small": 0.0,
    "normal": 0.6,
    "full": 1.0,
}


def run_paper_day(trade_date: str | None = None) -> str | None:
    """Run paper trading for one day.

    Args:
        trade_date: ISO date string. Defaults to today.

    Returns: episode_id if recorded, None if skipped.
    """
    if trade_date is None:
        trade_date = date.today().isoformat()

    print(f"\n{'=' * 60}")
    print(f"Paper Trading Runner - {trade_date}")
    print(f"{'=' * 60}")

    # ── 1. Guardian: State Transition Engine ──────────────
    print("\n1. Guardian (State Transition)...")
    ste = StateTransitionEngine(cache_db=CACHE_DB)

    # Ensure state history is up to date
    end_date = trade_date
    start_date = (date.fromisoformat(trade_date) - timedelta(days=365)).isoformat()
    ste.run(start_date, end_date)

    state_record = ste.get_state(trade_date)
    if state_record is None:
        print(f"   No state data for {trade_date}. Skipping.")
        return None

    market_state = state_record.state
    print(f"   Market state: {market_state}")

    # ── 2. Guardian: Recovery Confidence ──────────────────
    print("\n2. Guardian (Recovery Confidence)...")
    rc = RecoveryConfidence(eval_db=EVAL_DB, cache_db=CACHE_DB)
    conf_result = rc.compute(trade_date)

    confidence = conf_result.confidence
    confidence_band = conf_result.confidence_band
    allows_anomaly = conf_result.allows_anomaly

    print(f"   Confidence: {confidence:.1f} ({confidence_band})")
    print(f"   Allows anomaly: {allows_anomaly}")

    # ── 3. Decision: BUY or BLOCK ─────────────────────────
    # Guardian v1.1: BUY if confidence_band in (normal, full), else BLOCK
    if confidence_band in ("normal", "full"):
        decision = "BUY"
        position_target = BAND_POSITION_MAP[confidence_band]
    else:
        decision = "BLOCK"
        position_target = 0.0

    print(f"\n3. Decision: {decision} (position_target={position_target})")

    # ── 4. FRM Direction Gate (only for BUY) ──────────────
    frm_direction = None
    reason_codes = []

    if decision == "BUY":
        print("\n4. FRM Direction Gate...")
        # FRM is per-stock. For paper trading at portfolio level,
        # we record the gate as "active" but don't select individual stocks.
        # Individual stock selection is RETIRED (6-S.18.0).
        # FRM here means: the gate is open for recovery stocks.
        frm_direction = "gate_active"
        reason_codes.append("RECOVERY_CONFIRMED")
        print(f"   FRM gate: {frm_direction} (no stock selection - retired)")
    else:
        reason_codes.append("LOW_CONFIDENCE")
        if confidence_band == "blocked":
            reason_codes.append("VOL_NOT_REPAIRING")
        elif confidence_band == "small":
            reason_codes.append("TREND_NOT_CONFIRMED")

    # ── 5. MUS Calculation (DIAGNOSTIC ONLY) ─────────────
    # MUS = Guardian's confidence score, used as uncertainty indicator.
    # It is recorded for audit purposes but does NOT influence position.
    print("\n5. MUS (diagnostic only, NOT in capital path)...")
    mus_value = confidence  # MUS IS the confidence score, reinterpreted as uncertainty
    print(f"   MUS value: {mus_value:.1f}")
    print(f"   *** MUS does NOT influence position_target={position_target} ***")

    # ── 6. Static Bayesian Portfolio ──────────────────────
    print("\n6. Static Bayesian Portfolio...")
    bae = BayesianAllocationEngine(eval_db=EVAL_DB)
    allocation = bae.compute_allocation(market_state, as_of_date=trade_date)
    print(f"   Allocation (doctrine weights): {allocation}")
    print(f"   Position target: {position_target} (from confidence_band, NOT MUS)")

    # ── 7. Decision Fingerprint ───────────────────────────
    fingerprint_json = create_fingerprint(
        guardian_state=market_state,
        guardian_confidence=confidence,
        confidence_band=confidence_band,
        decision=decision,
        position_target=position_target,
        frm_direction=frm_direction,
        mus_value=mus_value,
    )

    # ── 8. Record to shadow_trading.db ────────────────────
    print("\n7. Recording to shadow_trading.db...")
    episode_id = record_episode(
        trade_date=trade_date,
        market_state=market_state,
        confidence=confidence,
        confidence_band=confidence_band,
        decision=decision,
        position_target=position_target,
        vol_20d=state_record.vol_20d,
        vol_change=state_record.vol_change,
        trend_ma60=state_record.trend,
        breadth=state_record.breadth,
        recovery_prob=state_record.recovery_prob,
        reason_codes=reason_codes,
        mus_value=mus_value,
        frm_direction=frm_direction,
        decision_fingerprint_json=fingerprint_json,
    )

    print(f"\n{'=' * 60}")
    print(f"Episode: {episode_id}")
    print(f"Decision: {decision}")
    print(f"Market state: {market_state}")
    print(f"Confidence: {confidence:.1f} ({confidence_band})")
    print(f"Position target: {position_target}")
    print(f"MUS (diagnostic): {mus_value:.1f}")
    print("Fingerprint: capital sources = guardian + frm + static_policy")
    print("             forbidden inputs used = NONE")
    print("             mus_used = False")
    print(f"{'=' * 60}")

    return episode_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Trading Runner")
    parser.add_argument("--date", help="Trading date (YYYY-MM-DD). Default: today.")
    args = parser.parse_args()

    episode_id = run_paper_day(args.date)
    if episode_id:
        print(f"\n✅ Paper trading recorded: {episode_id}")
    else:
        print("\n⏭️ No episode recorded (insufficient data)")


if __name__ == "__main__":
    main()
