"""Characterization tests for BayesianAllocationEngine (6-S.6.2).

RECORD what the module currently does — not what it should do.

Covered surface:
  - constants (priors / half-life / temperature / doctrine list)
  - compute_allocation: no-data uniform, softmax with centered scores
  - _compute_doctrine_stats: None gates, posterior math, sharpe clip,
    time-decay weights, bad-date 0.5 fallback
  - get_full_allocation_table structure
"""

import math
import sqlite3

import pytest

from src.thesis.bayesian_allocation import BayesianAllocationEngine

SCHEMA = (
    "CREATE TABLE thesis_ledger (quality_verdict TEXT, contrarian_verdict TEXT, "
    "value_verdict TEXT, actual_return REAL, trade_date TEXT, market_state TEXT)"
)


@pytest.fixture()
def engine(tmp_path):
    db = tmp_path / "eval.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    return BayesianAllocationEngine(eval_db=str(db)), str(db)


def insert(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO thesis_ledger VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class TestConstants:
    def test_values(self, engine):
        eng, _ = engine
        assert eng.PRIOR_ALPHA == 2.0
        assert eng.PRIOR_BETA == 2.0
        assert eng.TIME_DECAY_HALF_LIFE_DAYS == 365
        assert eng.SOFTMAX_TEMPERATURE == 10.0
        assert eng.DOCTRINES == ["quality", "contrarian", "value"]


class TestComputeAllocation:
    def test_no_data_uniform(self, engine):
        eng, _ = engine
        alloc = eng.compute_allocation("PANIC", "2026-01-01")
        assert alloc == pytest.approx({d: 1 / 3 for d in eng.DOCTRINES})

    def test_allocation_sums_to_one(self, engine):
        eng, db = engine
        insert(
            db,
            [
                ("PASS", None, None, 0.10, "2025-12-01", "PANIC"),
                ("PASS", None, None, 0.05, "2025-11-01", "PANIC"),
            ],
        )
        alloc = eng.compute_allocation("PANIC", "2026-01-01")
        assert sum(alloc.values()) == pytest.approx(1.0)
        assert set(alloc) == set(eng.DOCTRINES)

    def test_better_doctrine_gets_more_weight(self, engine):
        eng, db = engine
        # quality: strong positive returns; contrarian: negative
        insert(
            db,
            [
                ("PASS", None, None, 0.20, "2025-12-01", "PANIC"),
                ("PASS", None, None, 0.15, "2025-12-01", "PANIC"),
                (None, "PASS", None, -0.10, "2025-12-01", "PANIC"),
                (None, "PASS", None, -0.05, "2025-12-01", "PANIC"),
            ],
        )
        alloc = eng.compute_allocation("PANIC", "2026-01-01")
        assert alloc["quality"] > alloc["contrarian"]


class TestDoctrineStats:
    def test_no_rows_none(self, engine):
        eng, _ = engine
        assert eng._compute_doctrine_stats("quality", "PANIC", "2026-01-01") is None

    def test_no_pass_verdicts_none(self, engine):
        eng, db = engine
        insert(db, [("REJECT", None, None, 0.10, "2025-12-01", "PANIC")])
        assert eng._compute_doctrine_stats("quality", "PANIC", "2026-01-01") is None

    def test_posterior_math(self, engine):
        eng, db = engine
        # 3 passes: 2 positive, 1 negative
        insert(
            db,
            [
                ("PASS", None, None, 0.10, "2025-12-01", "PANIC"),
                ("PASS", None, None, 0.05, "2025-12-02", "PANIC"),
                ("PASS", None, None, -0.03, "2025-12-03", "PANIC"),
            ],
        )
        s = eng._compute_doctrine_stats("quality", "PANIC", "2026-01-01")
        assert s.n_theses == 3
        assert s.n_success == 2
        assert s.n_failure == 1
        assert s.alpha == 4.0 and s.beta == 3.0
        assert s.posterior_win_prob == pytest.approx(4 / 7)

    def test_sharpe_clip_and_return_quality(self, engine):
        eng, db = engine
        # huge sharpe -> clipped at 2.0
        insert(db, [("PASS", None, None, 0.50, "2025-06-01", "PANIC")])
        s = eng._compute_doctrine_stats("quality", "PANIC", "2026-01-01")
        # single return: std=0.1 -> sharpe=5 -> clip 2.0
        assert s.return_quality == pytest.approx(s.posterior_win_prob * (1.0 + 2.0))

    def test_time_decay_recent_heavier(self, engine):
        eng, db = engine
        # same magnitude return, one recent one old: weighted_return ≈ 0.1 both,
        # but weights differ; check stats constructed without error and
        # time_weighted_score uses weighted sharpe
        insert(
            db,
            [
                ("PASS", None, None, 0.10, "2025-12-30", "PANIC"),
                ("PASS", None, None, 0.10, "2024-01-01", "PANIC"),
            ],
        )
        s = eng._compute_doctrine_stats("quality", "PANIC", "2026-01-01")
        assert s is not None
        # with identical returns, weighted_std -> floor 0.01, sharpe = 0.10/0.01 = 10 -> clip 2.0
        assert s.time_weighted_score == pytest.approx(s.posterior_win_prob * 3.0)

    def test_bad_date_weight_half(self, engine):
        eng, db = engine
        insert(
            db,
            [
                ("PASS", None, None, 0.10, "not-a-date", "PANIC"),
                ("PASS", None, None, -0.10, "2025-12-30", "PANIC"),
            ],
        )
        s = eng._compute_doctrine_stats("quality", "PANIC", "2026-01-01")
        # bad date -> weight 0.5; recent -> weight ~1.0
        # weighted_return = (0.5*0.10 + 1.0*(-0.10)) / 1.5
        w_bad, w_good = 0.5, math.exp(-math.log(2) / 365 * 2)
        wr = (w_bad * 0.10 + w_good * (-0.10)) / (w_bad + w_good)
        wstd = max(
            0.01,
            math.sqrt((w_bad * (0.10 - wr) ** 2 + w_good * (-0.10 - wr) ** 2) / (w_bad + w_good)),
        )
        wsharpe = max(-0.5, min(2.0, wr / wstd))
        assert s.time_weighted_score == pytest.approx(s.posterior_win_prob * (1.0 + wsharpe))


class TestFullAllocationTable:
    def test_structure(self, engine):
        eng, _ = engine
        table = eng.get_full_allocation_table("2026-01-01")
        assert set(table) == {"PANIC", "STABILIZING", "EARLY_RECOVERY", "CONFIRMED_RECOVERY"}
        for _state, payload in table.items():
            assert set(payload) == {"allocation", "stats"}
            assert sum(payload["allocation"].values()) == pytest.approx(1.0)
            # no data -> stats defaults
            for d in eng.DOCTRINES:
                assert payload["stats"][d]["n"] == 0
                assert payload["stats"][d]["win_rate"] == 0.5
