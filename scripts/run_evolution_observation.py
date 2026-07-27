"""
Evolution Observation Arena - Commit 6-L.9.1 + multi-generation experiment.

Runs 3-5 generations of doctrine evolution with the full 6-L.7~6-L.9 pipeline:
  - SurvivalArena backtest (real returns + attribution)
  - Alpha Quality (residual × stability × breadth × capacity)
  - Fitness (0.5 quality + 0.2 regime + 0.15 drawdown + 0.1 capacity + 0.05 diversity)
  - Selection Guard (max_share 35% + min_family 5 + elite protection)
  - DoctrineEngine crossover + mutate (breed next generation)
  - Survival Logging (doctrine_survival_history)

This is an OBSERVATION experiment, not production evolution. The goal is to
see what emerges when a credible selection pressure runs for a few generations
without human intervention.

Usage:
    python scripts/run_evolution_observation.py --generations 5
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

import numpy as np

from src.agents.doctrine_engine import (
    DoctrineEngine,
    DoctrineGenome,
    DoctrineRegistry,
)
from src.evolution.alpha_quality import AlphaQualityCalculator
from src.evolution.fitness import FitnessCalculator
from src.evolution.selection_guard import (
    BreedingPlan,
    DoctrineFitnessInfo,
    EvolutionSelectionGuard,
)
from src.evolution.survival_arena import DoctrineSurvivalArena
from src.utils.logger import get_logger

logger = get_logger(__name__)


class EvolutionObservationArena:
    """Multi-generation evolution experiment with full logging.

    Each generation:
      1. Run SurvivalArena on historical dates (all doctrines)
      2. Compute Alpha Quality + Fitness for each doctrine
      3. Selection Guard picks survivors + breeding plan
      4. DoctrineEngine breeds children (crossover + mutate)
      5. Log everything to doctrine_survival_history
      6. Next generation = survivors + children
    """

    def __init__(self, eval_db: str = "data/evaluation.db"):
        self.eval_db = eval_db
        self.engine = DoctrineEngine()
        self.arena = DoctrineSurvivalArena(eval_db=eval_db)
        self.fitness_calc = FitnessCalculator(eval_db=eval_db)
        self.aq_calc = AlphaQualityCalculator(eval_db=eval_db)
        self.guard = EvolutionSelectionGuard()

    def run(self, generations: int = 5, backtest_dates: list[str] | None = None) -> dict:
        """Run multi-generation evolution observation.

        Args:
            generations: number of generations to run (3-5 recommended)
            backtest_dates: historical dates for SurvivalArena per generation.
                If None, uses a default set.

        Returns: summary of evolution trajectory.
        """
        if backtest_dates is None:
            backtest_dates = ["2026-04-15", "2026-04-29", "2026-05-13", "2026-05-27", "2026-06-10"]

        # Generation 0: the 8 base doctrines (from archetypes)
        reg = DoctrineRegistry(self.eval_db)
        reg.seed_archetypes()  # ensure 12 archetypes exist
        active = reg.get_active()
        # Start with the 8 that match base personalities
        base_ids = [
            "deep_value_purist",
            "aggressive_growth_hunter",
            "momentum_trend_follower",
            "deep_contrarian",
            "dividend_income_compounder",
            "quality_moat_compounder",
            "quantitative_factor_rotator",
            "smart_money_tracker",
        ]
        population = [d for d in active if d.doctrine_id in base_ids]
        if len(population) < 8:
            # Some archetypes missing - classify from identities
            for iv in self._base_identities():
                d = self.engine.classify(iv)
                if d.doctrine_id in base_ids and d.doctrine_id not in [
                    p.doctrine_id for p in population
                ]:
                    reg.save(d)
                    population.append(d)

        print("=== Evolution Observation Arena ===")
        print(f"Generations: {generations}")
        print(f"Backtest dates per gen: {backtest_dates}")
        print(f"Gen 0 population: {len(population)} doctrines")
        for d in population:
            print(f"  {d.doctrine_id}")
        print()

        trajectory = []

        for gen in range(generations + 1):  # gen 0 = initial evaluation
            print(f"\n{'=' * 60}")
            print(f"GENERATION {gen}")
            print(f"{'=' * 60}")
            print(f"Population: {len(population)} doctrines")

            # 1. Clear previous fitness history for this gen's backtest
            conn = sqlite3.connect(self.eval_db)
            conn.execute("DELETE FROM doctrine_fitness_history")
            conn.execute("DELETE FROM doctrine_regime_statistics")
            conn.commit()
            conn.close()

            # 2. Run SurvivalArena (backtest all doctrines)
            self.arena.run_cycle(backtest_dates, doctrines=population)

            # 2b. Commit 6-N: Record alpha ecology (decay + crowding) per doctrine
            self._record_ecology(gen, population, backtest_dates[-1])

            # 3. Compute fitness + alpha quality for each doctrine
            population_counts = {d.doctrine_id: 1 for d in population}
            fitness_map: dict[str, DoctrineFitnessInfo] = {}
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

            # 4. Selection Guard: survivors + breeding plan
            survivors_info = self.guard.select_survivors(fitness_map, population_counts)
            survivor_ids = {s.doctrine_id for s in survivors_info}
            survivors = [d for d in population if d.doctrine_id in survivor_ids]

            breeding_plans = self.guard.plan_breeding(
                survivors_info, target_population=len(population)
            )

            # 5. Log survival decisions
            self._log_generation(gen, population, fitness_map, survivors_info, breeding_plans)

            # 6. Record trajectory
            gen_summary = self._summarize_generation(
                gen, population, fitness_map, survivors_info, breeding_plans
            )
            trajectory.append(gen_summary)

            # 7. Print generation summary
            print(f"\n--- Gen {gen} Summary ---")
            print(f"Survivors: {len(survivors)}/{len(population)}")
            print(f"Children to breed: {sum(p.num_children for p in breeding_plans)}")
            families = {d.doctrine_id.split("_")[0] for d in survivors}
            print(f"Surviving families: {len(families)}: {sorted(families)}")
            # Top/bottom
            ranked = sorted(fitness_map.values(), key=lambda x: x.fitness, reverse=True)
            if ranked:
                print(
                    f"Top:    {ranked[0].doctrine_id} (fitness={ranked[0].fitness:.3f}, quality={ranked[0].alpha_quality:+.4f})"
                )
                print(
                    f"Bottom: {ranked[-1].doctrine_id} (fitness={ranked[-1].fitness:.3f}, quality={ranked[-1].alpha_quality:+.4f})"
                )

            if gen == generations:
                break  # don't breed after last generation

            # 8. Breed next generation
            print(f"\n--- Breeding Gen {gen + 1} ---")
            children = self._breed(survivors, breeding_plans)
            print(f"Bred {len(children)} children")

            # Save children to registry
            for child in children:
                reg.save(child)

            # Next generation = survivors + children
            population = survivors + children

        return {"trajectory": trajectory, "final_population": len(population)}

    def _record_ecology(self, gen: int, population: list[DoctrineGenome], trade_date: str) -> None:
        """Commit 6-N: Record alpha ecology state for this generation.

        For each doctrine: compute crowding score + record to alpha_decay_history.
        Also update doctrine_memory (6-N.5) with regime→family performance.
        """
        from src.evolution.alpha_ecology import AlphaEcology
        from src.factors.snapshot_builder import FactorSnapshotBuilder

        ecology = AlphaEcology(eval_db=self.eval_db, cache_db=self.arena.cache_db)
        builder = FactorSnapshotBuilder()

        # Read current gen's backtest results from doctrine_fitness_history
        conn = sqlite3.connect(self.eval_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT doctrine_id, factor_alpha, selection_alpha, origin_quality, "
                "market_regime FROM doctrine_fitness_history"
            ).fetchall()
        finally:
            conn.close()

        # Aggregate per doctrine (multiple dates per doctrine in one gen)
        doctrine_stats: dict[str, dict] = {}
        for row in rows:
            did = row["doctrine_id"]
            if did not in doctrine_stats:
                doctrine_stats[did] = {
                    "factor_alphas": [],
                    "selection_alphas": [],
                    "origin_qualities": [],
                    "regimes": [],
                }
            if row["factor_alpha"] is not None:
                doctrine_stats[did]["factor_alphas"].append(row["factor_alpha"])
            if row["selection_alpha"] is not None:
                doctrine_stats[did]["selection_alphas"].append(row["selection_alpha"])
            if row["origin_quality"] is not None:
                doctrine_stats[did]["origin_qualities"].append(row["origin_quality"])
            if row["market_regime"]:
                doctrine_stats[did]["regimes"].append(row["market_regime"])

        # Compute crowding + record
        ecology_map: dict[str, dict] = {}
        for d in population:
            stats = doctrine_stats.get(d.doctrine_id, {})
            avg_sel = (
                float(np.mean(stats["selection_alphas"])) if stats.get("selection_alphas") else 0.0
            )
            avg_fact = float(np.mean(stats["factor_alphas"])) if stats.get("factor_alphas") else 0.0
            avg_oq = (
                float(np.mean(stats["origin_qualities"])) if stats.get("origin_qualities") else 0.5
            )

            # Compute crowding (needs picks)
            picks = builder.score_universe(trade_date, d.factor_bias, top_n=20)
            crowding_info = ecology.compute_crowding(d, picks, population, trade_date)

            ecology_map[d.doctrine_id] = {
                "factor_alpha": avg_fact,
                "selection_alpha": avg_sel,
                "origin_quality": avg_oq,
                "crowding_score": crowding_info.crowding_score,
            }

            # Update doctrine_memory (6-N.5)
            family = d.doctrine_id.split("_")[0] if "_" in d.doctrine_id else d.doctrine_id
            for regime in stats.get("regimes", ["unknown"]):
                ecology.update_memory(family, regime, avg_sel)

        # Record to alpha_decay_history
        ecology.record_generation(gen, ecology_map)

        # Print ecology summary
        print(f"\n--- Ecology (Gen {gen}) ---")
        for did, info in sorted(ecology_map.items(), key=lambda x: -x[1]["crowding_score"]):
            decay = ecology.get_decay_info(did)
            decay_rate = decay.decay_rate if decay else 0.0
            half_life = decay.alpha_half_life if decay else 0
            print(
                f"  {did:36s}: crowd={info['crowding_score']:.2f} "
                f"sel_alpha={info['selection_alpha']:+.4f} "
                f"decay={decay_rate:+.2%} half_life={half_life}gen"
            )

    def _breed(
        self, survivors: list[DoctrineGenome], plans: list[BreedingPlan]
    ) -> list[DoctrineGenome]:
        """Breed children according to the guard's plan."""
        children = []
        doctrine_by_id = {d.doctrine_id: d for d in survivors}

        for plan in plans:
            parent = doctrine_by_id.get(plan.parent_doctrine_id)
            if not parent:
                continue

            for _i in range(plan.num_children):
                # Pick a partner (different doctrine for crossover, or mutate)
                partners = [d for d in survivors if d.doctrine_id != parent.doctrine_id]
                if partners:
                    import random

                    partner = random.choice(partners)
                    child = self.engine.crossover(parent, partner)
                    # Then mutate
                    mutated = self.engine.mutate(child, mutation_rate=0.1)
                    if mutated:
                        children.append(mutated)
                    else:
                        children.append(child)
                else:
                    # Only one survivor - just mutate
                    mutated = self.engine.mutate(parent, mutation_rate=0.15)
                    if mutated:
                        children.append(mutated)

        return children

    def _log_generation(
        self,
        gen: int,
        population: list[DoctrineGenome],
        fitness_map: dict,
        survivors_info: list,
        breeding_plans: list,
    ) -> None:
        """Log every doctrine's survival decision to doctrine_survival_history."""
        survivor_ids = {s.doctrine_id for s in survivors_info}
        children_counts = {p.parent_doctrine_id: p.num_children for p in breeding_plans}

        conn = sqlite3.connect(self.eval_db)
        try:
            for d in population:
                info = fitness_map.get(d.doctrine_id)
                survived = d.doctrine_id in survivor_ids
                children = children_counts.get(d.doctrine_id, 0)

                # Determine survival reason
                if survived and children > 0:
                    status = "survived_bred"
                    reason = "elite_fitness" if info and info.fitness > 0.5 else "supplement"
                elif survived:
                    status = "survived"
                    reason = "niche_protection"
                else:
                    status = "eliminated"
                    reason = "low_fitness"

                conn.execute(
                    """
                    INSERT INTO doctrine_survival_history
                    (generation, doctrine_id, parent_doctrine, children_count,
                     fitness, alpha_quality, regime_adaptation,
                     survival_status, survival_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        gen,
                        d.doctrine_id,
                        d.parent_doctrine_id,
                        children,
                        info.fitness if info else None,
                        info.alpha_quality if info else None,
                        info.regime_adaptation if info else None,
                        status,
                        reason,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _summarize_generation(
        self, gen: int, population: list, fitness_map: dict, survivors: list, breeding_plans: list
    ) -> dict:
        """Compact summary for trajectory tracking."""
        families = {}
        for d in population:
            fam = d.doctrine_id.split("_")[0]
            if fam not in families:
                families[fam] = 0
            families[fam] += 1

        fitnesses = [info.fitness for info in fitness_map.values()] if fitness_map else []
        qualities = [info.alpha_quality for info in fitness_map.values()] if fitness_map else []

        return {
            "generation": gen,
            "population_size": len(population),
            "family_count": len(families),
            "family_distribution": families,
            "avg_fitness": sum(fitnesses) / len(fitnesses) if fitnesses else 0,
            "max_fitness": max(fitnesses) if fitnesses else 0,
            "min_fitness": min(fitnesses) if fitnesses else 0,
            "avg_quality": sum(qualities) / len(qualities) if qualities else 0,
            "survivors": len(survivors),
            "children_bred": sum(p.num_children for p in breeding_plans),
        }

    def _base_identities(self) -> list[dict]:
        return [
            {
                "valuation": 90,
                "quality": 85,
                "growth": 40,
                "momentum": 15,
                "macro": 30,
                "contrarian": 80,
                "patience": 95,
                "concentration": 70,
            },
            {
                "valuation": 50,
                "quality": 70,
                "growth": 90,
                "momentum": 40,
                "macro": 45,
                "contrarian": 20,
                "patience": 50,
                "concentration": 60,
            },
            {
                "valuation": 10,
                "quality": 40,
                "growth": 40,
                "momentum": 95,
                "macro": 50,
                "contrarian": 5,
                "patience": 20,
                "concentration": 55,
            },
            {
                "valuation": 85,
                "quality": 50,
                "growth": 15,
                "momentum": 5,
                "macro": 60,
                "contrarian": 95,
                "patience": 85,
                "concentration": 65,
            },
            {
                "valuation": 75,
                "quality": 70,
                "growth": 25,
                "momentum": 10,
                "macro": 35,
                "contrarian": 55,
                "patience": 85,
                "concentration": 50,
            },
            {
                "valuation": 50,
                "quality": 95,
                "growth": 55,
                "momentum": 15,
                "macro": 25,
                "contrarian": 35,
                "patience": 90,
                "concentration": 80,
            },
            {
                "valuation": 40,
                "quality": 50,
                "growth": 50,
                "momentum": 55,
                "macro": 65,
                "contrarian": 30,
                "patience": 30,
                "concentration": 40,
            },
            {
                "valuation": 55,
                "quality": 55,
                "growth": 50,
                "momentum": 35,
                "macro": 40,
                "contrarian": 45,
                "patience": 60,
                "concentration": 65,
            },
        ]


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Evolution Observation Arena")
    parser.add_argument(
        "--generations", type=int, default=3, help="Number of generations (default 3)"
    )
    args = parser.parse_args()

    arena = EvolutionObservationArena()
    result = arena.run(generations=args.generations)

    print(f"\n{'=' * 60}")
    print("EVOLUTION TRAJECTORY SUMMARY")
    print(f"{'=' * 60}")
    for gen_info in result["trajectory"]:
        print(f"\nGen {gen_info['generation']}:")
        print(f"  Population: {gen_info['population_size']} (families: {gen_info['family_count']})")
        print(
            f"  Fitness: avg={gen_info['avg_fitness']:.3f} "
            f"min={gen_info['min_fitness']:.3f} max={gen_info['max_fitness']:.3f}"
        )
        print(f"  Alpha Quality: avg={gen_info['avg_quality']:+.4f}")
        print(f"  Survivors: {gen_info['survivors']}, Children: {gen_info['children_bred']}")
        print(f"  Family distribution: {gen_info['family_distribution']}")


if __name__ == "__main__":
    main()
