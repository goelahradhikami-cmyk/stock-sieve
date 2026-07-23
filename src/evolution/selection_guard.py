"""
Evolution Selection Guard - Commit 6-L.9 (lightweight).

Prevents winner-takes-all in evolution. Without this, a doctrine that wins
one cycle (e.g. momentum in a bull run) would be cloned until the population
collapses to a single strategy - then dies when the regime flips.

Three rules (per user decision, lightweight version - full ecology in v2):
  1. max_doctrine_share: 0.35 - no single doctrine type >35% of next gen
  2. minimum_family_count: 5 - keep at least 5 distinct doctrine families
  3. elite_protection: not just fitness-top, also protect:
     - top alpha_quality (skill)
     - top regime specialist (best in each regime)
     - top defensive (best drawdown control)
     - top capacity (most deployable)

This mirrors how real fund-of-funds work: you don't fire every manager
except the YTD leader. You keep specialists for different environments.

Usage:
    from src.evolution.selection_guard import EvolutionSelectionGuard
    guard = EvolutionSelectionGuard()
    survivors = guard.select_survivors(doctrine_fitness_map, population_counts)
    breeding_plan = guard.plan_breeding(survivors, target_population=20)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DoctrineFitnessInfo:
    """Compact fitness info for selection decisions."""

    doctrine_id: str
    fitness: float
    alpha_quality: float = 0.0
    regime_adaptation: float = 0.5
    drawdown_score: float = 0.5
    capacity_score: float = 0.0
    sample_count: int = 0

    @property
    def family(self) -> str:
        """Doctrine family = first word of id (e.g. 'deep' from 'deep_value_purist')."""
        return self.doctrine_id.split("_")[0] if "_" in self.doctrine_id else self.doctrine_id

    @property
    def has_data(self) -> bool:
        return self.sample_count > 0


@dataclass
class BreedingPlan:
    """How many children each surviving doctrine should produce."""

    parent_doctrine_id: str
    num_children: int
    reason: str  # "elite_fitness" / "regime_specialist" / "defensive" / "capacity"


class EvolutionSelectionGuard:
    """Lightweight selection guard with ecological niche protection.

    Full ecology (extinction, niche competition, predator/prey) is deferred
    to Evolution Engine v2. This version just enforces the three rules so
    a single cycle can't collapse the population.
    """

    MAX_DOCTRINE_SHARE = 0.35  # no single family >35% of next gen
    MINIMUM_FAMILY_COUNT = 5  # keep at least 5 distinct families
    ELITE_FRACTION = 0.30  # top 30% by fitness are elites
    SPECIALIST_SLOTS = 4  # 4 protected slots (regime/defensive/capacity)

    def select_survivors(
        self,
        fitness_map: dict[str, DoctrineFitnessInfo],
        current_population: dict[str, int] | None = None,
    ) -> list[DoctrineFitnessInfo]:
        """Select which doctrines survive to the next generation.

        Combines:
          - Top by overall fitness (elites)
          - Top by alpha_quality (skill specialist)
          - Best drawdown control (defensive specialist)
          - Best capacity (deployable specialist)

        Ensures MINIMUM_FAMILY_COUNT distinct families survive.

        Returns: list of DoctrineFitnessInfo that survive.
        """
        if not fitness_map:
            return []

        doctrines = list(fitness_map.values())
        # Only consider doctrines with data
        with_data = [d for d in doctrines if d.has_data]
        without_data = [d for d in doctrines if not d.has_data]

        if not with_data:
            # No fitness data yet - keep everyone (cold start)
            return doctrines

        survivors: list[DoctrineFitnessInfo] = []
        survivor_ids: set[str] = set()

        def add(d: DoctrineFitnessInfo):
            if d.doctrine_id not in survivor_ids:
                survivors.append(d)
                survivor_ids.add(d.doctrine_id)

        # 1. Elites: top by fitness
        elites = sorted(with_data, key=lambda d: d.fitness, reverse=True)
        elite_n = max(1, int(len(with_data) * self.ELITE_FRACTION))
        for d in elites[:elite_n]:
            add(d)

        # 2. Alpha quality specialist (top by alpha_quality, if not already in)
        quality_top = sorted(with_data, key=lambda d: d.alpha_quality, reverse=True)
        if quality_top:
            add(quality_top[0])

        # 3. Defensive specialist (top by drawdown_score)
        defensive_top = sorted(with_data, key=lambda d: d.drawdown_score, reverse=True)
        if defensive_top:
            add(defensive_top[0])

        # 4. Capacity specialist (top by capacity_score)
        capacity_top = sorted(with_data, key=lambda d: d.capacity_score, reverse=True)
        if capacity_top:
            add(capacity_top[0])

        # 5. Ensure minimum family diversity
        families_present = {d.family for d in survivors}
        if len(families_present) < self.MINIMUM_FAMILY_COUNT:
            # Add best doctrine from each missing family
            for d in sorted(with_data, key=lambda x: x.fitness, reverse=True):
                if d.family not in families_present:
                    add(d)
                    families_present.add(d.family)
                    if len(families_present) >= self.MINIMUM_FAMILY_COUNT:
                        break

        # 6. Cold-start doctrines (no data) survive but don't get elite status
        for d in without_data:
            add(d)

        logger.info(
            "selection_guard: %d/%d doctrines survive (families=%d)",
            len(survivors),
            len(doctrines),
            len({d.family for d in survivors}),
        )
        return survivors

    def plan_breeding(
        self, survivors: list[DoctrineFitnessInfo], target_population: int = 20
    ) -> list[BreedingPlan]:
        """Plan how many children each survivor produces.

        Enforces MAX_DOCTRINE_SHARE: no family produces more than 35% of
        target_population children. This is the core anti-domination rule.

        Returns: list of BreedingPlan (parent + num_children + reason).
        """
        if not survivors:
            return []

        max_per_family = max(1, int(target_population * self.MAX_DOCTRINE_SHARE))

        # Rank survivors by fitness for breeding allocation
        ranked = sorted([s for s in survivors if s.has_data], key=lambda d: d.fitness, reverse=True)

        # Allocate children: top survivors get more, but capped per family
        family_children: dict[str, int] = defaultdict(int)
        plans: list[BreedingPlan] = []
        remaining = target_population - len(survivors)  # children to produce

        if remaining <= 0:
            # Population already at/over target - no breeding needed
            return []

        # Distribute remaining slots proportionally to fitness, capped per family
        total_fitness = sum(d.fitness for d in ranked) or 1.0
        for d in ranked:
            if remaining <= 0:
                break
            # Proportional allocation
            share = d.fitness / total_fitness
            allocated = max(1, int(remaining * share))
            # Cap at family limit
            family_room = max_per_family - family_children[d.family]
            allocated = min(allocated, family_room, remaining)
            if allocated > 0:
                plans.append(
                    BreedingPlan(
                        parent_doctrine_id=d.doctrine_id,
                        num_children=allocated,
                        reason="elite_fitness" if d.fitness > 0.5 else "supplement",
                    )
                )
                family_children[d.family] += allocated
                remaining -= allocated

        # If slots remain (due to family caps), distribute to underrepresented families
        if remaining > 0:
            for d in ranked:
                if remaining <= 0:
                    break
                family_room = max_per_family - family_children[d.family]
                if family_room > 0:
                    extra = min(1, family_room, remaining)
                    # Add to existing plan or create new
                    found = False
                    for p in plans:
                        if p.parent_doctrine_id == d.doctrine_id:
                            p.num_children += extra
                            found = True
                            break
                    if not found:
                        plans.append(
                            BreedingPlan(
                                parent_doctrine_id=d.doctrine_id,
                                num_children=extra,
                                reason="diversity_fill",
                            )
                        )
                    family_children[d.family] += extra
                    remaining -= extra

        total_children = sum(p.num_children for p in plans)
        logger.info(
            "selection_guard: breeding plan %d children across %d families (max share enforced)",
            total_children,
            len(family_children),
        )
        return plans
