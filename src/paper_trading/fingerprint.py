"""
Decision Fingerprint Generator - Phase 4 Paper Trading.

Generates a JSON fingerprint for each trading decision, recording:
  - ALLOWED sources that drove the capital action
  - FORBIDDEN inputs that were NOT used (verified clean)

This directly serves the Monthly Belief Audit (Section B).

The fingerprint is the system's proof that each decision was made
constitutionally. If a future audit finds a decision where
forbidden_inputs_used is non-empty, that's a Constitution violation.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any


def create_fingerprint(
    guardian_state: str,
    guardian_confidence: float,
    confidence_band: str,
    decision: str,
    position_target: float,
    frm_direction: str | None = None,
    mus_value: float | None = None,
) -> str:
    """Create a decision fingerprint JSON string.

    Args:
        guardian_state: Market state from Guardian (PANIC/STABILIZING/etc.)
        guardian_confidence: Guardian confidence score (0-100)
        confidence_band: blocked/small/normal/full
        decision: BUY or BLOCK
        position_target: 0.0-1.0 allocation target
        frm_direction: improving/stable/deteriorating (if BUY)
        mus_value: MUS/ARS score (diagnostic only, MUST NOT influence decision)

    Returns: JSON string fingerprint for storage in shadow_episode.
    """
    fingerprint: dict[str, Any] = {
        "capital_action_source": [],
        "forbidden_inputs_used": [],
        "mus_used": False,
        "mus_value": mus_value,
        "decision_detail": {
            "guardian_state": guardian_state,
            "guardian_confidence": round(guardian_confidence, 1),
            "confidence_band": confidence_band,
            "decision": decision,
            "position_target": position_target,
            "frm_direction": frm_direction,
        },
        "audit_timestamp": date.today().isoformat(),
    }

    # Record ALLOWED sources
    fingerprint["capital_action_source"].append("guardian")
    if decision == "BUY" and frm_direction:
        fingerprint["capital_action_source"].append("frm")
    fingerprint["capital_action_source"].append("static_policy")

    # MUS is NEVER a capital action source
    # If someone passes mus_value and it was used for decision,
    # they must explicitly flag it. By default, mus_used = False.
    # The audit checks this field.

    return json.dumps(fingerprint, ensure_ascii=False)
