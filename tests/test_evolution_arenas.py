"""Characterization tests for the three evolution arenas.

Pins CURRENT behavior of the three arenas through two refactors:
  - arena.py            -> tournament.py            -> archive/tournament.py     [archived]
  - competitive_arena.py -> crowding_arena.py       -> archive/crowding_arena.py [archived]
  - survival_arena.py   (DoctrineSurvivalArena)     -> unchanged (active production)

These tests must stay green across the rename — they define "no behavior change".
Heavy provider/network dependencies are bypassed (``__new__`` + direct attribute
injection) so the tests pin logic, not I/O.
"""

import sqlite3
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.agents.doctrine_engine import DoctrineGenome
from src.evolution.archive.crowding_arena import CompetitiveArena
from src.evolution.archive.tournament import EvolutionArena
from src.evolution.survival_arena import DoctrineSurvivalArena

# ─────────────────────────────────────────────────────────
# arena.py — EvolutionArena (tournament engine)
# ─────────────────────────────────────────────────────────


def make_arena(db_path: str) -> EvolutionArena:
    """Build an EvolutionArena without touching market-data providers."""
    arena = EvolutionArena.__new__(EvolutionArena)
    arena.db = sqlite3.connect(db_path)
    arena._ensure_table()
    return arena


def kline_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


class TestEvolutionArena:
    def test_monthly_trade_dates_capped_at_12(self, tmp_path):
        arena = make_arena(str(tmp_path / "a.db"))
        dates = arena._get_monthly_trade_dates("2024-01-01", "2026-06-30")
        assert dates[0] == date(2024, 1, 1)
        assert len(dates) == 12  # capped
        assert dates[1] == date(2024, 2, 1)
        assert dates[11] == date(2024, 12, 1)
        arena.db.close()

    def test_genome_score_formula(self, tmp_path):
        arena = make_arena(str(tmp_path / "a.db"))
        closes = list(np.linspace(100, 120, 80))  # 80 bars, steady uptrend
        df = kline_df(closes)

        # Zero weights -> zero score
        assert arena._calc_genome_score({}, df) == 0

        # Pure quality weight contributes exactly weight*30
        genome = {"factor_model": {"quality": {"weight": 0.5}}}
        assert arena._calc_genome_score(genome, df) == pytest.approx(15.0)

        # Too-short kline -> 0
        assert arena._calc_genome_score(genome, kline_df([1.0, 2.0])) == 0
        arena.db.close()

    def test_rankings_fitness_formula(self, tmp_path):
        arena = make_arena(str(tmp_path / "a.db"))
        arena.db.execute(
            """
            INSERT INTO evolution_arena_results
            (cycle_id, agent_id, trade_date, horizon, alpha_vs_market,
             sharpe, sortino, max_drawdown, win_rate)
            VALUES (1, 'a1', '2026-01-01', 20, 0.10, 1.0, 1.2, -0.05, 0.6)
            """
        )
        arena.db.execute(
            """
            INSERT INTO evolution_arena_results
            (cycle_id, agent_id, trade_date, horizon, alpha_vs_market,
             sharpe, sortino, max_drawdown, win_rate)
            VALUES (1, 'a2', '2026-01-01', 20, -0.02, 0.5, 0.4, -0.20, 0.4)
            """
        )
        arena.db.commit()

        rankings = arena._calculate_rankings(cycle_id=1)
        assert [r["agent_id"] for r in rankings] == ["a1", "a2"]
        # fitness = alpha*0.35 + sharpe*0.25 + sortino*0.15 + wr*0.15 - abs(dd)*0.10
        expected_a1 = 0.10 * 0.35 + 1.0 * 0.25 + 1.2 * 0.15 + 0.6 * 0.15 - 0.05 * 0.10
        assert rankings[0]["fitness"] == pytest.approx(expected_a1, abs=1e-9)
        assert rankings[0]["rank"] == 1
        assert rankings[1]["rank"] == 2
        arena.db.close()

    def test_save_and_read_active_agents(self, tmp_path):
        db_path = str(tmp_path / "a.db")
        arena = make_arena(db_path)
        arena.db.execute(
            """
            CREATE TABLE agent_genome_snapshots (
                agent_id TEXT, strategy_genus TEXT, genome_yaml TEXT, status TEXT
            )
            """
        )
        arena.db.execute(
            "INSERT INTO agent_genome_snapshots VALUES ('x1', 'value', 'identity: {}', 'active')"
        )
        arena.db.execute(
            "INSERT INTO agent_genome_snapshots VALUES ('x2', 'growth', 'identity: {}', 'frozen')"
        )
        arena.db.commit()

        agents = arena._get_active_agents()
        assert [a["agent_id"] for a in agents] == ["x1"]
        assert agents[0]["strategy_genus"] == "value"
        arena.db.close()


# ─────────────────────────────────────────────────────────
# crowding_arena.py — CompetitiveArena (crowding metrics)
# ─────────────────────────────────────────────────────────

SNAPSHOT_SCHEMA = """
CREATE TABLE stock_factor_snapshot (
    security_id TEXT, trade_date TEXT,
    quality_score REAL, value_score REAL, growth_score REAL,
    momentum_score REAL, risk_score REAL, sentiment_score REAL
);
"""


def seed_snapshot(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript(SNAPSHOT_SCHEMA)
    # s1: high quality; s2: high value; s3: mid both
    rows = [
        ("s1", "2026-07-01", 90, 10, 0, 0, 0, 0),
        ("s2", "2026-07-01", 10, 90, 0, 0, 0, 0),
        ("s3", "2026-07-01", 50, 50, 0, 0, 0, 0),
    ]
    conn.executemany("INSERT INTO stock_factor_snapshot VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class TestCompetitiveArena:
    def test_avg_overlap_jaccard(self, tmp_path):
        arena = CompetitiveArena(eval_db_path=str(tmp_path / "c.db"))
        picks = {"d1": ["a", "b", "c"], "d2": ["b", "c", "d"]}
        # intersection {b,c}=2, union {a,b,c,d}=4 -> 0.5
        assert arena._avg_overlap(picks) == pytest.approx(0.5)
        assert arena._avg_overlap({"d1": ["a"]}) == 0.0  # single agent
        assert arena._avg_overlap({}) == 0.0

    def test_score_and_pick_orders_by_bias(self, tmp_path):
        db_path = str(tmp_path / "c.db")
        seed_snapshot(db_path)
        arena = CompetitiveArena(eval_db_path=db_path)

        quality_picks = arena._score_and_pick("2026-07-01", {"quality": 1.0}, top_n=2)
        assert quality_picks[0] == "s1"  # highest quality_score

        value_picks = arena._score_and_pick("2026-07-01", {"value": 1.0}, top_n=2)
        assert value_picks[0] == "s2"  # highest value_score

    def test_run_competition_crowding_metrics(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "c.db")
        seed_snapshot(db_path)
        arena = CompetitiveArena(eval_db_path=db_path)
        # Pin only crowding behavior; skip the network-dependent alpha-decay leg
        monkeypatch.setattr(arena, "_compute_alpha_decay", lambda *a, **k: None)

        d_q = DoctrineGenome(doctrine_id="doc_quality", factor_bias={"quality": 1.0})
        d_v = DoctrineGenome(doctrine_id="doc_value", factor_bias={"value": 1.0})
        result = arena.run_competition("2026-07-01", top_n=2, doctrines=[d_q, d_v])

        assert result.num_agents == 2
        assert result.doctrine_picks["doc_quality"] == ["s1", "s3"]
        assert result.doctrine_picks["doc_value"] == ["s2", "s3"]
        # s3 picked by both -> crowding 2; s1/s2 -> 1
        assert result.stock_crowding == {"s1": 1, "s3": 2, "s2": 1}
        assert result.max_crowding == 2
        assert result.crowded_stocks == [("s3", 2)]
        # overlap: |{s3}| / |{s1,s2,s3}| = 1/3
        assert result.avg_overlap_rate == pytest.approx(1 / 3)


# ─────────────────────────────────────────────────────────
# survival_arena.py — DoctrineSurvivalArena (active production)
# ─────────────────────────────────────────────────────────


def make_survival_arena(eval_db: str, cache_db: str) -> DoctrineSurvivalArena:
    arena = DoctrineSurvivalArena.__new__(DoctrineSurvivalArena)
    arena.eval_db = eval_db
    arena.cache_db = cache_db
    return arena


class TestSurvivalArena:
    def test_eval_date_offsets_by_horizon(self, tmp_path):
        cache_db = str(tmp_path / "cache.db")
        conn = sqlite3.connect(cache_db)
        conn.execute("CREATE TABLE trading_calendar (trade_date TEXT, is_trading INTEGER)")
        # 25 consecutive trading days from 2026-07-01
        for i in range(1, 26):
            conn.execute("INSERT INTO trading_calendar VALUES (?, 1)", (f"2026-07-{i:02d}",))
        conn.commit()
        conn.close()

        arena = make_survival_arena(str(tmp_path / "eval.db"), cache_db)
        # horizon=20 -> 20th trading day after 2026-07-01
        assert arena._eval_date("2026-07-01", 20) == "2026-07-21"
        # beyond calendar -> None
        assert arena._eval_date("2026-07-10", 20) is None

    def test_summary_aggregates_history(self, tmp_path):
        eval_db = str(tmp_path / "eval.db")
        conn = sqlite3.connect(eval_db)
        conn.execute(
            """
            CREATE TABLE doctrine_fitness_history (
                doctrine_id TEXT, trade_date TEXT, market_regime TEXT,
                total_return REAL, market_beta REAL, sector_return REAL,
                residual_alpha REAL, drawdown REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO doctrine_fitness_history VALUES "
            "('d1', '2026-07-01', 'bull', 0.10, 0.04, 0.02, 0.04, -0.03)"
        )
        conn.execute(
            "INSERT INTO doctrine_fitness_history VALUES "
            "('d1', '2026-07-02', 'bull', 0.20, 0.08, 0.04, 0.08, -0.01)"
        )
        conn.commit()
        conn.close()

        arena = make_survival_arena(eval_db, str(tmp_path / "cache.db"))
        summary = arena._summary([DoctrineGenome(doctrine_id="d1")])
        assert summary["d1"]["n"] == 2
        assert summary["d1"]["avg_total"] == pytest.approx(0.15)
        assert summary["d1"]["avg_residual"] == pytest.approx(0.06)
        assert summary["d1"]["avg_beta"] == pytest.approx(0.06)

    def test_base_identities_shape(self, tmp_path):
        arena = make_survival_arena(str(tmp_path / "e.db"), str(tmp_path / "c.db"))
        identities = arena._base_identities()
        assert len(identities) == 8
        expected_dims = {
            "valuation",
            "quality",
            "growth",
            "momentum",
            "macro",
            "contrarian",
            "patience",
            "concentration",
        }
        for iv in identities:
            assert set(iv.keys()) == expected_dims
