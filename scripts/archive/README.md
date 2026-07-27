# scripts/archive — one-off research scripts (archived 2026-07-27)

These 25 scripts are **one-off milestone / experiment instruments** from the
v2 → v4.0 research phase. They are archived, NOT deleted, because the frozen
config (`config/investment_brain_v1_freeze.yaml`) and `PROJECT_HANDOVER.md`
reference several of them by their original `scripts/` path as milestone
reproduction artifacts.

**Path mapping:** every file here used to live at `scripts/<name>.py`.
When a frozen document says `scripts/run_guardian_autopsy.py`, read it as
`scripts/archive/run_guardian_autopsy.py`.

## What stayed in `scripts/` (operational)

- `backfill_*.py` (13) — data rebuilding tools (data layer is external state,
  these are the reconstruction path)
- `build_factor_snapshots.py`, `generate_v3_candidate_snapshot.py` — snapshot CLIs
- `shadow_recorder.py`, `shadow_outcome_evaluator.py` — daily shadow-trading ops
- `monthly_belief_audit.py` — monthly ops
- `run_bootstrap_validation.py` — **frozen gate reproduction** (referenced 5x by
  the frozen config for Sections 0-4 threshold values); keep runnable
- `run_evolution_observation.py` — Phase 4 evolution observation ops

## What is here (one-off research)

| Script | Milestone |
|---|---|
| evaluate_portfolio_construction.py | 6-S.12.4 |
| high_gap_autopsy.py | v3.3.1 autopsy |
| preview_child.py / probe_baostock_storage.py | dev probes |
| run_benchmark_robustness.py | benchmark robustness study |
| run_ege_validation.py | 6-S.15 EGE validation |
| run_evolution_v2.py / run_evolution_v3.py | superseded evolution engines |
| run_gate1_5_avoidance.py / run_gate1_exp0e.py | Gate 1 experiments |
| run_guardian_autopsy.py / _v2 | Guardian alpha decomposition (Exp0A-C) |
| run_rs_ablation_study.py | v3.2.2 RS ablation |
| run_security_analyst_v2/v3_replay.py | 6-S.12.5 / v3 replays |
| run_thesis_reality_check_v2.py | v2 reality check |
| run_v3_4_phase1_validation.py / _sustainability_ablation.py | 6-S.16 Phase 1/1.5 |
| run_v3_5_*/run_v3_ab_comparison.py | v3.5 experiment series (Exp1/8/9/10/11) |

Scripts are not imported as modules anywhere (verified 2026-07-27: no
cross-imports, no test/CI references), so this move changes no runnable
behavior. Re-running one of these requires the corresponding historical
databases; treat outputs as historical artifacts, not live dashboards.
