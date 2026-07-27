"""Characterization tests for ThesisValidator (6-Q.3).

RECORD what the module currently does — not what it should do.

Covered surface:
  - validate: earnings-change math, defaults, correctness, accuracy boost
  - validate_batch: suffix strip, signal extraction, pick_returns bounds
  - compute_thesis_accuracy: empty neutral, mean of scores
  - _get_financials_at: nearest-report lookup (seeded tmp sqlite)
"""

import sqlite3

import pytest

from src.thesis.thesis_validator import ThesisValidator

SCHEMA = "CREATE TABLE akshare_financials (code TEXT, report_date TEXT, earnings_yoy REAL)"


@pytest.fixture()
def validator(tmp_path):
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    return ThesisValidator(cache_db=str(db)), str(db)


def insert(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO akshare_financials VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


class TestValidate:
    def test_earnings_change_math(self, validator):
        tv, db = validator
        insert(
            db,
            [
                ("600519", "2026-03-31", 10.0),
                ("600519", "2026-06-30", 15.0),
            ],
        )
        v = tv.validate("600519", "2026-05-27", "2026-08-01", actual_price_change=0.1)
        assert v.actual_earnings_change == pytest.approx((15.0 - 10.0) / 100.0)

    def test_missing_financials_zero_change(self, validator):
        tv, _ = validator
        v = tv.validate("000001", "2026-05-27", "2026-08-01", actual_price_change=0.1)
        assert v.actual_earnings_change == 0.0

    def test_predicted_acceleration_defaults_true(self, validator):
        tv, _ = validator
        v = tv.validate("000001", "2026-05-27", "2026-08-01", actual_price_change=0.0)
        assert v.predicted_acceleration is True

    def test_correctness_matches_reality(self, validator):
        tv, db = validator
        insert(db, [("600519", "2026-03-31", 10.0), ("600519", "2026-06-30", 15.0)])
        # actual acceleration happened (delta > 0)
        v_true = tv.validate(
            "600519",
            "2026-05-27",
            "2026-08-01",
            predicted_acceleration=True,
            actual_price_change=0.0,
        )
        assert v_true.thesis_correct is True
        assert v_true.accuracy_score == 1.0
        v_false = tv.validate(
            "600519",
            "2026-05-27",
            "2026-08-01",
            predicted_acceleration=False,
            actual_price_change=0.0,
        )
        assert v_false.thesis_correct is False
        assert v_false.accuracy_score == 0.0

    def test_mispricing_boost(self, validator):
        tv, db = validator
        insert(db, [("600519", "2026-03-31", 10.0), ("600519", "2026-06-30", 15.0)])
        # correct + predicted_mispricing + earnings>0 + price>0 -> +0.2 capped at 1.0
        v = tv.validate(
            "600519",
            "2026-05-27",
            "2026-08-01",
            predicted_acceleration=True,
            predicted_mispricing=True,
            actual_price_change=0.1,
        )
        assert v.accuracy_score == 1.0  # min(1.0, 1.0+0.2)
        # boost does NOT apply when price <= 0
        v2 = tv.validate(
            "600519",
            "2026-05-27",
            "2026-08-01",
            predicted_acceleration=False,
            predicted_mispricing=True,
            actual_price_change=-0.1,
        )
        assert v2.accuracy_score == 0.0

    def test_predicted_mispricing_none_becomes_false(self, validator):
        tv, _ = validator
        v = tv.validate("000001", "2026-05-27", "2026-08-01", actual_price_change=0.0)
        assert v.predicted_mispricing is False

    def test_code_zfilled(self, validator):
        tv, _ = validator
        v = tv.validate(519, "2026-05-27", "2026-08-01", actual_price_change=0.0)
        assert v.code == "000519"


class TestValidateBatch:
    def test_suffix_strip_and_signal_extraction(self, validator):
        tv, db = validator
        insert(db, [("600519", "2026-03-31", 10.0), ("600519", "2026-06-30", 15.0)])
        picks = [
            {
                "security_id": "600519.SH",
                "thesis_signals": {"fundamental_acceleration": 0.2, "mispricing_gap": 0.1},
            }
        ]
        out = tv.validate_batch(picks, "2026-05-27", "2026-08-01", pick_returns=[0.05])
        assert list(out) == ["600519"]
        v = out["600519"]
        assert v.predicted_acceleration is True
        assert v.predicted_mispricing is True
        assert v.actual_price_change == 0.05

    def test_pick_returns_out_of_bounds(self, validator, monkeypatch):
        tv, _ = validator
        # empty pick_returns -> price_ret=None -> falls back to _get_price_return
        monkeypatch.setattr(tv, "_get_price_return", lambda *a: -0.123)
        picks = [{"security_id": "000001", "thesis_signals": {}}]
        out = tv.validate_batch(picks, "2026-05-27", "2026-08-01", pick_returns=[])
        assert out["000001"].actual_price_change == -0.123
        # shorter list than picks -> second pick also falls back
        picks2 = [
            {"security_id": "000001", "thesis_signals": {}},
            {"security_id": "000002", "thesis_signals": {}},
        ]
        out2 = tv.validate_batch(picks2, "2026-05-27", "2026-08-01", pick_returns=[0.05])
        assert out2["000001"].actual_price_change == 0.05
        assert out2["000002"].actual_price_change == -0.123

    def test_default_signals_not_positive(self, validator):
        tv, _ = validator
        picks = [{"security_id": "000001"}]  # no thesis_signals key
        out = tv.validate_batch(picks, "2026-05-27", "2026-08-01", pick_returns=[0.0])
        assert out["000001"].predicted_acceleration is False
        assert out["000001"].predicted_mispricing is False


class TestThesisAccuracy:
    def test_empty_neutral(self, validator):
        tv, _ = validator
        assert tv.compute_thesis_accuracy({}) == 0.5

    def test_mean_of_scores(self, validator):
        tv, db = validator
        insert(db, [("600519", "2026-03-31", 10.0), ("600519", "2026-06-30", 15.0)])
        picks = [
            {"security_id": "600519", "thesis_signals": {"fundamental_acceleration": 0.2}},
            {"security_id": "000001", "thesis_signals": {"fundamental_acceleration": 0.2}},
        ]
        out = tv.validate_batch(picks, "2026-05-27", "2026-08-01", pick_returns=[0.0, 0.0])
        # 600519: predicted True, actual True -> 1.0 ; 000001: no data -> actual False -> 0.0
        assert tv.compute_thesis_accuracy(out) == pytest.approx(0.5)


class TestGetFinancialsAt:
    def test_nearest_prior_report(self, validator):
        tv, db = validator
        insert(
            db,
            [
                ("600519", "2026-03-31", 10.0),
                ("600519", "2026-06-30", 15.0),
            ],
        )
        # eval date between reports -> picks 03-31
        fin = tv._get_financials_at("600519", "2026-05-27")
        assert fin["earnings_yoy"] == 10.0
        # eval date after latest -> picks 06-30
        fin2 = tv._get_financials_at("600519", "2026-08-01")
        assert fin2["earnings_yoy"] == 15.0

    def test_no_report_returns_none(self, validator):
        tv, _ = validator
        assert tv._get_financials_at("000001", "2026-01-01") is None
