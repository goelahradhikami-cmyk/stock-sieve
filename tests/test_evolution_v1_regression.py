"""Characterization regression tests for EvolutionEngineV1.

Purpose: pin the CURRENT behavior of the production evolution engine before
structural refactors (genome.py extraction / engine rename). These tests must
stay green across the refactor — they define "no behavior change".

Scope (pure structure, no new evolution capability):
  - fitness formula + MIN_SAMPLES gating
  - cold-start grace (scoreless agents never eliminated)
  - dry-run cycle makes no state changes
  - cosine diversity
  - crossover interpolation invariants
  - mutation weight clamping
"""

import sqlite3

import numpy as np
import pytest
import yaml

from src.evolution.engine_v1 import EvolutionEngineV1

# ── Minimal schema the engine touches ─────────────────────

SCHEMA = """
CREATE TABLE evolution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER, event_type TEXT, agent_id TEXT, parent_id TEXT,
    description TEXT, details_json TEXT
);
CREATE TABLE agent_genome_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT, strategy_genus TEXT, strategy_species TEXT,
    generation INTEGER, parent_agent_id TEXT, genome_hash TEXT,
    genome_yaml TEXT, birth_date TEXT, frozen_date TEXT, status TEXT
);
CREATE TABLE research_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT
);
CREATE TABLE evaluation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    research_decision_id INTEGER, alpha_vs_market REAL,
    max_drawdown_during REAL, eval_date TEXT,
    alpha_error REAL, market_regime TEXT
);
"""


def make_db(tmp_path) -> str:
    db_path = str(tmp_path / "evaluation.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def genome_yaml(agent_id: str, genus: str, dims: dict) -> str:
    return yaml.dump(
        {
            "identity": {"agent_id": agent_id, "strategy_genus": genus, "generation": 1},
            "investment_identity": {"dimensions": dims},
        }
    )


def add_agent(db_path: str, agent_id: str, genus: str = "value", dims: dict | None = None):
    dims = dims or {"valuation": 50, "quality": 50, "growth": 50, "momentum": 50}
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO agent_genome_snapshots "
        "(agent_id, strategy_genus, strategy_species, generation, genome_hash, "
        " genome_yaml, birth_date, status) VALUES (?,?,?,?,?,?,date('now'),'active')",
        (agent_id, genus, "species_x", 1, f"hash_{agent_id}", genome_yaml(agent_id, genus, dims)),
    )
    conn.commit()
    conn.close()


def add_evals(db_path: str, agent_id: str, n: int, alpha: float, dd: float = -0.05):
    conn = sqlite3.connect(db_path)
    for _ in range(n):
        cur = conn.execute("INSERT INTO research_decisions (agent_id) VALUES (?)", (agent_id,))
        conn.execute(
            "INSERT INTO evaluation_results "
            "(research_decision_id, alpha_vs_market, max_drawdown_during, eval_date, "
            " alpha_error, market_regime) VALUES (?,?,?,date('now'),NULL,'bull')",
            (cur.lastrowid, alpha, dd),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def engine(tmp_path):
    db_path = make_db(tmp_path)
    eng = EvolutionEngineV1(db_path=db_path, dry_run=True)
    eng.MIN_SAMPLES = 3
    yield eng
    eng.db.close()


# ── Fitness ────────────────────────────────────────────────


class TestFitness:
    def test_below_min_samples_returns_none(self, engine, tmp_path):
        add_agent(str(tmp_path / "evaluation.db"), "a1")
        add_evals(str(tmp_path / "evaluation.db"), "a1", n=2, alpha=0.10)
        assert engine._calculate_fitness("a1") is None

    def test_fitness_formula_pinned(self, engine, tmp_path):
        db_path = str(tmp_path / "evaluation.db")
        add_agent(db_path, "a1")
        add_evals(db_path, "a1", n=3, alpha=0.10, dd=-0.05)
        f = engine._calculate_fitness("a1")
        assert f is not None
        # 0.10*0.30 + 1.0*0.25 + (-0.05)*0.20 + (1-0.5)*0.15 + 0.10 + 0.5*0.10
        assert f.fitness == pytest.approx(0.495, abs=1e-9)
        assert f.avg_alpha == pytest.approx(0.10)
        assert f.win_rate == pytest.approx(1.0)
        assert f.sample_count == 3
        assert f.genome_hash == "hash_a1"
        assert f.strategy_genus == "value"


# ── Diversity ──────────────────────────────────────────────


class TestDiversity:
    def test_cosine_distance(self, engine):
        a = np.array([1.0, 0.0])
        b = np.array([0.0, 1.0])
        assert engine._cosine_distance(a, b) == pytest.approx(1.0)
        assert engine._cosine_distance(a, a) == pytest.approx(0.0)
        assert engine._cosine_distance(np.zeros(3), a) == 1.0


# ── Mutation clamping ──────────────────────────────────────


class TestMutate:
    def test_weights_clamped_to_bounds(self, engine):
        genome = {"factor_model": {"quality": {"weight": 0.50}, "value": {"weight": 0.0}}}
        for _ in range(50):
            out = engine._mutate(genome)
            for cfg in out["factor_model"].values():
                assert 0.0 <= cfg["weight"] <= 0.50

    def test_post_mortem_nudge_clamped_at_100(self, engine):
        genome = {"investment_identity": {"dimensions": {"valuation": 98}}}
        mutations = [{"target": "valuation_gate"}]  # maps to ("valuation", +8)
        out = engine._apply_post_mortem_mutations(genome, mutations)
        assert out["investment_identity"]["dimensions"]["valuation"] == 100


# ── Full cycle (dry-run) ───────────────────────────────────


class TestRunCycleDryRun:
    def test_skips_when_fewer_than_3_agents(self, engine, tmp_path):
        db_path = str(tmp_path / "evaluation.db")
        add_agent(db_path, "a1")
        add_agent(db_path, "a2")
        result = engine.run_cycle()
        assert result["status"] == "skipped"
        assert result["reason"] == "insufficient_agents"

    def test_dry_run_pins_selection_shape(self, engine, tmp_path):
        db_path = str(tmp_path / "evaluation.db")
        # 3 scored agents: elite / mid / bottom
        add_agent(db_path, "elite", dims={"valuation": 90, "quality": 90})
        add_agent(db_path, "mid", dims={"valuation": 50, "quality": 50})
        add_agent(db_path, "bottom", dims={"valuation": 10, "quality": 10})
        add_evals(db_path, "elite", n=3, alpha=0.20)
        add_evals(db_path, "mid", n=3, alpha=0.05)
        add_evals(db_path, "bottom", n=3, alpha=-0.10)
        # 1 cold-start agent: no evals, must be protected
        add_agent(db_path, "newborn", dims={"valuation": 0, "quality": 0})

        result = engine.run_cycle()

        assert result["mode"] == "dry_run"
        assert "newborn" not in result["eliminated_candidates"]
        # bottom is the only scored agent in the bottom fraction
        assert result["eliminated_candidates"] == ["bottom"]
        assert "elite" in result["elites"]

        # Dry-run must not freeze anyone
        conn = sqlite3.connect(db_path)
        active = {
            r[0]
            for r in conn.execute(
                "SELECT agent_id FROM agent_genome_snapshots WHERE status='active'"
            ).fetchall()
        }
        conn.close()
        assert active == {"elite", "mid", "bottom", "newborn"}


# ── Crossover invariants ───────────────────────────────────


class TestCrossover:
    def test_child_invariants(self, engine, tmp_path):
        db_path = str(tmp_path / "evaluation.db")
        add_agent(db_path, "pa", dims={"valuation": 80, "quality": 20})
        add_agent(db_path, "pb", dims={"valuation": 40, "quality": 60})
        from src.evolution.engine_v1 import AgentFitness

        a = AgentFitness("pa", "h1", "value", 0.9, 0.1, 0.8, -0.05, 10)
        b = AgentFitness("pb", "h2", "value", 0.8, 0.05, 0.7, -0.03, 10)

        np.random.seed(7)
        child = engine._crossover(a, b)

        assert child["identity"]["parent_agent_id"] == "pa"
        assert child["identity"]["generation"] == 2
        dims = child["investment_identity"]["dimensions"]
        # interpolation must land between the parents (inclusive)
        assert 40 <= dims["valuation"] <= 80
        assert 20 <= dims["quality"] <= 60
