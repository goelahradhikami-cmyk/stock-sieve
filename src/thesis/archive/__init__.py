# Archived thesis research tools — 2026-07-27
#
# These modules are research instruments, NOT v4.0 operational components.
# They have zero production callers (verified 2026-07-27):
#
#   adaptive_router.py      (AdaptiveRouter)      — multi-doctrine softmax allocation;
#                                                   never wired into any pipeline
#   market_state_machine.py (MarketStateMachine)  — 4-state daily classifier;
#                                                   superseded by the frozen
#                                                   state_transition.py (Guardian core);
#                                                   state_transition.py references it
#                                                   only in a docstring
#
# They are archived, NOT deleted, because tests/test_adaptive_router.py and
# tests/test_market_state_machine.py still characterize their behavior
# (30 tests) — the safety net travels with the code.
#
# Reviving one of these requires: move back to src/thesis/, re-run its tests,
# and re-evaluate against the frozen v4.0 protocol before wiring it in.
"""Archived thesis research tools (zero production callers since 2026-07-27)."""
