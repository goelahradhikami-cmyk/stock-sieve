"""Characterization tests for ThesisResidualizer (6-Q.2).

RECORD what the module currently does — not what it should do.

Covered surface:
  - FACTOR_FAMILIES constant
  - orthogonalize_universe: empty passthrough, <10 matched passthrough,
    OLS residual on >=10 matched, factor normalization /100 + default 50
  - orthogonalize_single: no-betas passthrough, factor component, orthogonality
  - _load_factor_scores: NULL -> 50 (seeded tmp sqlite)
"""

import sqlite3

import numpy as np
import pytest

from src.thesis.residualizer import ThesisResidualizer

SCHEMA = (
    "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, "
    "quality_score REAL, value_score REAL, growth_score REAL, "
    "momentum_score REAL, risk_score REAL, sentiment_score REAL)"
)


@pytest.fixture()
def residualizer(tmp_path):
    db = tmp_path / "eval.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    return ThesisResidualizer(eval_db=str(db)), str(db)


def seed_factors(db, date, qualities):
    """Seed stocks q0..qN with varying quality; other factors fixed at 50."""
    conn = sqlite3.connect(db)
    rows = [(f"q{i:02d}", date, q, 50.0, 50.0, 50.0, 50.0, 50.0) for i, q in enumerate(qualities)]
    conn.executemany("INSERT INTO stock_factor_snapshot VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class TestConstants:
    def test_factor_families(self):
        assert ThesisResidualizer.FACTOR_FAMILIES == [
            "quality",
            "value",
            "growth",
            "momentum",
            "risk",
            "sentiment",
        ]


class TestOrthogonalizeUniverse:
    def test_empty_input(self, residualizer):
        rz, _ = residualizer
        assert rz.orthogonalize_universe("2023-01-31", {}) == {}

    def test_too_few_matched_returns_raw(self, residualizer):
        rz, db = residualizer
        seed_factors(db, "2023-01-31", [10, 20, 30])
        signals = {f"q{i:02d}": {"thesis_score": float(i)} for i in range(3)}
        out = rz.orthogonalize_universe("2023-01-31", signals)
        # < 10 matched -> raw thesis passthrough
        assert out == {f"q{i:02d}": float(i) for i in range(3)}

    def test_perfect_factor_explanation_zero_residual(self, residualizer):
        rz, db = residualizer
        qualities = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        seed_factors(db, "2023-01-31", qualities)
        # thesis exactly = 2 * (quality/100) -> OLS explains everything
        signals = {f"q{i:02d}": {"thesis_score": 2 * q / 100.0} for i, q in enumerate(qualities)}
        out = rz.orthogonalize_universe("2023-01-31", signals)
        assert len(out) == 10
        for v in out.values():
            assert abs(v) < 1e-10

    def test_orthogonal_signal_survives(self, residualizer):
        rz, db = residualizer
        qualities = [50.0] * 10  # all factors identical -> no explanatory power
        seed_factors(db, "2023-01-31", qualities)
        signals = {f"q{i:02d}": {"thesis_score": float(i) - 4.5} for i in range(10)}
        out = rz.orthogonalize_universe("2023-01-31", signals)
        # constant F columns can only explain the mean; residuals = demeaned T
        for i in range(10):
            assert out[f"q{i:02d}"] == pytest.approx(float(i) - 4.5, abs=1e-8)

    def test_unmatched_codes_dropped_when_regression_runs(self, residualizer):
        rz, db = residualizer
        qualities = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        seed_factors(db, "2023-01-31", qualities)
        signals = {f"q{i:02d}": {"thesis_score": 2 * q / 100.0} for i, q in enumerate(qualities)}
        signals["zz99"] = {"thesis_score": 9.9}  # no factor data
        out = rz.orthogonalize_universe("2023-01-31", signals)
        assert "zz99" not in out  # only matched codes returned

    def test_missing_thesis_score_key_not_matched(self, residualizer):
        rz, db = residualizer
        qualities = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        seed_factors(db, "2023-01-31", qualities)
        signals = {f"q{i:02d}": {"other": 1.0} for i in range(10)}
        out = rz.orthogonalize_universe("2023-01-31", signals)
        # 0 matched -> <10 branch -> raw passthrough with default 0
        assert out == {f"q{i:02d}": 0 for i in range(10)}


class TestOrthogonalizeSingle:
    def test_no_betas_passthrough(self, residualizer):
        rz, _ = residualizer
        sig = rz.orthogonalize_single(0.15, {"quality": 80.0}, universe_betas=None)
        assert sig.raw_thesis == 0.15
        assert sig.factor_component == 0.0
        assert sig.residual == 0.15
        assert sig.orthogonality == 1.0

    def test_with_betas(self, residualizer):
        rz, _ = residualizer
        betas = np.array([2.0, 0, 0, 0, 0, 0])
        # quality 100 -> F = [1.0, 0.5, 0.5, 0.5, 0.5, 0.5] -> component = 2.0
        sig = rz.orthogonalize_single(3.0, {"quality": 100.0}, universe_betas=betas)
        assert sig.factor_component == pytest.approx(2.0)
        assert sig.residual == pytest.approx(1.0)
        assert sig.orthogonality == pytest.approx(max(0.0, 1.0 - abs(2.0 / 3.0)))

    def test_orthogonality_floored_at_zero(self, residualizer):
        rz, _ = residualizer
        betas = np.array([10.0, 0, 0, 0, 0, 0])
        sig = rz.orthogonalize_single(0.5, {"quality": 100.0}, universe_betas=betas)
        assert sig.orthogonality == 0.0  # factor dominates -> floored

    def test_zero_thesis_orthogonality_one(self, residualizer):
        rz, _ = residualizer
        betas = np.array([2.0, 0, 0, 0, 0, 0])
        sig = rz.orthogonalize_single(0.0, {"quality": 100.0}, universe_betas=betas)
        assert sig.orthogonality == 1.0  # |thesis| <= 1e-6 branch


class TestLoadFactorScores:
    def test_null_becomes_50(self, residualizer):
        rz, db = residualizer
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO stock_factor_snapshot VALUES (?,?,?,?,?,?,?,?)",
            ("600519", "2023-01-31", None, 60.0, None, None, None, None),
        )
        conn.commit()
        conn.close()
        scores = rz._load_factor_scores("2023-01-31")
        assert scores["600519"]["quality"] == 50
        assert scores["600519"]["value"] == 60.0
        assert scores["600519"]["risk"] == 50
