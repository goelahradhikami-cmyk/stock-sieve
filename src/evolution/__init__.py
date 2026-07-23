# Stock Sieve — 进化引擎
#
# 模块地图：
#   genome.py       — 基因组数据类（AgentGenome 等）
#   spec_engine.py  — 协议级季度进化机制（EvolutionEngine，实现 evolution_engine_spec_v1）
#   engine_v1.py    — 生产级每日进化引擎（EvolutionEngineV1，数据驱动选择 + 沙盒验证）
from .genome import AgentGenome, MutationCandidate, PerformanceRecord, SelectionResult
from .spec_engine import (
    CrossoverEngine,
    EvolutionEngine,
    MutationEngine,
    SandboxValidator,
    SelectionEngine,
    SurvivalCriteria,
)

__all__ = [
    "AgentGenome",
    "CrossoverEngine",
    "EvolutionEngine",
    "MutationCandidate",
    "MutationEngine",
    "PerformanceRecord",
    "SandboxValidator",
    "SelectionEngine",
    "SelectionResult",
    "SurvivalCriteria",
]
