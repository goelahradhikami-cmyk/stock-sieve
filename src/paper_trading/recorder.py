"""
Paper Trading Recorder - Phase 4.

Writes paper trading episodes to shadow_trading.db.
Adds execution_mode and decision_fingerprint columns to distinguish
live paper trading from historical backfill.

This module has WRITE access to shadow_trading.db ONLY for inserting
new paper trading episodes. It does NOT modify historical data.
"""

from __future__ import annotations

import json
import sqlite3

from src.utils.logger import get_logger

logger = get_logger(__name__)
SHADOW_DB = "data/shadow_trading.db"


def ensure_columns():
    """Add execution_mode and decision_fingerprint columns if not present."""
    conn = sqlite3.connect(SHADOW_DB)
    try:
        # Check existing columns
        cur = conn.execute("PRAGMA table_info(shadow_episode)")
        cols = [r[1] for r in cur.fetchall()]

        if "execution_mode" not in cols:
            conn.execute(
                "ALTER TABLE shadow_episode ADD COLUMN execution_mode TEXT DEFAULT 'historical'"
            )
            logger.info("recorder: added execution_mode column")

        if "decision_fingerprint" not in cols:
            conn.execute("ALTER TABLE shadow_episode ADD COLUMN decision_fingerprint TEXT")
            logger.info("recorder: added decision_fingerprint column")

        conn.commit()
    finally:
        conn.close()


def record_episode(
    trade_date: str,
    market_state: str,
    confidence: float,
    confidence_band: str,
    decision: str,
    position_target: float,
    vol_20d: float | None,
    vol_change: float | None,
    trend_ma60: float | None,
    breadth: float | None,
    recovery_prob: float | None,
    reason_codes: list[str],
    mus_value: float | None,
    frm_direction: str | None,
    decision_fingerprint_json: str,
    brain_version: str = "4.0-paper-trading",
) -> str:
    """Record a paper trading episode.

    Returns: episode_id
    """
    ensure_columns()
    episode_id = f"E{trade_date.replace('-', '')}"

    conn = sqlite3.connect(SHADOW_DB)
    try:
        # Check if episode already exists (idempotent)
        existing = conn.execute(
            "SELECT 1 FROM shadow_episode WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if existing:
            logger.info("recorder: episode %s already exists, skipping", episode_id)
            return episode_id

        conn.execute(
            """INSERT INTO shadow_episode
               (episode_id, trade_date, market_state, confidence, confidence_band,
                decision, position_target, vol_20d, vol_change, trend_ma60,
                breadth, recovery_prob, reason_codes, brain_version, status,
                execution_mode, decision_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode_id,
                trade_date,
                market_state,
                confidence,
                confidence_band,
                decision,
                position_target,
                vol_20d,
                vol_change,
                trend_ma60,
                breadth,
                recovery_prob,
                json.dumps(reason_codes),
                brain_version,
                "pending",
                "paper_live",
                decision_fingerprint_json,
            ),
        )
        conn.commit()
        logger.info("recorder: recorded episode %s (%s %s)", episode_id, decision, market_state)
        return episode_id
    finally:
        conn.close()
