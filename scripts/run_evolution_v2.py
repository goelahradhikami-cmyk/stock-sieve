"""
Evolution Engine v2 - Long Horizon Evolution Arena (50 generations).

Commit Evolution Engine v2 Phase 1: multi-generation evolution with extinction,
Shannon diversity tracking, and adaptive mutation pressure.

Goal: observe whether the investment ecosystem remains stable over 50
generations without human intervention. Not looking for max return - looking
for ecological health: diversity preservation, new species emergence,
natural extinction, alpha half-life emergence.

Success criteria:
  - Family count: 5-8 maintained (not collapsed to 1)
  - Shannon diversity: not persistently declining
  - Extinction: some occur (selection pressure is real)
  - New doctrine emergence: mutations produce novel factor_bias
  - No single doctrine >35% (Guard working)

Usage:
    python scripts/run_evolution_v2.py --generations 50 --population 32
"""

from __future__ import annotations

import os
import sys
import json
import math
import sqlite3
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.doctrine_engine import (
    DoctrineEngine, DoctrineGenome, DoctrineRegistry,
)
from src.evolution.survival_arena import DoctrineSurvivalArena
from src.evolution.fitness import FitnessCalculator
from src.evolution.alpha_quality import AlphaQualityCalculator
from src.evolution.alpha_ecology import AlphaEcology
from src.evolution.selection_guard import (
    EvolutionSelectionGuard, DoctrineFitnessInfo,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GenerationHealth:
    """Health metrics for one generation (Evolution Health Dashboard)."""
    generation: int
    population_size: int
    family_count: int
    shannon_diversity: float        # -Σ p_i ln(p_i) over families
    identity_entropy: float         # entropy over identity vectors
    fitness_mean: float
    fitness_max: float
    fitness_min: float
    fitness_spread: float           # max - min
    avg_alpha_quality: float
    avg_selection_alpha: float
    avg_crowding: float
    avg_decay: float
    extinct_count: int
    born_count: int
    mutation_count: int
    family_distribution: dict = field(default_factory=dict)
    top_doctrine: str = ""
    bottom_doctrine: str = ""


class EvolutionEngineV2:
    """Long-horizon evolution with extinction + Shannon diversity + adaptive mutation.

    Key differences from v1 (run_evolution_observation.py):
      - 50 generations × 32 doctrines (vs 3 × 8)
      - Extinction: doctrines with fitness < threshold for N consecutive gens -> EXTINCT
      - Shannon Diversity: tracked per generation, alerts if collapsing
      - Adaptive Mutation: crowding/decay pressure increases mutation_rate
      - Population management: births replace extinct doctrines
    """

    # Extinction config
    EXTINCTION_MIN_AGE = 5          # doctrine must exist >= 5 gens before eligible
    EXTINCTION_FITNESS_THRESHOLD = 0.35  # below this for EXTINCTION_CONSECUTIVE gens
    EXTINCTION_CONSECUTIVE = 5      # consecutive bad gens -> extinct

    # Adaptive mutation
    BASE_MUTATION_RATE = 0.05
    CROWDING_MUTATION_BOOST = 1.5   # crowding > 0.7 -> rate *= 1.5
    DECAY_MUTATION_BOOST = 2.0      # decay < -0.05 -> rate *= 2.0
    CROWDING_THRESHOLD = 0.7
    DECAY_THRESHOLD = -0.05

    def __init__(self, eval_db: str = "data/evaluation.db",
                 cache_db: str = "data/cache.db"):
        self.eval_db = eval_db
        self.cache_db = cache_db
        self.engine = DoctrineEngine()
        self.arena = DoctrineSurvivalArena(eval_db=eval_db)
        self.fitness_calc = FitnessCalculator(eval_db=eval_db)
        self.aq_calc = AlphaQualityCalculator(eval_db=eval_db)
        self.ecology = AlphaEcology(eval_db=eval_db, cache_db=cache_db)
        self.guard = EvolutionSelectionGuard()
        self.registry = DoctrineRegistry(eval_db)

        # Track doctrine ages and consecutive bad gens for extinction
        self.doctrine_ages: dict[str, int] = {}
        self.consecutive_bad: dict[str, int] = {}

    def run(self, generations: int = 50,
            target_population: int = 32,
            backtest_dates: list[str] | None = None) -> list[GenerationHealth]:
        """Run long-horizon evolution.

        Args:
            generations: number of generations (50 recommended)
            target_population: population size (32 recommended)
            backtest_dates: dates for SurvivalArena per generation

        Returns: list of GenerationHealth (the evolution trajectory).
        """
        if backtest_dates is None:
            backtest_dates = ['2026-04-15', '2026-04-29', '2026-05-13',
                              '2026-05-27', '2026-06-10']

        # Initialize population: 8 base doctrines + 24 initial children
        population = self._initialize_population(target_population)
        print(f"=== Evolution Engine v2 ===")
        print(f"Generations: {generations}, Population: {target_population}")
        print(f"Initial population: {len(population)} doctrines")
        print()

        trajectory: list[GenerationHealth] = []
        t_start = time.time()

        for gen in range(generations + 1):
            t_gen = time.time()

            # 1. Backtest all doctrines (SurvivalArena)
            self._clear_fitness_history()
            self.arena.run_cycle(backtest_dates, doctrines=population)

            # 2. Record ecology (decay + crowding)
            self._record_ecology(gen, population, backtest_dates[-1])

            # 3. Compute fitness for each doctrine
            population_counts = Counter(d.doctrine_id for d in population)
            fitness_map = self._compute_fitness(population, population_counts)

            # 4. Extinction check
            extinct_ids = self._check_extinction(gen, fitness_map, population)

            # 5. Selection Guard: survivors + breeding plan
            survivors_info = self.guard.select_survivors(fitness_map, dict(population_counts))
            survivor_ids = {s.doctrine_id for s in survivors_info}
            survivors = [d for d in population if d.doctrine_id in survivor_ids]

            # 6. Breeding (with adaptive mutation)
            n_children = target_population - len(survivors)
            breeding_plan = self.guard.plan_breeding(survivors_info, target_population)
            children = self._breed_adaptive(survivors, breeding_plan, gen)

            # 7. Update ages
            for d in population:
                self.doctrine_ages[d.doctrine_id] = self.doctrine_ages.get(d.doctrine_id, 0) + 1

            # 8. Compute health metrics
            health = self._compute_health(gen, population, fitness_map, children, extinct_ids)
            trajectory.append(health)

            # 9. Print health dashboard
            elapsed_gen = time.time() - t_gen
            self._print_dashboard(health, elapsed_gen)

            if gen == generations:
                break

            # 10. Next generation = survivors + children (replace extinct)
            population = survivors + children
            # If still under target, breed more
            while len(population) < target_population:
                if survivors:
                    parent = random.choice(survivors)
                    mutated = self.engine.mutate(parent, mutation_rate=0.15)
                    if mutated:
                        population.append(mutated)

        total_time = time.time() - t_start
        print(f"\n=== Evolution Complete: {generations} generations in {total_time:.0f}s ===")
        return trajectory

    def _initialize_population(self, target: int) -> list[DoctrineGenome]:
        """Start with 8 base doctrines + initial children to reach target."""
        base_identities = self._base_identities()
        population = []
        for iv in base_identities:
            d = self.engine.classify(iv)
            self.registry.save(d)
            population.append(d)

        # Breed initial children to reach target
        while len(population) < target:
            parent_a = random.choice(population)
            parent_b = random.choice([d for d in population if d.doctrine_id != parent_a.doctrine_id])
            child = self.engine.crossover(parent_a, parent_b)
            mutated = self.engine.mutate(child, mutation_rate=0.1)
            if mutated:
                self.registry.save(mutated)
                population.append(mutated)
            else:
                population.append(child)
                self.registry.save(child)

        return population

    def _clear_fitness_history(self):
        conn = sqlite3.connect(self.eval_db)
        conn.execute("DELETE FROM doctrine_fitness_history")
        conn.execute("DELETE FROM doctrine_regime_statistics")
        conn.commit()
        conn.close()

    def _record_ecology(self, gen: int, population: list[DoctrineGenome],
                         trade_date: str):
        """Record alpha ecology for this generation."""
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT doctrine_id, factor_alpha, selection_alpha, origin_quality, market_regime "
                "FROM doctrine_fitness_history"
            ).fetchall()
        finally:
            conn.close()

        doctrine_stats: dict[str, dict] = {}
        for row in rows:
            did = row["doctrine_id"]
            if did not in doctrine_stats:
                doctrine_stats[did] = {"factor_alphas": [], "selection_alphas": [],
                                        "origin_qualities": [], "regimes": []}
            if row["factor_alpha"] is not None:
                doctrine_stats[did]["factor_alphas"].append(row["factor_alpha"])
            if row["selection_alpha"] is not None:
                doctrine_stats[did]["selection_alphas"].append(row["selection_alpha"])
            if row["origin_quality"] is not None:
                doctrine_stats[did]["origin_qualities"].append(row["origin_quality"])
            if row["market_regime"]:
                doctrine_stats[did]["regimes"].append(row["market_regime"])

        from src.factors.snapshot_builder import FactorSnapshotBuilder
        builder = FactorSnapshotBuilder()
        ecology_map = {}
        for d in population:
            stats = doctrine_stats.get(d.doctrine_id, {})
            avg_sel = float(np.mean(stats["selection_alphas"])) if stats.get("selection_alphas") else 0.0
            avg_fact = float(np.mean(stats["factor_alphas"])) if stats.get("factor_alphas") else 0.0
            avg_oq = float(np.mean(stats["origin_qualities"])) if stats.get("origin_qualities") else 0.5

            picks = builder.score_universe(trade_date, d.factor_bias, top_n=20)
            crowd_info = self.ecology.compute_crowding(d, picks, population, trade_date)

            ecology_map[d.doctrine_id] = {
                "factor_alpha": avg_fact, "selection_alpha": avg_sel,
                "origin_quality": avg_oq, "crowding_score": crowd_info.crowding_score,
            }

            family = d.doctrine_id.split("_")[0] if "_" in d.doctrine_id else d.doctrine_id
            for regime in stats.get("regimes", ["unknown"]):
                self.ecology.update_memory(family, regime, avg_sel)

        self.ecology.record_generation(gen, ecology_map)

    def _compute_fitness(self, population: list[DoctrineGenome],
                          population_counts: dict[str, int]) -> dict[str, DoctrineFitnessInfo]:
        """Compute fitness for all doctrines."""
        fitness_map = {}
        for d in population:
            fr = self.fitness_calc.calculate_doctrine_fitness(d.doctrine_id, population_counts)
            aq = self.aq_calc.calculate_doctrine_quality(d.doctrine_id)
            if fr:
                fitness_map[d.doctrine_id] = DoctrineFitnessInfo(
                    doctrine_id=d.doctrine_id,
                    fitness=fr.fitness,
                    alpha_quality=aq["avg_quality"] if aq else 0.0,
                    regime_adaptation=fr.regime_adaptation,
                    drawdown_score=fr.drawdown_score,
                    capacity_score=fr.capacity_score,
                    sample_count=fr.sample_count,
                )
        return fitness_map

    def _check_extinction(self, gen: int, fitness_map: dict,
                           population: list[DoctrineGenome]) -> list[str]:
        """Check for doctrine extinction + record death memory (6-N.2b).

        A doctrine goes EXTINCT if:
          - age >= EXTINCTION_MIN_AGE
          - fitness < EXTINCTION_FITNESS_THRESHOLD for EXTINCTION_CONSECUTIVE consecutive gens
        """
        from src.evolution.competition import CompetitionEngine
        comp = CompetitionEngine(eval_db=self.eval_db, cache_db=self.cache_db)

        extinct_ids = []
        for d in population:
            info = fitness_map.get(d.doctrine_id)
            age = self.doctrine_ages.get(d.doctrine_id, 0)

            if info and info.fitness < self.EXTINCTION_FITNESS_THRESHOLD:
                self.consecutive_bad[d.doctrine_id] = self.consecutive_bad.get(d.doctrine_id, 0) + 1
            else:
                self.consecutive_bad[d.doctrine_id] = 0

            if (age >= self.EXTINCTION_MIN_AGE and
                self.consecutive_bad.get(d.doctrine_id, 0) >= self.EXTINCTION_CONSECUTIVE):
                extinct_ids.append(d.doctrine_id)
                self.registry.extinct(d.doctrine_id)

                # 6-N.2b: Record death memory
                death_reason = f"low_fitness_{self.consecutive_bad[d.doctrine_id]}_gens"
                aq_info = self.aq_calc.calculate_doctrine_quality(d.doctrine_id)
                alpha_peak = aq_info.get("avg_quality") if aq_info else 0.0
                alpha_final = info.alpha_quality if info else 0.0
                half_life = comp.estimate_half_life(d.doctrine_id)

                # Get latest crowding
                conn = sqlite3.connect(self.eval_db)
                try:
                    crowding_row = conn.execute(
                        "SELECT crowding_score FROM alpha_decay_history "
                        "WHERE doctrine_id=? ORDER BY generation DESC LIMIT 1",
                        (d.doctrine_id,),
                    ).fetchone()
                    avg_crowding = crowding_row[0] if crowding_row else 0.0
                finally:
                    conn.close()

                comp.record_death(
                    doctrine_id=d.doctrine_id,
                    death_generation=gen,
                    death_reason=death_reason,
                    alpha_peak=alpha_peak,
                    alpha_final=alpha_final,
                    alpha_half_life=half_life,
                    avg_crowding=avg_crowding,
                )

                logger.info("EXTINCTION: %s (age=%d, consecutive_bad=%d, half_life=%d)",
                           d.doctrine_id, age, self.consecutive_bad[d.doctrine_id], half_life)

        return extinct_ids

    def _breed_adaptive(self, survivors: list[DoctrineGenome],
                         breeding_plans: list, gen: int) -> list[DoctrineGenome]:
        """Breed children with adaptive mutation rate.

        Mutation rate increases when:
          - crowding > 0.7 (1.5x)
          - decay < -0.05 (2.0x)
        """
        children = []
        doctrine_by_id = {d.doctrine_id: d for d in survivors}

        for plan in breeding_plans:
            parent = doctrine_by_id.get(plan.parent_doctrine_id)
            if not parent:
                continue

            # Adaptive mutation rate
            mutation_rate = self.BASE_MUTATION_RATE
            decay_info = self.ecology.get_decay_info(parent.doctrine_id)
            if decay_info:
                if decay_info.decay_rate < self.DECAY_THRESHOLD:
                    mutation_rate *= self.DECAY_MUTATION_BOOST
            # Check crowding from latest ecology record
            conn = sqlite3.connect(self.eval_db)
            try:
                row = conn.execute(
                    "SELECT crowding_score FROM alpha_decay_history "
                    "WHERE doctrine_id=? ORDER BY generation DESC LIMIT 1",
                    (parent.doctrine_id,),
                ).fetchone()
                if row and row[0] and row[0] > self.CROWDING_THRESHOLD:
                    mutation_rate *= self.CROWDING_MUTATION_BOOST
            finally:
                conn.close()

            for i in range(plan.num_children):
                partners = [d for d in survivors if d.doctrine_id != parent.doctrine_id]
                if partners:
                    partner = random.choice(partners)
                    child = self.engine.crossover(parent, partner)
                    mutated = self.engine.mutate(child, mutation_rate=mutation_rate)
                    if mutated:
                        self.registry.save(mutated)
                        self._record_birth(mutated, gen, "crossover_mutation")
                        children.append(mutated)
                    else:
                        self.registry.save(child)
                        self._record_birth(child, gen, "crossover")
                        children.append(child)
                else:
                    mutated = self.engine.mutate(parent, mutation_rate=mutation_rate * 1.5)
                    if mutated:
                        self.registry.save(mutated)
                        self._record_birth(mutated, gen, "mutation_only")
                        children.append(mutated)

        return children

    def _record_birth(self, doctrine: DoctrineGenome, gen: int, reason: str):
        """Record a doctrine's birth to survival memory (6-N.2b)."""
        from src.evolution.competition import CompetitionEngine
        comp = CompetitionEngine(eval_db=self.eval_db, cache_db=self.cache_db)
        family = doctrine.doctrine_id.split("_")[0] if "_" in doctrine.doctrine_id else doctrine.doctrine_id
        comp.record_birth(doctrine.doctrine_id, family, gen, reason)

    def _compute_health(self, gen: int, population: list[DoctrineGenome],
                         fitness_map: dict, children: list, extinct_ids: list) -> GenerationHealth:
        """Compute Shannon diversity + health metrics."""
        # Family distribution
        family_counts = Counter(d.doctrine_id.split("_")[0] if "_" in d.doctrine_id else d.doctrine_id for d in population)
        total = sum(family_counts.values())

        # Shannon diversity
        shannon = 0.0
        for count in family_counts.values():
            p = count / total
            if p > 0:
                shannon -= p * math.log(p)

        # Fitness stats
        fitnesses = [info.fitness for info in fitness_map.values()] if fitness_map else []
        avg_quality = np.mean([info.alpha_quality for info in fitness_map.values()]) if fitness_map else 0
        avg_sel = np.mean([info.alpha_quality for info in fitness_map.values()]) if fitness_map else 0

        # Ecology stats
        avg_crowding = 0.0
        avg_decay = 0.0
        conn = sqlite3.connect(self.eval_db)
        try:
            row = conn.execute(
                "SELECT AVG(crowding_score), AVG(decay_rate) FROM alpha_decay_history WHERE generation=?",
                (gen,),
            ).fetchone()
            if row:
                avg_crowding = row[0] or 0.0
                avg_decay = row[1] or 0.0
        finally:
            conn.close()

        # Top/bottom
        ranked = sorted(fitness_map.values(), key=lambda x: x.fitness, reverse=True) if fitness_map else []

        return GenerationHealth(
            generation=gen,
            population_size=len(population),
            family_count=len(family_counts),
            shannon_diversity=shannon,
            identity_entropy=shannon,  # simplified: same as family Shannon
            fitness_mean=np.mean(fitnesses) if fitnesses else 0,
            fitness_max=np.max(fitnesses) if fitnesses else 0,
            fitness_min=np.min(fitnesses) if fitnesses else 0,
            fitness_spread=(max(fitnesses) - min(fitnesses)) if fitnesses else 0,
            avg_alpha_quality=float(avg_quality),
            avg_selection_alpha=float(avg_sel),
            avg_crowding=float(avg_crowding),
            avg_decay=float(avg_decay),
            extinct_count=len(extinct_ids),
            born_count=len(children),
            mutation_count=len(children),
            family_distribution=dict(family_counts),
            top_doctrine=ranked[0].doctrine_id if ranked else "",
            bottom_doctrine=ranked[-1].doctrine_id if ranked else "",
        )

    def _print_dashboard(self, health: GenerationHealth, elapsed: float):
        """Print one-line health dashboard per generation."""
        print(f"Gen {health.generation:3d} | "
              f"Pop={health.population_size:2d} Fam={health.family_count} "
              f"H={health.shannon_diversity:.2f} | "
              f"Fit: {health.fitness_mean:.3f}±{health.fitness_spread:.3f} "
              f"[{health.fitness_min:.3f}~{health.fitness_max:.3f}] | "
              f"Crowd={health.avg_crowding:.2f} Decay={health.avg_decay:+.2%} | "
              f"Extinct={health.extinct_count} Born={health.born_count} | "
              f"{elapsed:.1f}s")

    def _base_identities(self) -> list[dict]:
        return [
            {"valuation": 90, "quality": 85, "growth": 40, "momentum": 15, "macro": 30, "contrarian": 80, "patience": 95, "concentration": 70},
            {"valuation": 50, "quality": 70, "growth": 90, "momentum": 40, "macro": 45, "contrarian": 20, "patience": 50, "concentration": 60},
            {"valuation": 10, "quality": 40, "growth": 40, "momentum": 95, "macro": 50, "contrarian": 5, "patience": 20, "concentration": 55},
            {"valuation": 85, "quality": 50, "growth": 15, "momentum": 5, "macro": 60, "contrarian": 95, "patience": 85, "concentration": 65},
            {"valuation": 75, "quality": 70, "growth": 25, "momentum": 10, "macro": 35, "contrarian": 55, "patience": 85, "concentration": 50},
            {"valuation": 50, "quality": 95, "growth": 55, "momentum": 15, "macro": 25, "contrarian": 35, "patience": 90, "concentration": 80},
            {"valuation": 40, "quality": 50, "growth": 50, "momentum": 55, "macro": 65, "contrarian": 30, "patience": 30, "concentration": 40},
            {"valuation": 55, "quality": 55, "growth": 50, "momentum": 35, "macro": 40, "contrarian": 45, "patience": 60, "concentration": 65},
        ]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Evolution Engine v2")
    parser.add_argument("--generations", type=int, default=50)
    parser.add_argument("--population", type=int, default=32)
    args = parser.parse_args()

    engine = EvolutionEngineV2()
    trajectory = engine.run(generations=args.generations, target_population=args.population)

    # Final analysis
    print(f"\n{'='*70}")
    print("EVOLUTION HEALTH ANALYSIS")
    print(f"{'='*70}")
    print(f"\n{'Gen':>4} {'Pop':>4} {'Fam':>4} {'Shannon':>8} {'FitMean':>8} {'Spread':>8} {'Crowd':>6} {'Decay':>8} {'Ext':>4} {'Born':>4}")
    for h in trajectory:
        print(f"{h.generation:4d} {h.population_size:4d} {h.family_count:4d} "
              f"{h.shannon_diversity:8.3f} {h.fitness_mean:8.3f} {h.fitness_spread:8.3f} "
              f"{h.avg_crowding:6.2f} {h.avg_decay:+8.2%} {h.extinct_count:4d} {h.born_count:4d}")

    # Summary
    first = trajectory[0]
    last = trajectory[-1]
    print(f"\n=== Summary (Gen {first.generation} -> Gen {last.generation}) ===")
    print(f"Shannon diversity: {first.shannon_diversity:.3f} -> {last.shannon_diversity:.3f} "
          f"({'stable' if abs(last.shannon_diversity - first.shannon_diversity) < 0.3 else 'CHANGED'})")
    print(f"Family count: {first.family_count} -> {last.family_count}")
    print(f"Fitness mean: {first.fitness_mean:.3f} -> {last.fitness_mean:.3f}")
    print(f"Total extinctions: {sum(h.extinct_count for h in trajectory)}")
    print(f"Total births: {sum(h.born_count for h in trajectory)}")

    # Health verdict
    if last.shannon_diversity < 0.5:
        print("⚠️ ECOSYSTEM COLLAPSED: Shannon diversity < 0.5")
    elif last.family_count < 3:
        print("⚠️ ECOSYSTEM COLLAPSED: < 3 families remaining")
    else:
        print("✅ Ecosystem stable: diversity maintained")


if __name__ == "__main__":
    main()
