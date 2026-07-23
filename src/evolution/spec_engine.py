"""
Spec-level Evolution Engine — quarterly selection/mutation/crossover machinery.

Implements evolution_engine_spec_v1.yaml:
  - SelectionEngine: elimination (grace period → watchlist → freeze) + reproduction
  - MutationEngine: 4 mutation sources (factor_memory / post_mortem / thesis / regime)
  - CrossoverEngine: factor_weight_interpolation with α∈[0.3, 0.7]
  - SandboxValidator: 3-month backtest, bootstrap significance test
  - SurvivalCriteria: diversity exemption, newborn protection, absolute minimum

Naming: this module is the SPEC-level orchestrator (EvolutionEngine). The
production daily-cycle engine lives in engine_v1.py (EvolutionEngineV1).
Genome data classes live in genome.py.

LLM is NOT part of this module — see evolution_engine_spec §8.
"""

import hashlib
import json
import random
from datetime import date, timedelta

import numpy as np
import yaml

# Genome data classes live in genome.py (single source of truth).
# Re-exported here so existing imports keep working unchanged.
from .genome import (
    AgentGenome,
    MutationCandidate,
    PerformanceRecord,
    SelectionResult,
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


# ═══════════════════════════════════════════════════════════
# Selection Engine
# ═══════════════════════════════════════════════════════════


class SelectionEngine:
    """Implements evolution_engine_spec §3 agent_selection."""

    ELITE_THRESHOLD = 0.25  # top 25%
    BOTTOM_PERCENTILE = 0.20  # bottom 20%
    MIN_LIFESPAN_MONTHS = 12
    MIN_DECISIONS = 100
    CONSECUTIVE_BOTTOM_QUARTERS = 2
    WATCHLIST_DURATION_MONTHS = 6
    GRACE_PERIOD_QUARTERS = 1

    def __init__(self, db):
        self.db = db  # EvaluationDB instance

    def run_selection_cycle(
        self, genomes: list[AgentGenome], performances: list[PerformanceRecord], watchlist: set[str]
    ) -> SelectionResult:
        """Run one quarterly selection cycle.

        Returns decisions for elimination, watchlist, and reproduction.
        """
        result = SelectionResult(
            eliminated=[], watchlist_additions=[], watchlist_recoveries=[], reproduction_pairs=[]
        )

        # Group performance by agent
        agent_scores = self._group_scores(performances)

        # ── 3.1 Elimination ─────────────────────────────
        for genome in genomes:
            aid = genome.agent_id
            scores = agent_scores.get(aid, [])

            # Check absolute minimum
            recent_score = scores[-1].personality_score if scores else 0
            if recent_score < 0.20:
                if aid in watchlist:
                    result.eliminated.append(aid)
                else:
                    result.watchlist_additions.append(aid)
                continue

            # Check relative competition (within genus)
            genus_agents = [g for g in genomes if g.strategy_genus == genome.strategy_genus]
            genus_scores = []
            for g in genus_agents:
                gs = agent_scores.get(g.agent_id, [])
                if gs:
                    genus_scores.append((g.agent_id, gs[-1].personality_score))

            genus_scores.sort(key=lambda x: x[1])
            bottom_n = max(1, int(len(genus_scores) * self.BOTTOM_PERCENTILE))
            bottom_ids = {x[0] for x in genus_scores[:bottom_n]}

            # Check consecutive quarters
            if aid in bottom_ids and len(scores) >= self.CONSECUTIVE_BOTTOM_QUARTERS:
                all_bottom = all(
                    self._is_bottom(s.personality_score, genus_scores)
                    for s in scores[-self.CONSECUTIVE_BOTTOM_QUARTERS :]
                )
                if all_bottom:
                    if aid in watchlist:
                        result.eliminated.append(aid)
                    else:
                        result.watchlist_additions.append(aid)

            # Watchlist recovery
            if aid in watchlist:
                median = np.median([s[1] for s in genus_scores])
                if recent_score >= median:
                    result.watchlist_recoveries.append(aid)

        # ── 3.2 Reproduction Selection ──────────────────
        genomes_by_id = {g.agent_id: g for g in genomes}
        for genus in set(g.strategy_genus for g in genomes):
            genus_agents = [g for g in genomes if g.strategy_genus == genus]
            genus_perf = []
            for g in genus_agents:
                gs = agent_scores.get(g.agent_id, [])
                if gs:
                    genus_perf.append((g.agent_id, gs[-1].personality_score))

            genus_perf.sort(key=lambda x: x[1], reverse=True)
            elite_n = max(1, int(len(genus_perf) * self.ELITE_THRESHOLD))
            elite = [
                genomes_by_id[aid]
                for aid, _ in genus_perf[:elite_n]
                if aid in genomes_by_id and aid not in watchlist
            ]

            # Diversity-weighted pairing
            pairs = self._diversity_weighted_pairing(elite)
            result.reproduction_pairs.extend(pairs)

        return result

    def _group_scores(self, performances: list[PerformanceRecord]) -> dict:
        groups = {}
        for p in performances:
            groups.setdefault(p.agent_id, []).append(p)
        return groups

    def _is_bottom(self, score: float, genus_scores: list) -> bool:
        """Check if score is in bottom 20% of genus."""
        all_scores = [s[1] for s in genus_scores]
        threshold = np.percentile(all_scores, self.BOTTOM_PERCENTILE * 100)
        return score <= threshold

    def _diversity_weighted_pairing(self, elite: list[AgentGenome]) -> list[tuple[str, str]]:
        """Pair elite agents with identity distance 0.3-0.7.

        Returns list of (parent_a_id, parent_b_id).
        """
        pairs = []
        used = set()

        for i, a in enumerate(elite):
            if a.agent_id in used:
                continue
            candidates = []
            for j, b in enumerate(elite):
                if i == j or b.agent_id in used:
                    continue
                dist = a.identity_distance(b)
                if 0.3 <= dist <= 0.7:
                    # Score: higher combined performance + optimal distance
                    score = dist  # closer to 0.5 = better
                    candidates.append((b, dist, score))

            if candidates:
                candidates.sort(key=lambda x: abs(x[1] - 0.5))
                best = candidates[0][0]
                pairs.append((a.agent_id, best.agent_id))
                used.add(a.agent_id)
                used.add(best.agent_id)

        return pairs


# ═══════════════════════════════════════════════════════════
# Mutation Engine
# ═══════════════════════════════════════════════════════════


class MutationEngine:
    """Implements evolution_engine_spec §4 mutation_rules.

    4 mutation sources:
      1. factor_memory_driven — IC-based weight adjustment
      2. post_mortem_driven — frequent suggested_actions
      3. thesis_outcome_driven — low win_rate patterns
      4. regime_adaptation — market regime mismatch
    """

    MAX_ADJUSTMENT = 0.05  # max factor weight change per generation
    MAX_SCORING_CHANGE = 0.10
    POST_MORTEM_THRESHOLD = 3  # suggested_action frequency for auto-inclusion
    THESIS_LOW_WIN_THRESHOLD = 0.4  # win_rate below this triggers mutation
    THESIS_QUARTERS_THRESHOLD = 8  # consecutive quarters
    REGIME_MISMATCH_MONTHS = 6
    COOLDOWN_QUARTERS = 2

    def __init__(self, db):
        self.db = db

    def generate_candidates(self, genome: AgentGenome) -> list[MutationCandidate]:
        """Generate mutation candidates from all 4 sources."""
        candidates = []

        # Source 1: Factor memory
        candidates.extend(self._factor_memory_mutations(genome))

        # Source 2: Post-mortem
        candidates.extend(self._post_mortem_mutations(genome))

        # Source 3: Thesis outcomes
        candidates.extend(self._thesis_outcome_mutations(genome))

        # Source 4: Regime adaptation
        candidates.extend(self._regime_adaptation_mutations(genome))

        return candidates

    def _factor_memory_mutations(self, genome: AgentGenome) -> list[MutationCandidate]:
        """If a factor's IC is significantly above 5yr mean, propose weight increase."""
        candidates = []
        six_months_ago = (date.today() - timedelta(days=180)).isoformat()

        for factor_name, weight in genome.factor_weights.items():
            memory = self.db.get_factor_memory(factor_name)
            if len(memory) < 4:
                continue

            # Compare recent 12-month IC to 5-year average
            recent = [m for m in memory if m["period_end"] >= six_months_ago]
            if not recent or len(recent) < 2:
                continue

            recent_ic = np.mean([m["ic_mean"] for m in recent if m["ic_mean"] is not None])
            all_ic = np.mean([m["ic_mean"] for m in memory if m["ic_mean"] is not None])

            if recent_ic > all_ic * 1.5 and weight < 0.30:  # Significant improvement
                candidates.append(
                    MutationCandidate(
                        proposal_id=f"factor_{factor_name}_{genome.agent_id}",
                        parent_agent_id=genome.agent_id,
                        hypothesis=f"Factor {factor_name} IC improved: {recent_ic:.3f} vs {all_ic:.3f} avg",
                        affected_parameter=f"factor_weight.{factor_name}",
                        direction="increase",
                        specific_value=min(
                            weight + random.uniform(0.01, self.MAX_ADJUSTMENT), 0.30
                        ),
                        expected_effect=f"Expected to improve alpha by ~{(recent_ic - all_ic):.3f}",
                        confidence=min(0.9, (recent_ic - all_ic) / all_ic) if all_ic > 0 else 0.5,
                        source="factor_memory",
                    )
                )

        return candidates

    def _post_mortem_mutations(self, genome: AgentGenome) -> list[MutationCandidate]:
        """Count suggested_actions from recent post-mortems. >=3 occurrences → candidate."""
        six_months_ago = (date.today() - timedelta(days=180)).isoformat()
        mortems = self.db.get_post_mortems_since(genome.agent_id, six_months_ago)

        # Count suggested action frequency
        action_counts = {}
        for pm in mortems:
            actions = pm.get("suggested_actions")
            if not actions:
                continue
            try:
                if isinstance(actions, str):
                    actions = json.loads(actions)
                for action in actions:
                    key = action.get("action", "") + ":" + action.get("target", "")
                    action_counts[key] = action_counts.get(key, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

        candidates = []
        for key, count in action_counts.items():
            if count >= self.POST_MORTEM_THRESHOLD:
                action_type, target = key.split(":", 1)
                candidates.append(
                    MutationCandidate(
                        proposal_id=f"pm_{target}_{genome.agent_id}",
                        parent_agent_id=genome.agent_id,
                        hypothesis=f"Post-mortem suggests {action_type} on {target} (appeared {count}x)",
                        affected_parameter=target,
                        direction=action_type,
                        specific_value=None,  # Rule engine computes exact value
                        expected_effect="Address recurring failure pattern from post-mortems",
                        confidence=min(0.8, count / 10.0),
                        source="post_mortem",
                    )
                )

        return candidates

    def _thesis_outcome_mutations(self, genome: AgentGenome) -> list[MutationCandidate]:
        """If a thesis pattern has sustained low win_rate, reduce its weight."""
        candidates = []
        # TODO: Query thesis_outcomes table for low-performing patterns
        return candidates

    def _regime_adaptation_mutations(self, genome: AgentGenome) -> list[MutationCandidate]:
        """If current regime mismatches genome's preference for 6+ months, suggest adaptation."""
        candidates = []
        # TODO: Check market_regime_snapshots vs genome's market_regime_preference
        return candidates

    def validate_mutation(
        self, candidate: MutationCandidate, parent_genome: AgentGenome, sandbox_result: dict
    ) -> bool:
        """Validate a single mutation candidate against sandbox results.

        Returns True if mutation should be applied.
        """
        # Check sandbox performance
        parent_score = sandbox_result.get("parent_score", 0)
        child_score = sandbox_result.get("child_score", 0)

        if parent_score <= 0:
            return False

        improvement = (child_score - parent_score) / parent_score
        return improvement >= 0.05  # 5% improvement required


# ═══════════════════════════════════════════════════════════
# Crossover Engine
# ═══════════════════════════════════════════════════════════


class CrossoverEngine:
    """Implements evolution_engine_spec §5 crossover_rules.

    factor_weight_interpolation: child = parent_a * α + parent_b * (1-α)
    α ∈ [0.3, 0.7] randomly sampled per crossover.
    """

    ALPHA_RANGE = (0.3, 0.7)

    def crossover(self, parent_a: AgentGenome, parent_b: AgentGenome) -> AgentGenome:
        """Produce a child genome from two parent genomes.

        Parent A is the primary inheritance line (doctrine, decision_graph).
        Factor weights are interpolated between parents.
        """
        alpha = random.uniform(*self.ALPHA_RANGE)

        # ── Interpolate factor weights ───────────────────
        child_weights = {}
        all_factor_names = set(parent_a.factor_weights.keys()) | set(parent_b.factor_weights.keys())
        for name in all_factor_names:
            wa = parent_a.factor_weights.get(name, 0.0)
            wb = parent_b.factor_weights.get(name, 0.0)
            child_weights[name] = round(wa * alpha + wb * (1 - alpha), 4)
            # Clamp to [0.0, 0.35]
            child_weights[name] = max(0.0, min(0.35, child_weights[name]))

        # ── Interpolate identity vector ──────────────────
        child_identity = {}
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
        for d in dims:
            a_val = parent_a.identity_vector.get(d, 50)
            b_val = parent_b.identity_vector.get(d, 50)
            child_identity[d] = round(a_val * alpha + b_val * (1 - alpha))

        # ── Inherit doctrine from parent_a (primary line) ──
        child_doctrine = parent_a.doctrine.copy()

        # ── Inherit decision_graph from parent_a ──
        child_decision = parent_a.decision_graph.copy()

        # ── Thesis scoring: average of parents ──
        child_thesis = {}
        if parent_a.thesis_scoring and parent_b.thesis_scoring:
            for key in set(parent_a.thesis_scoring.keys()) | set(parent_b.thesis_scoring.keys()):
                child_thesis[key] = (
                    parent_a.thesis_scoring.get(key, 0) + parent_b.thesis_scoring.get(key, 0)
                ) / 2

        # ── Build new genome YAML ─────────────────────────
        child_id = f"{parent_a.strategy_genus}_v{parent_a.generation + 1}_{hashlib.sha256(str(random.random()).encode()).hexdigest()[:6]}"

        new_yaml = self._build_genome_yaml(
            agent_id=child_id,
            strategy_genus=parent_a.strategy_genus,
            strategy_species=f"hybrid_{parent_a.strategy_species}_{parent_b.strategy_species}",
            generation=max(parent_a.generation, parent_b.generation) + 1,
            parent_agent_id=parent_a.agent_id,
            identity=child_identity,
            doctrine=child_doctrine,
            factor_weights=child_weights,
            thesis_scoring=child_thesis,
            decision_graph=child_decision,
        )

        child = AgentGenome(
            agent_id=child_id,
            strategy_genus=parent_a.strategy_genus,
            strategy_species="hybrid",
            generation=max(parent_a.generation, parent_b.generation) + 1,
            parent_agent_id=parent_a.agent_id,
            yaml_content=new_yaml,
            raw=yaml.safe_load(new_yaml) or {},
            identity_vector=child_identity,
            doctrine=child_doctrine,
            factor_weights=child_weights,
            thesis_scoring=child_thesis,
            decision_graph=child_decision,
        )

        return child

    def _build_genome_yaml(self, **kwargs) -> str:
        """Build a minimal genome YAML string for the child."""
        lines = [
            "# personality_genome_schema_v3.2.yaml",
            "# Auto-generated child genome",
            "schema: stock-sieve.io/personality-genome",
            "schema_version: 3.2.0",
            "",
            "identity:",
            f"  agent_id: {kwargs['agent_id']}",
            f"  strategy_genus: {kwargs['strategy_genus']}",
            f"  strategy_species: {kwargs['strategy_species']}",
            f"  generation: {kwargs['generation']}",
            f"  parent_agent_id: {kwargs['parent_agent_id']}",
            "",
            "investment_identity:",
            "  dimensions:",
        ]
        for dim, val in kwargs["identity"].items():
            lines.append(f"    {dim}: {val}")

        lines.append("")
        lines.append("factor_weights:")
        for name, weight in kwargs["factor_weights"].items():
            lines.append(f"  {name}: {weight}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# Sandbox Validator
# ═══════════════════════════════════════════════════════════


class SandboxValidator:
    """Implements evolution_engine_spec §4 validation.

    Tests mutation candidates with 3-month backtest.
    Acceptance: personality_score improvement ≥ 5% over parent.
    """

    SANDBOX_DURATION_DAYS = 90
    ACCEPTANCE_THRESHOLD = 0.05  # 5%
    MAX_ATTEMPTS = 3
    BOOTSTRAP_SAMPLES = 1000
    BOOTSTRAP_SIGNIFICANCE = 0.10  # p < 0.1

    def validate(
        self, parent_genome: AgentGenome, child_genome: AgentGenome, historical_data: dict
    ) -> dict:
        """Run sandbox validation for a child genome.

        Args:
            parent_genome: The parent (baseline) genome
            child_genome: The proposed child/mutant genome
            historical_data: dict with 'prices', 'financials', 'dates' for backtest

        Returns:
            dict with validation result, parent_score, child_score, etc.
        """
        # Simulate parent performance
        parent_score = self._simulate_performance(parent_genome, historical_data)

        # Simulate child performance
        child_score = self._simulate_performance(child_genome, historical_data)

        improvement = (child_score - parent_score) / parent_score if parent_score > 0 else 0

        # Bootstrap significance test
        p_value = self._bootstrap_test(parent_genome, child_genome, historical_data)

        passed = improvement >= self.ACCEPTANCE_THRESHOLD and p_value < self.BOOTSTRAP_SIGNIFICANCE

        return {
            "passed": passed,
            "parent_score": parent_score,
            "child_score": child_score,
            "improvement": improvement,
            "p_value": p_value,
            "parent_agent_id": parent_genome.agent_id,
            "child_agent_id": child_genome.agent_id,
        }

    def _simulate_performance(self, genome: AgentGenome, data: dict) -> float:
        """Simulate agent performance over sandbox period.

        Simplified: computes a weighted factor-based return.
        In production, this would use the full ResearchAgent + PortfolioAgent pipeline.
        """
        prices = data.get("prices")
        if prices is None or prices.empty:
            return 0.01  # default

        # Simplified simulation: random with genome influence
        np.random.seed(hash(genome.agent_id) % (2**31))
        base_return = np.random.normal(0.05, 0.15)

        # Factor weights influence expected return
        quality_weight = sum(
            w for n, w in genome.factor_weights.items() if n in ("roe", "roic", "gross_margin")
        )
        momentum_weight = sum(
            w for n, w in genome.factor_weights.items() if n.startswith("momentum")
        )

        adjusted_return = base_return + quality_weight * 0.02 - momentum_weight * 0.01
        return max(-0.4, min(0.5, adjusted_return))

    def _bootstrap_test(self, parent: AgentGenome, child: AgentGenome, data: dict) -> float:
        """Bootstrap test for significance of improvement."""
        np.random.seed(42)
        parent_scores = []
        child_scores = []

        for _ in range(min(self.BOOTSTRAP_SAMPLES, 100)):
            parent_scores.append(self._simulate_performance(parent, data))
            child_scores.append(self._simulate_performance(child, data))

        diffs = np.array(child_scores) - np.array(parent_scores)
        return float((diffs <= 0).mean())  # proportion of non-positive


# ═══════════════════════════════════════════════════════════
# Evolution Engine (orchestrator)
# ═══════════════════════════════════════════════════════════


class EvolutionEngine:
    """Orchestrates the full 7-step genome_update pipeline.

    Implements evolution_engine_spec §6 genome_update.
    """

    def __init__(self, db, factor_engine=None, data_provider=None):
        self.db = db
        self.factor_engine = factor_engine
        self.data_provider = data_provider
        self.selection = SelectionEngine(db)
        self.mutation = MutationEngine(db)
        self.crossover = CrossoverEngine()
        self.sandbox = SandboxValidator()

        self.watchlist: set[str] = set()

    def run_cycle(
        self,
        genomes: list[AgentGenome],
        performances: list[PerformanceRecord],
        historical_data: dict = None,
    ) -> dict:
        """Run one complete evolution cycle (quarterly).

        Returns dict with cycle summary.
        """
        result = {
            "new_children": [],
            "frozen": [],
            "watchlist_added": [],
            "watchlist_recovered": [],
            "rejected_mutations": [],
        }

        # ── Step 1-2: Selection ───────────────────────────
        selection_result = self.selection.run_selection_cycle(genomes, performances, self.watchlist)

        result["frozen"] = selection_result.eliminated
        result["watchlist_added"] = selection_result.watchlist_additions
        result["watchlist_recovered"] = selection_result.watchlist_recoveries

        # Update watchlist
        self.watchlist.update(selection_result.watchlist_additions)
        self.watchlist.difference_update(selection_result.watchlist_recoveries)

        # ── Step 3-7: Reproduction → Mutation → Sandbox ──
        genomes_by_id = {g.agent_id: g for g in genomes}

        for parent_a_id, parent_b_id in selection_result.reproduction_pairs:
            parent_a = genomes_by_id.get(parent_a_id)
            parent_b = genomes_by_id.get(parent_b_id)
            if not parent_a or not parent_b:
                continue

            # Crossover
            child = self.crossover.crossover(parent_a, parent_b)

            # Generate mutation candidates
            candidates = self.mutation.generate_candidates(parent_a)
            candidates += self.mutation.generate_candidates(parent_b)

            # Apply mutations (each validated in sandbox)
            for candidate in candidates[:3]:  # Limit to 3 mutations per child
                sandbox_result = self.sandbox.validate(parent_a, child, historical_data or {})

                if sandbox_result["passed"]:
                    # Adjust child genome based on candidate
                    if candidate.specific_value is not None:
                        param = candidate.affected_parameter.replace("factor_weight.", "")
                        child.factor_weights[param] = candidate.specific_value

                    # Record child birth
                    self.db.insert_genome_snapshot(
                        agent_id=child.agent_id,
                        strategy_genus=child.strategy_genus,
                        strategy_species=child.strategy_species,
                        generation=child.generation,
                        parent_agent_id=child.parent_agent_id,
                        genome_hash=child.genome_hash(),
                        genome_yaml=child.yaml_content,
                        birth_date=date.today().isoformat(),
                        mutation_reason=candidate.source,
                        mutation_detail=candidate.hypothesis,
                        status="active",
                    )

                    self.db.insert_decision_event(
                        agent_id=child.agent_id,
                        event_type="CHILD_AGENT_BORN",
                        event_summary=f"Crossover of {parent_a_id} × {parent_b_id}, mutation: {candidate.hypothesis}",
                        event_data={
                            "parent_a": parent_a_id,
                            "parent_b": parent_b_id,
                            "mutation": candidate.proposal_id,
                            "sandbox_improvement": sandbox_result["improvement"],
                        },
                    )

                    result["new_children"].append(child.agent_id)
                else:
                    result["rejected_mutations"].append(candidate.proposal_id)

        return result


# ═══════════════════════════════════════════════════════════
# Survival Criteria Checker
# ═══════════════════════════════════════════════════════════


class SurvivalCriteria:
    """Implements evolution_engine_spec §6 survival_criteria."""

    ABSOLUTE_MIN_SCORE = 0.20
    DIVERSITY_EXEMPTION_DISTANCE = 0.5
    NEWBORN_PROTECTION_QUARTERS = 3
    NEWBORN_RELAXED_SCORE = 0.15

    @staticmethod
    def check_absolute_minimum(score: float) -> bool:
        """Returns True if score is below absolute minimum."""
        return score < SurvivalCriteria.ABSOLUTE_MIN_SCORE

    @staticmethod
    def check_diversity_exemption(genome: AgentGenome, other_genomes: list[AgentGenome]) -> bool:
        """Returns True if this agent qualifies for diversity exemption.

        Condition: min identity distance to other agents in same genus > 0.5.
        """
        same_genus = [
            g
            for g in other_genomes
            if g.strategy_genus == genome.strategy_genus and g.agent_id != genome.agent_id
        ]
        if not same_genus:
            return False

        min_dist = min(genome.identity_distance(g) for g in same_genus)
        return min_dist > SurvivalCriteria.DIVERSITY_EXEMPTION_DISTANCE

    @staticmethod
    def check_newborn_protection(generation: int, score: float) -> bool:
        """Returns True if newborn should be protected from elimination.

        Newborn: generation 0-1, relaxed score threshold.
        """
        if generation <= 1:
            return score >= SurvivalCriteria.NEWBORN_RELAXED_SCORE
        return True  # No protection for older agents
