# Archived evolution research tools — 2026-07-23
#
# These modules are v2-v3 research instruments, NOT v4.0 operational components.
# They have zero production callers (verified 2026-07-23):
#
#   tournament.py     (EvolutionArena)    — multi-agent tournament + fitness ranking;
#                                           run_evolution_v3.py carries its own V3 variant
#   crowding_arena.py (CompetitiveArena)  — single-date crowding / overlap / alpha-decay
#
# They are archived, NOT deleted, because tests/test_evolution_arenas.py still
# characterizes their behavior (10 tests) — the safety net travels with the code.
#
# Reviving one of these requires: move back to src/evolution/, re-run its tests,
# and re-evaluate against the current evolution protocol before wiring it in.
"""
Archived evolution research tools (zero production callers since 2026-07-23).
"""
