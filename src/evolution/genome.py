"""
Genome data classes — shared vocabulary of the evolution subsystem.

Extracted from the former engine.py (now spec_engine.py) so that the
spec-level engines, the production engine (engine_v1), and consumers such
as research_agent.py all import genome data structures from one place.

Contents:
  - AgentGenome: parsed genome of a Research Agent
  - PerformanceRecord: quarterly performance for survival decisions
  - MutationCandidate: proposed mutation for sandbox validation
  - SelectionResult: output of a selection cycle

LLM is NOT part of this module — see evolution_engine_spec §8.
"""

import hashlib
import math
from dataclasses import dataclass, field


@dataclass
class AgentGenome:
    """Parsed genome of a Research Agent."""

    agent_id: str
    strategy_genus: str
    strategy_species: str
    generation: int
    parent_agent_id: str | None
    yaml_content: str

    # Parsed from YAML
    raw: dict = field(default_factory=dict)

    # Investment identity (8-dim vector)
    identity_vector: dict = field(default_factory=dict)

    # Doctrine (immutable)
    doctrine: dict = field(default_factory=dict)

    # Factor weights (mutable)
    factor_weights: dict = field(default_factory=dict)

    # Thesis scoring weights (mutable)
    thesis_scoring: dict = field(default_factory=dict)

    # Decision graph (mutable)
    decision_graph: dict = field(default_factory=dict)

    def identity_distance(self, other: "AgentGenome") -> float:
        """Compute Euclidean distance between identity vectors."""
        dims = [
            "valuation",
            "quality",
            "growth",
            "momentum",
            "macro",
            "contrarian",
            "patience",
            "concentration",
        ]
        sq_sum = 0.0
        for d in dims:
            a = self.identity_vector.get(d, 50)
            b = other.identity_vector.get(d, 50)
            sq_sum += ((a - b) / 100.0) ** 2
        return math.sqrt(sq_sum)

    def genome_hash(self) -> str:
        return hashlib.sha256(self.yaml_content.encode()).hexdigest()[:16]


@dataclass
class PerformanceRecord:
    """Quarterly performance for survival decisions."""

    agent_id: str
    strategy_genus: str
    period_end: str
    personality_score: float
    total_return: float
    sharpe: float
    max_drawdown: float


@dataclass
class MutationCandidate:
    """Proposed mutation for sandbox validation."""

    proposal_id: str
    parent_agent_id: str
    hypothesis: str
    affected_parameter: str
    direction: str  # "increase" / "decrease" / "add" / "remove"
    specific_value: float | None  # Set by rule engine, NOT by LLM
    expected_effect: str
    confidence: float
    source: str  # factor_memory / post_mortem / thesis / regime


@dataclass
class SelectionResult:
    """Output of a selection cycle."""

    eliminated: list[str]  # agent_ids to freeze
    watchlist_additions: list[str]  # agent_ids to add to watchlist
    watchlist_recoveries: list[str]  # agent_ids recovering from watchlist
    reproduction_pairs: list[tuple[str, str]]  # (parent_a, parent_b)
