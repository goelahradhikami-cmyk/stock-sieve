"""
Evolution Arena v3 - Multi-Climate Evolution (Commit 6-O.5).

The key upgrade from v2: doctrines evolve across FOUR market climates
simultaneously (bull/bear/crash/sideway), not just one. Fitness v5 rewards
Climate Robustness (good in ALL climates, not just one).

Goal: observe whether the system NATURALLY discovers All-Weather species
(positive residual alpha in all 4 climates) without human selection -
vs v2 where quant_nerd was hand-picked as champion.

Fitness v5:
  0.35 Alpha Quality + 0.25 Climate Robustness + 0.15 Selection Alpha
  + 0.10 Uniqueness + 0.10 Survival + 0.05 Capacity

  Climate Robustness = 0.6×mean_alpha + 0.4×min_alpha - variance_penalty
  (rewards consistency across climates, not just average)

Climate Diversity Guard: ensures each climate niche keeps a specialist.
  - All-Weather (positive in all 4)
  - Bull specialist (best in bull)
  - Bear specialist (best in bear)
  - Crash survivor (best in crash)
  All must survive each generation.

Usage:
    python scripts/run_evolution_v3.py --generations 50 --population 32
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
from src.evolution.competition import CompetitionEngine
from src.evolution.selection_guard import (
    EvolutionSelectionGuard, DoctrineFitnessInfo,
)
from src.market.regime_bootstrap import RegimeBootstrap
from src.utils.logger import get_logger

logger = get_logger(__name__)


# Four climate worlds with historical dates
CLIMATE_WORLDS = {
    "bull":    ["2024-10-25", "2024-11-08", "2024-11-22", "2024-12-06", "2024-12-20"],
    "bear":    ["2022-03-07", "2022-03-18", "2022-04-01", "2022-04-15", "2022-04-29"],
    "crash":   ["2022-03-14", "2022-03-15", "2022-04-25", "2022-04-26", "2022-05-05"],
    "sideway": ["2023-01-13", "2023-02-10", "2023-03-10", "2023-04-14", "2023-05-12"],
}

# All dates combined (for SurvivalArena, which loops over dates)
ALL_CLIMATE_DATES = []
for dates in CLIMATE_WORLDS.values():
    ALL_CLIMATE_DATES.extend(dates)


@dataclass
class ClimateProfile:
    """A doctrine's performance across 4 climates."""
    bull_alpha: float = 0.0
    bear_alpha: float = 0.0
    crash_alpha: float = 0.0
    sideway_alpha: float = 0.0

    @property
    def mean_alpha(self) -> float:
        return np.mean([self.bull_alpha, self.bear_alpha, self.crash_alpha, self.sideway_alpha])

    @property
    def min_alpha(self) -> float:
        return min(self.bull_alpha, self.bear_alpha, self.crash_alpha, self.sideway_alpha)

    @property
    def variance(self) -> float:
        return float(np.std([self.bull_alpha, self.bear_alpha, self.crash_alpha, self.sideway_alpha]))

    @property
    def is_all_weather(self) -> bool:
        return self.min_alpha > 0.0

    @property
    def climate_robustness(self) -> float:
        """0.6×mean + 0.4×min - variance_penalty, normalized to 0-1."""
        raw = 0.6 * self.mean_alpha + 0.4 * self.min_alpha - self.variance * 2.0
        # Sigmoid normalize
        return 1.0 / (1.0 + np.exp(-raw * 15))

    @property
    def best_climate(self) -> str:
        alphas = {"bull": self.bull_alpha, "bear": self.bear_alpha,
                  "crash": self.crash_alpha, "sideway": self.sideway_alpha}
        return max(alphas, key=alphas.get)


@dataclass
class GenerationHealthV3:
    """Health metrics for v3 (includes climate diversity)."""
    generation: int
    population_size: int
    family_count: int
    shannon_diversity: float
    fitness_mean: float
    fitness_spread: float
    all_weather_count: int       # doctrines positive in all 4 climates
    climate_specialists: dict    # {climate: count of specialists}
    avg_climate_robustness: float
    extinct_count: int
    born_count: int
    top_doctrine: str = ""
    top_all_weather: str = ""


class EvolutionArenaV3:
    """Multi-climate evolution engine.

    Key difference from v2: every generation backtests across ALL 4 climate
    worlds (20 dates total), and fitness rewards Climate Robustness (good in
    all climates) not just average alpha.
    """

    EXTINCTION_MIN_AGE = 5
    EXTINCTION_FITNESS_THRESHOLD = 0.30  # lower than v2 (climate is harder)
    EXTINCTION_CONSECUTIVE = 5

    BASE_MUTATION_RATE = 0.05
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
        self.comp = CompetitionEngine(eval_db=eval_db, cache_db=cache_db)
        self.guard = EvolutionSelectionGuard()
        self.registry = DoctrineRegistry(eval_db)
        self.regime = RegimeBootstrap(eval_db=eval_db, cache_db=cache_db)

        self.doctrine_ages: dict[str, int] = {}
        self.consecutive_bad: dict[str, int] = {}

    def run(self, generations: int = 50,
            target_population: int = 32) -> list[GenerationHealthV3]:
        """Run multi-climate evolution."""
        population = self._initialize_population(target_population)
        print(f"=== Evolution Arena v3 (Multi-Climate) ===")
        print(f"Generations: {generations}, Population: {target_population}")
        print(f"Climates: {list(CLIMATE_WORLDS.keys())}")
        print(f"Dates per gen: {len(ALL_CLIMATE_DATES)} (4 worlds × 5 dates)")
        print()

        trajectory: list[GenerationHealthV3] = []
        t_start = time.time()

        for gen in range(generations + 1):
            t_gen = time.time()

            # 1. Backtest across ALL climate dates
            self._clear_fitness_history()
            self.arena.run_cycle(ALL_CLIMATE_DATES, doctrines=population)

            # 2. Record ecology
            self._record_ecology(gen, population, ALL_CLIMATE_DATES[-1])

            # 3. Compute climate profiles + fitness v5
            climate_profiles = self._compute_climate_profiles(population)
            population_counts = Counter(d.doctrine_id for d in population)
            fitness_map = self._compute_fitness_v5(population, population_counts, climate_profiles)

            # 4. Extinction
            extinct_ids = self._check_extinction(gen, fitness_map, population)

            # 5. Climate Diversity Guard: protect climate specialists
            survivors_info = self._climate_aware_selection(fitness_map, climate_profiles, population_counts)
            survivor_ids = {s.doctrine_id for s in survivors_info}
            survivors = [d for d in population if d.doctrine_id in survivor_ids]

            # 6. Breeding
            breeding_plan = self.guard.plan_breeding(survivors_info, target_population)
            children = self._breed_adaptive(survivors, breeding_plan, gen)

            # 7. Update ages
            for d in population:
                self.doctrine_ages[d.doctrine_id] = self.doctrine_ages.get(d.doctrine_id, 0) + 1

            # 8. Health
            health = self._compute_health(gen, population, fitness_map, climate_profiles, children, extinct_ids)
            trajectory.append(health)
            self._print_dashboard(health, time.time() - t_gen)

            if gen == generations:
                break

            # 9. Next gen
            population = survivors + children
            while len(population) < target_population:
                if survivors:
                    parent = random.choice(survivors)
                    mutated = self.engine.mutate(parent, mutation_rate=0.15)
                    if mutated:
                        population.append(mutated)

        total = time.time() - t_start
        print(f"\n=== Evolution v3 Complete: {generations} gens in {total:.0f}s ===")
        return trajectory

    def _compute_climate_profiles(self, population: list[DoctrineGenome]
                                   ) -> dict[str, ClimateProfile]:
        """Compute each doctrine's residual alpha per climate."""
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT doctrine_id, trade_date, residual_alpha "
                "FROM doctrine_fitness_history"
            ).fetchall()
        finally:
            conn.close()

        # Group by doctrine + climate
        profiles: dict[str, ClimateProfile] = {}
        for row in rows:
            did = row["doctrine_id"]
            date = row["trade_date"]
            alpha = row["residual_alpha"]
            if alpha is None:
                continue

            if did not in profiles:
                profiles[did] = ClimateProfile()

            # Determine which climate this date belongs to
            for climate, dates in CLIMATE_WORLDS.items():
                if date in dates:
                    if climate == "bull":
                        profiles[did].bull_alpha = (profiles[did].bull_alpha + alpha) / 2 if profiles[did].bull_alpha != 0 else alpha
                    elif climate == "bear":
                        profiles[did].bear_alpha = (profiles[did].bear_alpha + alpha) / 2 if profiles[did].bear_alpha != 0 else alpha
                    elif climate == "crash":
                        profiles[did].crash_alpha = (profiles[did].crash_alpha + alpha) / 2 if profiles[did].crash_alpha != 0 else alpha
                    elif climate == "sideway":
                        profiles[did].sideway_alpha = (profiles[did].sideway_alpha + alpha) / 2 if profiles[did].sideway_alpha != 0 else alpha
                    break

        return profiles

    def _compute_fitness_v5(self, population: list[DoctrineGenome],
                             population_counts: dict[str, int],
                             climate_profiles: dict[str, ClimateProfile]
                             ) -> dict[str, DoctrineFitnessInfo]:
        """Fitness v5 with Climate Robustness."""
        fitness_map = {}
        for d in population:
            fr = self.fitness_calc.calculate_doctrine_fitness(d.doctrine_id, population_counts)
            profile = climate_profiles.get(d.doctrine_id, ClimateProfile())

            if fr:
                # Boost fitness with Climate Robustness
                climate_boost = profile.climate_robustness * 0.25  # 0.25 weight
                fitness_v5 = fr.fitness * 0.75 + climate_boost  # blend

                fitness_map[d.doctrine_id] = DoctrineFitnessInfo(
                    doctrine_id=d.doctrine_id,
                    fitness=fitness_v5,
                    alpha_quality=fr.residual_alpha_normalized,
                    regime_adaptation=profile.climate_robustness,
                    drawdown_score=fr.drawdown_score,
                    capacity_score=fr.capacity_score,
                    sample_count=fr.sample_count,
                )
        return fitness_map

    def _climate_aware_selection(self, fitness_map: dict,
                                  climate_profiles: dict[str, ClimateProfile],
                                  population_counts: dict[str, int]
                                  ) -> list[DoctrineFitnessInfo]:
        """Selection with Climate Diversity Guard.

        Protects:
          - Top All-Weather doctrine (positive in all 4 climates)
          - Top Bull specialist
          - Top Bear specialist
          - Top Crash specialist
          - Top Sideway specialist
        """
        # Start with standard guard selection
        survivors = self.guard.select_survivors(fitness_map, population_counts)
        survivor_ids = {s.doctrine_id for s in survivors}

        # Climate niche protection
        for climate in ["bull", "bear", "crash", "sideway"]:
            # Find best doctrine in this climate (among ALL doctrines, not just survivors)
            best_id = None
            best_alpha = -999
            for did, profile in climate_profiles.items():
                alpha = getattr(profile, f"{climate}_alpha")
                if alpha > best_alpha and did in fitness_map:
                    best_alpha = alpha
                    best_id = did
            if best_id and best_id not in survivor_ids:
                # Add this specialist
                survivors.append(fitness_map[best_id])
                survivor_ids.add(best_id)

        # Also protect top All-Weather
        all_weather = [(did, p) for did, p in climate_profiles.items() if p.is_all_weather and did in fitness_map]
        if all_weather:
            best_aw = max(all_weather, key=lambda x: x[1].min_alpha)
            if best_aw[0] not in survivor_ids:
                survivors.append(fitness_map[best_aw[0]])
                survivor_ids.add(best_aw[0])

        return survivors

    def _initialize_population(self, target: int) -> list[DoctrineGenome]:
        base_identities = self._base_identities()
        population = []
        for iv in base_identities:
            d = self.engine.classify(iv)
            self.registry.save(d)
            population.append(d)
        while len(population) < target:
            parent_a = random.choice(population)
            parent_b = random.choice([d for d in population if d.doctrine_id != parent_a.doctrine_id])
            child = self.engine.crossover(parent_a, parent_b)
            mutated = self.engine.mutate(child, mutation_rate=0.1)
            if mutated:
                self.registry.save(mutated)
                population.append(mutated)
            else:
                self.registry.save(child)
                population.append(child)
        return population

    def _clear_fitness_history(self):
        conn = sqlite3.connect(self.eval_db)
        conn.execute("DELETE FROM doctrine_fitness_history")
        conn.execute("DELETE FROM doctrine_regime_statistics")
        conn.commit()
        conn.close()

    def _record_ecology(self, gen, population, trade_date):
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT doctrine_id, factor_alpha, selection_alpha, origin_quality, market_regime "
                "FROM doctrine_fitness_history"
            ).fetchall()
        finally:
            conn.close()

        from src.factors.snapshot_builder import FactorSnapshotBuilder
        builder = FactorSnapshotBuilder()
        ecology_map = {}
        for d in population:
            stats = defaultdict(list)
            for row in rows:
                if row["doctrine_id"] == d.doctrine_id:
                    if row["selection_alpha"] is not None:
                        stats["sel"].append(row["selection_alpha"])
                    if row["factor_alpha"] is not None:
                        stats["fact"].append(row["factor_alpha"])
                    if row["origin_quality"] is not None:
                        stats["oq"].append(row["origin_quality"])

            avg_sel = float(np.mean(stats["sel"])) if stats["sel"] else 0.0
            avg_fact = float(np.mean(stats["fact"])) if stats["fact"] else 0.0
            avg_oq = float(np.mean(stats["oq"])) if stats["oq"] else 0.5

            picks = builder.score_universe(trade_date, d.factor_bias, top_n=20)
            crowd_info = self.ecology.compute_crowding(d, picks, population, trade_date)
            ecology_map[d.doctrine_id] = {
                "factor_alpha": avg_fact, "selection_alpha": avg_sel,
                "origin_quality": avg_oq, "crowding_score": crowd_info.crowding_score,
            }

        self.ecology.record_generation(gen, ecology_map)

    def _check_extinction(self, gen, fitness_map, population):
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
                family = d.doctrine_id.split("_")[0] if "_" in d.doctrine_id else d.doctrine_id
                self.comp.record_death(d.doctrine_id, gen, f"low_fitness_{self.consecutive_bad[d.doctrine_id]}_gens")
        return extinct_ids

    def _breed_adaptive(self, survivors, breeding_plans, gen):
        children = []
        doctrine_by_id = {d.doctrine_id: d for d in survivors}
        for plan in breeding_plans:
            parent = doctrine_by_id.get(plan.parent_doctrine_id)
            if not parent:
                continue
            mutation_rate = self.BASE_MUTATION_RATE
            conn = sqlite3.connect(self.eval_db)
            try:
                row = conn.execute(
                    "SELECT crowding_score FROM alpha_decay_history "
                    "WHERE doctrine_id=? ORDER BY generation DESC LIMIT 1",
                    (parent.doctrine_id,),
                ).fetchone()
                if row and row[0] and row[0] > self.CROWDING_THRESHOLD:
                    mutation_rate *= 1.5
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
                        family = mutated.doctrine_id.split("_")[0] if "_" in mutated.doctrine_id else mutated.doctrine_id
                        self.comp.record_birth(mutated.doctrine_id, family, gen, "crossover_mutation")
                        children.append(mutated)
                    else:
                        self.registry.save(child)
                        children.append(child)
                else:
                    mutated = self.engine.mutate(parent, mutation_rate=mutation_rate * 1.5)
                    if mutated:
                        self.registry.save(mutated)
                        children.append(mutated)
        return children

    def _compute_health(self, gen, population, fitness_map, climate_profiles, children, extinct_ids):
        family_counts = Counter(d.doctrine_id.split("_")[0] if "_" in d.doctrine_id else d.doctrine_id for d in population)
        total = sum(family_counts.values())
        shannon = sum(-count/total * math.log(count/total) for count in family_counts.values() if count > 0)

        fitnesses = [info.fitness for info in fitness_map.values()] if fitness_map else []

        # Climate stats
        all_weather_count = sum(1 for p in climate_profiles.values() if p.is_all_weather)
        specialists = {}
        for climate in ["bull", "bear", "crash", "sideway"]:
            specialists[climate] = sum(1 for p in climate_profiles.values()
                                       if p.best_climate == climate and p.is_all_weather is False)
        avg_robustness = float(np.mean([p.climate_robustness for p in climate_profiles.values()])) if climate_profiles else 0

        ranked = sorted(fitness_map.values(), key=lambda x: x.fitness, reverse=True) if fitness_map else []
        all_weather_doctrines = [(did, p) for did, p in climate_profiles.items() if p.is_all_weather]
        top_aw = max(all_weather_doctrines, key=lambda x: x[1].min_alpha)[0] if all_weather_doctrines else ""

        return GenerationHealthV3(
            generation=gen, population_size=len(population),
            family_count=len(family_counts), shannon_diversity=shannon,
            fitness_mean=np.mean(fitnesses) if fitnesses else 0,
            fitness_spread=(max(fitnesses)-min(fitnesses)) if fitnesses else 0,
            all_weather_count=all_weather_count,
            climate_specialists=specialists,
            avg_climate_robustness=avg_robustness,
            extinct_count=len(extinct_ids), born_count=len(children),
            top_doctrine=ranked[0].doctrine_id if ranked else "",
            top_all_weather=top_aw,
        )

    def _print_dashboard(self, h: GenerationHealthV3, elapsed: float):
        print(f"Gen {h.generation:3d} | Pop={h.population_size:2d} Fam={h.family_count} "
              f"H={h.shannon_diversity:.2f} | Fit:{h.fitness_mean:.3f}±{h.fitness_spread:.3f} | "
              f"AW={h.all_weather_count:2d} "
              f"Spec(B{h.climate_specialists.get('bull',0)}/"
              f"Br{h.climate_specialists.get('bear',0)}/"
              f"C{h.climate_specialists.get('crash',0)}/"
              f"S{h.climate_specialists.get('sideway',0)}) | "
              f"Rob={h.avg_climate_robustness:.2f} | "
              f"Ext={h.extinct_count} Born={h.born_count} | {elapsed:.1f}s")

    def _base_identities(self):
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
    parser = argparse.ArgumentParser(description="Evolution Arena v3 - Multi-Climate")
    parser.add_argument("--generations", type=int, default=50)
    parser.add_argument("--population", type=int, default=32)
    args = parser.parse_args()

    arena = EvolutionArenaV3()
    trajectory = arena.run(generations=args.generations, target_population=args.population)

    print(f"\n{'='*80}")
    print("MULTI-CLIMATE EVOLUTION ANALYSIS")
    print(f"{'='*80}")
    print(f"\n{'Gen':>4} {'Pop':>4} {'Fam':>4} {'Shannon':>8} {'FitMean':>8} {'AW':>4} {'Rob':>6} {'Ext':>4} {'Born':>4}")
    for h in trajectory:
        print(f"{h.generation:4d} {h.population_size:4d} {h.family_count:4d} "
              f"{h.shannon_diversity:8.3f} {h.fitness_mean:8.3f} {h.all_weather_count:4d} "
              f"{h.avg_climate_robustness:6.2f} {h.extinct_count:4d} {h.born_count:4d}")

    first, last = trajectory[0], trajectory[-1]
    print(f"\n=== Summary (Gen {first.generation} -> Gen {last.generation}) ===")
    print(f"Shannon: {first.shannon_diversity:.3f} -> {last.shannon_diversity:.3f}")
    print(f"Families: {first.family_count} -> {last.family_count}")
    print(f"All-Weather doctrines: {first.all_weather_count} -> {last.all_weather_count}")
    print(f"Climate Robustness: {first.avg_climate_robustness:.3f} -> {last.avg_climate_robustness:.3f}")
    print(f"Total extinctions: {sum(h.extinct_count for h in trajectory)}")
    print(f"Total births: {sum(h.born_count for h in trajectory)}")

    if last.all_weather_count > 0:
        print(f"\n✅ All-Weather species emerged naturally: {last.all_weather_count} doctrines")
    else:
        print(f"\n⚠️ No All-Weather species emerged")

    if last.shannon_diversity < 0.5:
        print("⚠️ ECOSYSTEM COLLAPSED: Shannon < 0.5")
    else:
        print("✅ Ecosystem stable: diversity maintained across climates")


if __name__ == "__main__":
    main()
