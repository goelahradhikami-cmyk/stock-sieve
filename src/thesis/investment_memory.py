"""
Investment Memory v1 - Commit 6-S.7.

Stores "what the system believes, why it believes it, and how much to trust it."

Four belief states (per 6-S.6.5 design):
  VERIFIED:     audit_score > 0.85, cross-regime validated -> can evolve
  CONDITIONAL:  evidence exists but missing audit dimensions -> can use, can't evolve
  HYPOTHESIS:   interesting signal, insufficient evidence -> observe only
  REJECTED:     failed causal/sample audit -> archive, don't propagate

Memory is NOT a fixed rulebook. It's a living document that:
  - Records what was observed (evidence)
  - Records what was concluded (belief)
  - Records what's missing (gaps)
  - Decays over time (forgetting)
  - Can be upgraded/downgraded as new evidence arrives

Usage:
    from src.thesis.investment_memory import InvestmentMemory
    mem = InvestmentMemory()
    mem.store_belief(state, doctrine, evidence, verdict)
    beliefs = mem.get_beliefs("CONFIRMED_RECOVERY")
    # Returns list of BeliefRecord with state + confidence + decay
"""

from __future__ import annotations

import sqlite3
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)


# Belief states
VERIFIED = "VERIFIED"
CONDITIONAL = "CONDITIONAL"
HYPOTHESIS = "HYPOTHESIS"
REJECTED = "REJECTED"

# Decay: half-life 365 days
DECAY_HALF_LIFE_DAYS = 365
DECAY_LAMBDA = math.log(2) / DECAY_HALF_LIFE_DAYS


@dataclass
class BeliefRecord:
    """One belief in the investment memory."""
    belief_id: str               # e.g. "CONFIRMED_RECOVERY+quality"
    market_state: str
    doctrine: str
    belief_state: str            # VERIFIED / CONDITIONAL / HYPOTHESIS / REJECTED

    # Evidence
    n_theses: int
    n_success: int
    win_rate: float
    avg_return: float
    avg_alpha: float             # beta-adjusted return

    # Audit scores
    sample_score: float          # 0-1
    regime_score: float          # 0-1
    causal_score: float          # 0-1
    decay_score: float           # 0-1
    audit_score: float           # composite

    # Missing evidence
    missing_audits: list[str]    # e.g. ["regime_stability", "time_decay"]

    # Memory management
    created_at: str
    last_updated: str
    decay_factor: float          # current decay (starts at 1.0)
    can_evolve: bool             # only VERIFIED + CONDITIONAL can evolve

    # Bayesian
    posterior_alpha: float
    posterior_beta: float

    def to_dict(self) -> dict:
        return {
            "belief_id": self.belief_id,
            "state": self.market_state,
            "doctrine": self.doctrine,
            "belief_state": self.belief_state,
            "n": self.n_theses,
            "win_rate": round(self.win_rate, 3),
            "avg_return": round(self.avg_return, 4),
            "avg_alpha": round(self.avg_alpha, 4),
            "audit_score": round(self.audit_score, 3),
            "missing": self.missing_audits,
            "decay": round(self.decay_factor, 3),
            "can_evolve": self.can_evolve,
            "posterior": f"Beta({self.posterior_alpha:.1f}, {self.posterior_beta:.1f})",
        }


class InvestmentMemory:
    """Investment Memory v1: stores beliefs with audit status + decay.

    This is the "experience database" that Evolution v4 reads from.
    Only VERIFIED and CONDITIONAL beliefs can influence evolution.
    HYPOTHESIS beliefs are observed but don't propagate.
    REJECTED beliefs are archived and blocked.
    """

    def __init__(self, eval_db: str = "data/evaluation.db"):
        self.eval_db = eval_db
        self._ensure_table()

    def _ensure_table(self):
        conn = sqlite3.connect(self.eval_db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS investment_memory (
                belief_id           TEXT PRIMARY KEY,
                market_state        TEXT NOT NULL,
                doctrine            TEXT NOT NULL,
                belief_state        TEXT NOT NULL,
                n_theses            INTEGER DEFAULT 0,
                n_success           INTEGER DEFAULT 0,
                win_rate            REAL DEFAULT 0.5,
                avg_return          REAL DEFAULT 0,
                avg_alpha           REAL DEFAULT 0,
                sample_score        REAL DEFAULT 0,
                regime_score        REAL DEFAULT 0,
                causal_score        REAL DEFAULT 0,
                decay_score         REAL DEFAULT 0,
                audit_score         REAL DEFAULT 0,
                missing_audits      TEXT DEFAULT '[]',
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                decay_factor        REAL DEFAULT 1.0,
                can_evolve          INTEGER DEFAULT 0,
                posterior_alpha     REAL DEFAULT 2.0,
                posterior_beta      REAL DEFAULT 2.0
            );
            CREATE INDEX IF NOT EXISTS idx_im_state ON investment_memory(market_state);
            CREATE INDEX IF NOT EXISTS idx_im_evolve ON investment_memory(can_evolve);
        """)
        conn.commit()
        conn.close()

    def store_belief(self, market_state: str, doctrine: str,
                      n_theses: int, n_success: int,
                      avg_return: float, avg_alpha: float,
                      sample_score: float, regime_score: float,
                      causal_score: float, decay_score: float,
                      missing_audits: list[str] = None) -> BeliefRecord:
        """Store or update a belief in memory.

        Args:
            market_state: PANIC/STABILIZING/EARLY_RECOVERY/CONFIRMED_RECOVERY
            doctrine: quality/contrarian/value
            n_theses, n_success: evidence counts
            avg_return: average thesis return
            avg_alpha: beta-adjusted average return
            sample/regime/causal/decay_score: audit scores (0-1)
            missing_audits: list of audit dimensions not yet validated

        Returns: BeliefRecord
        """
        belief_id = f"{market_state}+{doctrine}"
        audit_score = (sample_score + regime_score + causal_score + decay_score) / 4

        # Determine belief state
        if audit_score >= 0.85:
            belief_state = VERIFIED
            can_evolve = True
        elif audit_score >= 0.4:
            belief_state = CONDITIONAL
            can_evolve = True
        elif audit_score >= 0.2:
            belief_state = HYPOTHESIS
            can_evolve = False
        else:
            belief_state = REJECTED
            can_evolve = False

        win_rate = n_success / n_theses if n_theses > 0 else 0.5
        posterior_alpha = 2.0 + n_success
        posterior_beta = 2.0 + (n_theses - n_success)

        missing = missing_audits or []
        now = datetime.now().isoformat()

        conn = sqlite3.connect(self.eval_db)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO investment_memory
                (belief_id, market_state, doctrine, belief_state,
                 n_theses, n_success, win_rate, avg_return, avg_alpha,
                 sample_score, regime_score, causal_score, decay_score,
                 audit_score, missing_audits,
                 created_at, last_updated, decay_factor, can_evolve,
                 posterior_alpha, posterior_beta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?)
            """, (
                belief_id, market_state, doctrine, belief_state,
                n_theses, n_success, win_rate, avg_return, avg_alpha,
                sample_score, regime_score, causal_score, decay_score,
                audit_score, json.dumps(missing),
                now, now, can_evolve,
                posterior_alpha, posterior_beta,
            ))
            conn.commit()
        finally:
            conn.close()

        return BeliefRecord(
            belief_id=belief_id, market_state=market_state, doctrine=doctrine,
            belief_state=belief_state, n_theses=n_theses, n_success=n_success,
            win_rate=win_rate, avg_return=avg_return, avg_alpha=avg_alpha,
            sample_score=sample_score, regime_score=regime_score,
            causal_score=causal_score, decay_score=decay_score,
            audit_score=audit_score, missing_audits=missing,
            created_at=now, last_updated=now, decay_factor=1.0,
            can_evolve=can_evolve,
            posterior_alpha=posterior_alpha, posterior_beta=posterior_beta,
        )

    def get_beliefs(self, market_state: str = None) -> list[BeliefRecord]:
        """Get beliefs, optionally filtered by market state."""
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            if market_state:
                rows = conn.execute(
                    "SELECT * FROM investment_memory WHERE market_state=? ORDER BY audit_score DESC",
                    (market_state,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM investment_memory ORDER BY market_state, audit_score DESC"
                ).fetchall()
        finally:
            conn.close()

        return [self._row_to_belief(r) for r in rows]

    def get_evolvable_beliefs(self) -> list[BeliefRecord]:
        """Get beliefs that can influence Evolution v4."""
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM investment_memory WHERE can_evolve=1 ORDER BY audit_score DESC"
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_belief(r) for r in rows]

    def apply_decay(self, as_of_date: str = None) -> int:
        """Apply time decay to all beliefs.

        Beliefs not updated recently have their decay_factor reduced.
        This implements the "forgetting" mechanism.

        Returns: number of beliefs updated.
        """
        if as_of_date is None:
            as_of_date = date.today().isoformat()

        conn = sqlite3.connect(self.eval_db)
        try:
            rows = conn.execute(
                "SELECT belief_id, last_updated, decay_factor FROM investment_memory"
            ).fetchall()

            updated = 0
            for belief_id, last_updated, current_decay in rows:
                try:
                    last_dt = datetime.fromisoformat(last_updated)
                    now_dt = datetime.fromisoformat(as_of_date)
                    days_since = (now_dt - last_dt).days
                    new_decay = current_decay * math.exp(-DECAY_LAMBDA * max(0, days_since))
                    conn.execute(
                        "UPDATE investment_memory SET decay_factor=? WHERE belief_id=?",
                        (new_decay, belief_id),
                    )
                    updated += 1
                except (ValueError, TypeError):
                    pass

            conn.commit()
        finally:
            conn.close()

        return updated

    def get_memory_summary(self) -> dict:
        """Get summary of all beliefs by state."""
        conn = sqlite3.connect(self.eval_db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM investment_memory").fetchone()[0]
            by_state = {}
            for row in conn.execute(
                "SELECT belief_state, COUNT(*) FROM investment_memory GROUP BY belief_state"
            ).fetchall():
                by_state[row[0]] = row[1]

            evolvable = conn.execute(
                "SELECT COUNT(*) FROM investment_memory WHERE can_evolve=1"
            ).fetchone()[0]

            avg_decay = conn.execute(
                "SELECT AVG(decay_factor) FROM investment_memory"
            ).fetchone()[0] or 1.0
        finally:
            conn.close()

        return {
            "total": total,
            "by_state": by_state,
            "evolvable": evolvable,
            "avg_decay": round(avg_decay, 3),
        }

    def _row_to_belief(self, row: sqlite3.Row) -> BeliefRecord:
        return BeliefRecord(
            belief_id=row["belief_id"],
            market_state=row["market_state"],
            doctrine=row["doctrine"],
            belief_state=row["belief_state"],
            n_theses=row["n_theses"],
            n_success=row["n_success"],
            win_rate=row["win_rate"],
            avg_return=row["avg_return"],
            avg_alpha=row["avg_alpha"],
            sample_score=row["sample_score"],
            regime_score=row["regime_score"],
            causal_score=row["causal_score"],
            decay_score=row["decay_score"],
            audit_score=row["audit_score"],
            missing_audits=json.loads(row["missing_audits"]) if row["missing_audits"] else [],
            created_at=row["created_at"],
            last_updated=row["last_updated"],
            decay_factor=row["decay_factor"],
            can_evolve=bool(row["can_evolve"]),
            posterior_alpha=row["posterior_alpha"],
            posterior_beta=row["posterior_beta"],
        )
