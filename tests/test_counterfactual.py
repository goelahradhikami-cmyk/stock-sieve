"""Characterization tests for CounterfactualEngine (6-Q.2b).

RECORD what the module currently does — not what it should do.

Covered surface:
  - run_ab_test orchestration: eval-date gate, empty pool, control top-20,
    treatment quality>30 + residual sort, overlap, incremental alpha
  - run_batch: verdict bands, t-stat, no_data path
  - _bare_code / _get_eval_date
"""

import sqlite3

import numpy as np
import pytest

from src.thesis.counterfactual import ABTestResult, CounterfactualEngine


@pytest.fixture()
def engine(tmp_path):
    cache = tmp_path / "cache.db"
    conn = sqlite3.connect(cache)
    conn.execute("CREATE TABLE trading_calendar (trade_date TEXT, is_trading INTEGER)")
    conn.executemany(
        "INSERT INTO trading_calendar VALUES (?,1)",
        [(f"2026-01-{d:02d}",) for d in range(1, 32)],
    )
    conn.commit()
    conn.close()
    eng = CounterfactualEngine(eval_db=str(tmp_path / "eval.db"), cache_db=str(cache))
    return eng, str(cache)


def fake_pick(sid, quality=50):
    return {"security_id": sid, "quality_score": quality}


# run_ab_test reads doctrine.factor_bias before the (stubbed) builder call
DUMMY_DOCTRINE = type("D", (), {"factor_bias": {}})()


def wire(eng, pool, residuals, ret_map):
    """Stub out builder/signal/residualizer/local with deterministic fakes."""
    eng.builder = type("B", (), {
        "score_universe": lambda self, date, bias, top_n: pool[:top_n],
    })()
    eng.signal_engine = type("S", (), {
        "compute_signals_batch": lambda self, codes, date: {
            c: type("Sig", (), {"thesis_score": 0.0})() for c in codes
        },
    })()
    eng.residualizer = type("R", (), {
        "orthogonalize_universe": lambda self, date, ts: dict(residuals),
    })()

    import pandas as pd

    def fake_kline(code, start, end):
        r = ret_map.get(code, 0.0)
        return pd.DataFrame({"close": [100.0, 100.0 * (1 + r)]})

    eng.local = type("L", (), {"get_daily_kline": staticmethod(fake_kline)})()


class TestGates:
    def test_no_eval_date(self, engine):
        eng, _ = engine
        r = eng.run_ab_test(None, "2026-01-31", horizon=20)  # not enough days ahead
        assert r.eval_date == ""
        assert r.control_picks == []

    def test_empty_pool(self, engine):
        eng, _ = engine
        wire(eng, [], {}, {})
        r = eng.run_ab_test(DUMMY_DOCTRINE, "2026-01-05", horizon=5)
        assert r.eval_date == "2026-01-10"
        assert r.control_picks == []
        assert r.control_return == 0.0


class TestOrchestration:
    def test_control_vs_treatment(self, engine):
        eng, _ = engine
        pool = [fake_pick(f"s{i:02d}") for i in range(30)]  # control = first 20
        # treatment: sort by residual desc; give low-index stocks low residuals
        residuals = {f"s{i:02d}": float(30 - i) for i in range(30)}
        # s00..s19 (control) get residual 30..11 ; s29 gets 1 ... but s25-s29 have
        # higher residual than control tail -> treatment differs from control
        residuals["s29"] = 100.0  # force into treatment
        ret_map = {f"s{i:02d}": 0.01 for i in range(30)}
        ret_map["s29"] = 0.50  # treatment star
        wire(eng, pool, residuals, ret_map)

        r = eng.run_ab_test(DUMMY_DOCTRINE, "2026-01-05", horizon=5)
        assert len(r.control_picks) == 20
        assert "s29" in r.treatment_picks
        assert r.control_return == pytest.approx(0.01)
        # treatment: 19 stocks at 0.01 + s29 at 0.50
        assert r.treatment_return == pytest.approx((19 * 0.01 + 0.50) / 20)
        assert r.incremental_alpha == pytest.approx(r.treatment_return - r.control_return)
        assert r.overlap_rate == pytest.approx(19 / 20)

    def test_quality_filter(self, engine):
        eng, _ = engine
        pool = [fake_pick(f"s{i:02d}", quality=20 if i == 29 else 50) for i in range(30)]
        residuals = {"s29": 999.0}  # highest residual but quality 20 -> excluded
        wire(eng, pool, residuals, {})
        r = eng.run_ab_test(DUMMY_DOCTRINE, "2026-01-05", horizon=5)
        assert "s29" not in r.treatment_picks

    def test_bare_code(self):
        assert CounterfactualEngine._bare_code("600519.SH") == "600519"
        assert CounterfactualEngine._bare_code("600519") == "600519"


class TestBatch:
    def stub_run(self, eng, results_by_date):
        eng.run_ab_test = lambda doctrine, d, horizon=20: results_by_date[d]

    def make_result(self, alpha, overlap=0.5):
        return ABTestResult(
            trade_date="d",
            eval_date="e",
            control_return=0.01,
            treatment_return=0.01 + alpha,
            incremental_alpha=alpha,
            overlap_rate=overlap,
        )

    def test_no_data(self, engine):
        eng, _ = engine
        self.stub_run(eng, {"2026-01-05": ABTestResult(trade_date="d", eval_date="")})
        out = eng.run_batch(None, ["2026-01-05"])
        assert out == {"n": 0, "incremental_alpha": 0, "verdict": "no_data"}

    def test_verdict_adds_value(self, engine):
        eng, _ = engine
        dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
        results = {d: self.make_result(0.02) for d in dates}  # consistent alpha
        self.stub_run(eng, results)
        out = eng.run_batch(None, dates)
        # QUIRK (pinned): np.std of identical floats is a residue (~1e-18),
        # not exactly 0 -> the std==0 t_stat=0 branch does NOT fire and
        # t_stat explodes huge -> verdict is THESIS_ADDS_VALUE
        assert out["t_stat"] > 1.5
        assert out["verdict"] == "THESIS_ADDS_VALUE"

    def test_verdict_bands(self, engine):
        eng, _ = engine
        dates = [f"d{i}" for i in range(10)]
        # varying alphas -> std > 0
        alphas = [0.01 + 0.004 * ((-1) ** i) for i in range(10)]
        results = {d: self.make_result(a) for d, a in zip(dates, alphas, strict=True)}
        self.stub_run(eng, results)
        out = eng.run_batch(None, dates)
        avg = float(np.mean(alphas))
        std = float(np.std(alphas))
        t = avg / (std / np.sqrt(10))
        assert out["t_stat"] == pytest.approx(t)
        expected_verdict = (
            "THESIS_ADDS_VALUE" if avg > 0.005 and t > 1.5
            else "THESIS_NEUTRAL" if abs(avg) < 0.005
            else "THESIS_HURTS"
        )
        assert out["verdict"] == expected_verdict
        assert out["n"] == 10
        assert out["win_rate"] == 1.0  # all alphas positive

    def test_verdict_neutral(self, engine):
        eng, _ = engine
        dates = [f"d{i}" for i in range(5)]
        results = {d: self.make_result(0.001) for d in dates}
        self.stub_run(eng, results)
        out = eng.run_batch(None, dates)
        assert out["verdict"] == "THESIS_NEUTRAL"  # abs < 0.005
