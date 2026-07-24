"""Characterization tests for MarketAnomalyDetector / MispricingObject.

These tests RECORD what the module currently does — they do not prescribe
what it should do. If an assertion fails after a code change, the change
altered behavior and must be justified explicitly.

Covered surface:
  - V3CandidateFeatures defaults (all None, advisory-only)
  - MispricingObject.is_anomaly thresholds
  - is_true_mispricing 4-filter chain
  - triage_label all branches
  - to_dict rounding
  - _detect_anomaly composite math + divergence_type branch priority (faked providers)
  - _compute_roe_stability / _compute_momentum_rank / _get_universe (seeded tmp sqlite)
"""

import sqlite3

import pytest

from src.thesis.market_anomaly import (
    MarketAnomalyDetector,
    MispricingObject,
    V3CandidateFeatures,
)


def make_obj(**kw) -> MispricingObject:
    base = dict(code="600000", trade_date="2023-01-31")
    base.update(kw)
    return MispricingObject(**base)


# ── V3CandidateFeatures ────────────────────────────────────


class TestV3CandidateFeatures:
    def test_all_defaults_none(self):
        f = V3CandidateFeatures()
        for field in (
            "recovery_score",
            "earnings_acceleration",
            "frm_direction",
            "frm_score",
            "relative_strength",
            "sector_strength",
            "rs_score",
            "liquidity_pass",
            "candidate_stage",
            "rejection_reason",
        ):
            assert getattr(f, field) is None


# ── is_anomaly ─────────────────────────────────────────────


class TestIsAnomaly:
    def test_default_threshold_015(self):
        obj = make_obj(divergence_score=0.16, market_pessimism=0.5, business_strength=0.5)
        assert obj.is_anomaly() is True
        obj15 = make_obj(divergence_score=0.15, market_pessimism=0.5, business_strength=0.5)
        assert obj15.is_anomaly() is False  # strict >

    def test_custom_threshold(self):
        obj = make_obj(divergence_score=0.25, market_pessimism=0.5, business_strength=0.5)
        assert obj.is_anomaly(threshold=0.30) is False
        assert obj.is_anomaly(threshold=0.20) is True

    def test_pessimism_gate_at_04(self):
        obj = make_obj(divergence_score=0.9, market_pessimism=0.4, business_strength=0.9)
        assert obj.is_anomaly() is False  # needs > 0.4
        obj2 = make_obj(divergence_score=0.9, market_pessimism=0.41, business_strength=0.9)
        assert obj2.is_anomaly() is True

    def test_strength_gate_at_04(self):
        obj = make_obj(divergence_score=0.9, market_pessimism=0.9, business_strength=0.4)
        assert obj.is_anomaly() is False

    def test_defaults_not_anomaly(self):
        assert make_obj().is_anomaly() is False


# ── is_true_mispricing ─────────────────────────────────────


class TestIsTrueMispricing:
    def good(self, **kw):
        base = dict(
            price_drawdown_12m=-0.30,
            roe=0.15,
            margin_change=0.05,
            divergence_score=0.30,
            market_pessimism=0.6,
            business_strength=0.7,
        )
        base.update(kw)
        return make_obj(**base)

    def test_passes_all_filters(self):
        assert self.good().is_true_mispricing() is True

    def test_filter1_drawdown_boundary(self):
        # actual behavior: check is `> -0.15`, so exactly -0.15 PASSES filter 1
        assert self.good(price_drawdown_12m=-0.15).is_true_mispricing() is True
        assert self.good(price_drawdown_12m=-0.149).is_true_mispricing() is False
        assert self.good(price_drawdown_12m=0.0).is_true_mispricing() is False

    def test_filter2_roe_bounds(self):
        # actual behavior: check is `roe > 0.50 or roe < 0.02`, boundaries inclusive-pass
        assert self.good(roe=0.50).is_true_mispricing() is True
        assert self.good(roe=0.51).is_true_mispricing() is False
        assert self.good(roe=0.02).is_true_mispricing() is True
        assert self.good(roe=0.01).is_true_mispricing() is False

    def test_filter3_margin_plausibility(self):
        assert self.good(margin_change=0.50).is_true_mispricing() is True  # abs not > 0.50
        assert self.good(margin_change=0.51).is_true_mispricing() is False
        assert self.good(margin_change=-0.51).is_true_mispricing() is False

    def test_filter4_requires_anomaly(self):
        assert self.good(divergence_score=0.10).is_true_mispricing() is False


# ── triage_label ───────────────────────────────────────────


class TestTriageLabel:
    def test_true_mispricing_cyclical(self):
        obj = make_obj(
            price_drawdown_12m=-0.30,
            roe=0.15,
            margin_change=0.05,
            divergence_score=0.30,
            divergence_type="cyclical_misjudgment",
            market_pessimism=0.55,
            business_strength=0.65,
        )
        assert obj.triage_label() == "true_mispricing"

    def test_true_mispricing_strong_divergence(self):
        obj = make_obj(
            price_drawdown_12m=-0.30,
            roe=0.15,
            margin_change=0.05,
            divergence_score=0.30,
            divergence_type="market_overreaction",
            market_pessimism=0.65,
            business_strength=0.65,
        )
        assert obj.triage_label() == "true_mispricing"

    def test_true_mispricing_but_weak_gates_uncertain(self):
        obj = make_obj(
            price_drawdown_12m=-0.30,
            roe=0.15,
            margin_change=0.05,
            divergence_score=0.30,
            divergence_type="minor_divergence",
            market_pessimism=0.55,
            business_strength=0.55,
        )
        assert obj.triage_label() == "uncertain"

    def test_value_trap_pessimism_strength(self):
        obj = make_obj(market_pessimism=0.6, business_strength=0.4)
        assert obj.triage_label() == "value_trap"

    def test_value_trap_margin_decline(self):
        obj = make_obj(
            market_pessimism=0.3,
            business_strength=0.6,
            margin_change=-0.06,
        )
        assert obj.triage_label() == "value_trap"

    def test_margin_decline_boundary_not_trap(self):
        obj = make_obj(market_pessimism=0.3, business_strength=0.6, margin_change=-0.05)
        assert obj.triage_label() == "uncertain"

    def test_default_uncertain(self):
        assert make_obj().triage_label() == "uncertain"


# ── to_dict ────────────────────────────────────────────────


class TestToDict:
    def test_rounding(self):
        obj = make_obj(
            market_pessimism=0.12345,
            business_strength=0.98765,
            divergence_score=0.55555,
            price_drawdown_12m=-0.33333,
            roe=0.123456,
            margin_change=0.056789,
            confidence=0.44444,
            thesis="t",
        )
        d = obj.to_dict()
        assert d["market_pessimism"] == 0.123
        assert d["business_strength"] == 0.988
        assert d["divergence_score"] == 0.556
        assert d["price_drawdown_12m"] == -0.333
        assert d["roe"] == 0.1235
        assert d["margin_change"] == 0.0568
        assert d["confidence"] == 0.444
        assert d["code"] == "600000"
        assert d["thesis"] == "t"

    def test_keys(self):
        d = make_obj().to_dict()
        assert set(d) == {
            "code",
            "trade_date",
            "market_pessimism",
            "business_strength",
            "divergence_score",
            "divergence_type",
            "price_drawdown_12m",
            "roe",
            "margin_change",
            "thesis",
            "confidence",
        }


# ── Detector composites (faked providers) ──────────────────


class FakeLocal:
    def __init__(self, closes):
        self._closes = closes

    def get_daily_kline(self, code, start, end):
        import pandas as pd

        return pd.DataFrame({"close": self._closes})


class FakeAkshare:
    def __init__(self, fin_by_date):
        self._fin = fin_by_date

    def get_financial_dict_vintage(self, code, date):
        return self._fin.get(date)


@pytest.fixture()
def detector(tmp_path):
    # _detect_anomaly touches both sqlite DBs via _compute_momentum_rank /
    # _compute_roe_stability; missing tables raise OperationalError (uncaught).
    cache = tmp_path / "cache.db"
    ev = tmp_path / "eval.db"
    c = sqlite3.connect(cache)
    c.execute(
        "CREATE TABLE akshare_financials (code TEXT, roe REAL, report_date TEXT, available_date TEXT)"
    )
    c.commit()
    c.close()
    e = sqlite3.connect(ev)
    e.execute(
        "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, "
        "momentum_percentile REAL, momentum_score REAL)"
    )
    e.commit()
    e.close()
    return MarketAnomalyDetector(cache_db=str(cache), eval_db=str(ev))


class TestDetectAnomalyComposite:
    def wire(self, det, closes, fin_now, fin_past):
        det.local = FakeLocal(closes)
        det.akshare = FakeAkshare(fin_now)

    def test_full_composite_math(self, detector):
        # drawdown: (8-10)/10 = -0.2 ; closes length >= 10
        closes = [10.0] * 9 + [8.0]
        fin_now = {"roe": 0.20, "debt_to_equity": 1.5, "pe_ttm": 10.0, "net_margin": 0.12, "fcf": 110.0}
        fin_past = {"pe_ttm": 20.0, "net_margin": 0.10, "fcf": 100.0}
        det = detector
        det.local = FakeLocal(closes)
        det.akshare = FakeAkshare({"2023-01-31": fin_now, "2022-02-01": fin_past, "2022-01-31": fin_past})

        obj = det._detect_anomaly("600000", "2023-01-31")
        assert obj is not None

        # drawdown = -0.2 -> pessimism_price = 0.2/0.5 = 0.4
        assert obj.price_drawdown_12m == pytest.approx(-0.2)
        # pe_compression = 10/20 = 0.5 -> pessimism_pe = 1-0.5 = 0.5
        assert obj.pe_compression == pytest.approx(0.5)
        # momentum: no snapshot table -> exception -> caught? _compute_momentum_rank
        # would raise on missing table; scan() catches, _detect_anomaly does not.
        # So we only reach here if eval.db has the table; this test seeds it below.
        expected_pessimism = 0.5 * 0.4 + 0.3 * 0.5 + 0.2 * 0.5
        assert obj.market_pessimism == pytest.approx(expected_pessimism)
        # strength_roe = min(1, 0.20/0.20) = 1.0
        # margin_change = 0.02 -> margin = 0.5+0.02*5 = 0.6
        # cashflow_trend = (110-100)/100 = 0.1 -> cashflow = 0.5+0.1*5 = 1.0
        # debt_health = 1 - 1.5/3 = 0.5
        # roe_stability: no akshare_financials table -> sqlite error propagates?
        # seeded below as empty -> 0.5
        expected_strength = 0.30 * 1.0 + 0.20 * 0.5 + 0.20 * 0.6 + 0.15 * 1.0 + 0.15 * 0.5
        assert obj.business_strength == pytest.approx(expected_strength)
        assert obj.divergence_score == pytest.approx(expected_strength - expected_pessimism)
        assert obj.confidence == pytest.approx(min(1.0, max(0, obj.divergence_score * 1.5)))

    def test_divergence_type_priority(self, detector):
        det = detector
        # market_overreaction branch wins when pessimism > 0.7 and strength > 0.5
        closes = [10.0] * 9 + [4.0]  # -60% drawdown -> pessimism_price = 1.0
        fin = {"roe": 0.25, "debt_to_equity": 0.0, "net_margin": 0.2, "fcf": 200.0}
        past = {"pe_ttm": 10.0, "net_margin": 0.1, "fcf": 100.0}
        det.local = FakeLocal(closes)
        det.akshare = FakeAkshare({"2023-01-31": fin, "2022-02-01": past, "2022-01-31": past})
        obj = det._detect_anomaly("600000", "2023-01-31")
        assert obj.market_pessimism > 0.7
        assert obj.divergence_type == "market_overreaction"

    def test_no_kline_returns_none(self, detector):
        det = detector
        det.local = FakeLocal([])  # empty -> len < 10
        det.akshare = FakeAkshare({})
        assert det._detect_anomaly("600000", "2023-01-31") is None

    def test_no_financials_returns_none(self, detector):
        det = detector
        det.local = FakeLocal([10.0] * 9 + [8.0])
        det.akshare = FakeAkshare({"2023-01-31": {}})  # no roe
        assert det._detect_anomaly("600000", "2023-01-31") is None


# ── sqlite-backed helpers (seeded tmp db) ──────────────────


@pytest.fixture()
def seeded_detector(tmp_path):
    cache = tmp_path / "cache.db"
    ev = tmp_path / "eval.db"
    cconn = sqlite3.connect(cache)
    cconn.execute(
        "CREATE TABLE akshare_financials (code TEXT, roe REAL, report_date TEXT, available_date TEXT)"
    )
    cconn.commit()
    cconn.close()
    econn = sqlite3.connect(ev)
    econn.execute(
        "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, "
        "momentum_percentile REAL, momentum_score REAL)"
    )
    econn.commit()
    econn.close()
    det = MarketAnomalyDetector(cache_db=str(cache), eval_db=str(ev))
    return det, str(cache), str(ev)


class TestRoeStability:
    def insert(self, cache_db, rows):
        conn = sqlite3.connect(cache_db)
        conn.executemany(
            "INSERT INTO akshare_financials VALUES (?,?,?,?)", rows
        )
        conn.commit()
        conn.close()

    def test_fewer_than_3_rows_neutral(self, seeded_detector):
        det, cache, _ = seeded_detector
        self.insert(cache, [("600000", 0.1, "2022-12-31", "2023-01-01")])
        assert det._compute_roe_stability("600000", "2023-01-31") == 0.5

    def test_stable_roe_high_stability(self, seeded_detector):
        det, cache, _ = seeded_detector
        rows = [
            ("600000", 0.100, "2022-12-31", "2023-01-01"),
            ("600000", 0.101, "2022-09-30", "2022-10-01"),
            ("600000", 0.099, "2022-06-30", "2022-07-01"),
        ]
        self.insert(cache, rows)
        val = det._compute_roe_stability("600000", "2023-01-31")
        assert val > 0.98  # CV tiny -> 1 - cv ~ 1

    def test_volatile_roe_low_stability(self, seeded_detector):
        det, cache, _ = seeded_detector
        rows = [
            ("600000", 0.30, "2022-12-31", "2023-01-01"),
            ("600000", 0.05, "2022-09-30", "2022-10-01"),
            ("600000", 0.20, "2022-06-30", "2022-07-01"),
        ]
        self.insert(cache, rows)
        val = det._compute_roe_stability("600000", "2023-01-31")
        assert val < 0.5

    def test_vintage_cutoff_respected(self, seeded_detector):
        det, cache, _ = seeded_detector
        # all rows available AFTER trade_date -> invisible -> 0.5
        rows = [
            ("600000", 0.30, "2023-12-31", "2024-01-01"),
            ("600000", 0.05, "2023-09-30", "2023-10-01"),
            ("600000", 0.20, "2023-06-30", "2023-07-01"),
        ]
        self.insert(cache, rows)
        assert det._compute_roe_stability("600000", "2023-01-31") == 0.5


class TestMomentumRank:
    def insert(self, ev, rows):
        conn = sqlite3.connect(ev)
        conn.executemany("INSERT INTO stock_factor_snapshot VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()

    def test_percentile_preferred(self, seeded_detector):
        det, _, ev = seeded_detector
        self.insert(ev, [("600000", "2023-01-31", 0.77, 55.0)])
        assert det._compute_momentum_rank("600000", "2023-01-31") == 0.77

    def test_score_fallback_divided_by_100(self, seeded_detector):
        det, _, ev = seeded_detector
        self.insert(ev, [("600000", "2023-01-31", None, 55.0)])
        assert det._compute_momentum_rank("600000", "2023-01-31") == 0.55

    def test_missing_row_default_05(self, seeded_detector):
        det, _, _ = seeded_detector
        assert det._compute_momentum_rank("999999", "2023-01-31") == 0.5


class TestGetUniverse:
    def test_suffix_stripped(self, seeded_detector):
        det, _, ev = seeded_detector
        conn = sqlite3.connect(ev)
        conn.executemany(
            "INSERT INTO stock_factor_snapshot VALUES (?,?,?,?)",
            [
                ("600000.SH", "2023-01-31", None, None),
                ("000001", "2023-01-31", None, None),
            ],
        )
        conn.commit()
        conn.close()
        assert det._get_universe("2023-01-31") == ["600000", "000001"]
