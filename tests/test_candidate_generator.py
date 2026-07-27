"""Characterization tests for CandidateGenerator (6-S.13).

RECORD what the module currently does — not what it should do.

Covered surface:
  - stage thresholds
  - _get_snapshot_universe (frozen contract: stock_factor_snapshot)
  - _batch_get_volume_ratio: json parsing, chunking
  - _compute_recovery_score / _enrich_recovery_score formulas
  - Stage 1: liquidity gate, FRM hard gate, feature population
  - Stage 2: no hard gate (v3.3), EGE -> recovery_score mapping
  - Stage 3: anomaly as last gate, divergence ranking, top_n
  - funnel log buffer/update/flush
  - generate(): empty universe, full funnel integration

Sub-scorers (FRM/RS/EGE/anomaly) are replaced with fakes; the funnel
logic itself runs against real temp SQLite DBs.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.thesis.candidate_generator import (
    FRM_HARD_REJECT_DIRECTION,
    LIQUIDITY_MIN_VOLUME_RATIO,
    RS_HARD_GATE_THRESHOLD,
    CandidateGenerator,
)
from src.thesis.market_anomaly import MispricingObject, V3CandidateFeatures

SNAP_SCHEMA = (
    "CREATE TABLE stock_factor_snapshot (security_id TEXT, trade_date TEXT, "
    "factor_values_json TEXT)"
)
FUNNEL_SCHEMA = """
CREATE TABLE shadow_funnel_log (
    episode_id TEXT, trade_date TEXT, stock_code TEXT,
    stage1_liquidity_pass INTEGER, stage1_volume_ratio REAL,
    stage1_frm_direction TEXT, stage1_frm_score REAL,
    stage1_earnings_accel REAL, stage1_recovery_score REAL,
    stage1_pass INTEGER,
    stage2_rs_vs_sector REAL, stage2_sector_vs_market REAL,
    stage2_rs_score REAL, stage2_data_available INTEGER, stage2_pass INTEGER,
    stage3_divergence_score REAL, stage3_pass INTEGER,
    final_pass INTEGER, rejection_stage TEXT, rejection_reason TEXT
)
"""

DATE = "2024-08-29"


class FakeFRM:
    def __init__(self, results=None, default=None):
        self.results = results or {}
        self.default = default or SimpleNamespace(
            revision_direction="improving",
            score=70.0,
            earnings_yoy_current=0.20,
            earnings_yoy_previous=0.10,
            earnings_acceleration=80.0,
            margin_stabilization=60.0,
        )

    def compute(self, code, trade_date, market_state):
        return self.results.get(code, self.default)


class FakeRS:
    def compute(self, code, trade_date):
        return SimpleNamespace(
            rs_vs_sector=0.05, sector_vs_market=0.02, score=65.0, data_available=True
        )


class FakeEGE:
    def __init__(self, gaps=None):
        self.gaps = gaps or {}

    def compute(self, code, trade_date):
        return SimpleNamespace(gap_score=self.gaps.get(code, 0.4))


class FakeAnomaly:
    def __init__(self, anomalies=None):
        self.anomalies = anomalies or {}

    def _detect_anomaly(self, code, trade_date):
        return self.anomalies.get(code)


@pytest.fixture()
def gen(tmp_path):
    eval_db = tmp_path / "evaluation.db"
    shadow_db = tmp_path / "shadow_trading.db"
    cache_db = tmp_path / "cache.db"
    conn = sqlite3.connect(eval_db)
    conn.execute(SNAP_SCHEMA)
    conn.commit()
    conn.close()
    conn = sqlite3.connect(shadow_db)
    conn.execute(FUNNEL_SCHEMA)
    conn.commit()
    conn.close()
    cache_db.touch()

    g = CandidateGenerator(
        cache_db=str(cache_db), eval_db=str(eval_db), shadow_db=str(shadow_db)
    )
    g.frm_scorer = FakeFRM()
    g.rs_scorer = FakeRS()
    g.ege_engine = FakeEGE()
    g.anomaly_detector = FakeAnomaly()
    return g, str(eval_db), str(shadow_db)


def insert_snapshot(eval_db, rows):
    conn = sqlite3.connect(eval_db)
    conn.executemany("INSERT INTO stock_factor_snapshot VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def snap_row(code, volume_ratio=None, date=DATE):
    fj = json.dumps({"volume_ratio": volume_ratio}) if volume_ratio is not None else None
    return (code, date, fj)


def funnel_rows(shadow_db):
    conn = sqlite3.connect(shadow_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM shadow_funnel_log").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def feat(frm_score=70.0):
    f = V3CandidateFeatures()
    f.frm_score = frm_score
    return f


class TestConstants:
    def test_thresholds(self):
        assert LIQUIDITY_MIN_VOLUME_RATIO == 0.3
        assert FRM_HARD_REJECT_DIRECTION == "deteriorating"
        assert RS_HARD_GATE_THRESHOLD == 0.0


class TestSnapshotUniverse:
    def test_distinct_and_nonnull(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [
            ("AAA", DATE, None),
            ("AAA", DATE, None),  # duplicate
            ("BBB", DATE, None),
            (None, DATE, None),  # falsy filtered
            ("CCC", "2024-08-28", None),  # other date
        ])
        assert sorted(g._get_snapshot_universe(DATE)) == ["AAA", "BBB"]

    def test_empty(self, gen):
        g, _, _ = gen
        assert g._get_snapshot_universe(DATE) == []


class TestBatchVolumeRatio:
    def test_parses_json(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.75)])
        assert g._batch_get_volume_ratio(["AAA"], DATE) == {"AAA": 0.75}

    def test_skips_invalid_and_missing(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [
            ("AAA", DATE, "{not json"),
            ("BBB", DATE, json.dumps({"other": 1})),
            ("CCC", DATE, None),
            snap_row("DDD", 0.5),
        ])
        assert g._batch_get_volume_ratio(["AAA", "BBB", "CCC", "DDD"], DATE) == {
            "DDD": 0.5
        }

    def test_empty_universe(self, gen):
        g, _, _ = gen
        assert g._batch_get_volume_ratio([], DATE) == {}

    def test_chunking_over_500(self, gen):
        g, eval_db, _ = gen
        codes = [f"{i:06d}" for i in range(501)]
        insert_snapshot(eval_db, [snap_row(c, 0.5) for c in codes])
        result = g._batch_get_volume_ratio(codes, DATE)
        assert len(result) == 501


class TestRecoveryScoreFormulas:
    def test_stage1_formula(self, gen):
        g, _, _ = gen
        frm = SimpleNamespace(earnings_acceleration=80.0, margin_stabilization=60.0)
        # 0.35*80 + 0.25*60 + 0.40*50 = 28 + 15 + 20
        assert g._compute_recovery_score(frm) == pytest.approx(63.0)

    def test_stage1_formula_extremes(self, gen):
        g, _, _ = gen
        hi = SimpleNamespace(earnings_acceleration=100.0, margin_stabilization=100.0)
        lo = SimpleNamespace(earnings_acceleration=0.0, margin_stabilization=0.0)
        # QUIRK (pinned): with 0-100 subscores the result range is [20, 80];
        # the 0-100 clamp never actually triggers
        assert g._compute_recovery_score(hi) == pytest.approx(80.0)
        assert g._compute_recovery_score(lo) == pytest.approx(20.0)

    def test_enrich_formula(self, gen):
        g, _, _ = gen
        rs = SimpleNamespace(score=50.0)
        # 0.60*80 + 0.40*50 = 48 + 20
        assert g._enrich_recovery_score(feat(frm_score=80.0), rs) == pytest.approx(68.0)

    def test_enrich_none_defaults_50(self, gen):
        g, _, _ = gen
        f = feat(frm_score=None)
        rs = SimpleNamespace(score=None)
        # (50)*0.60 + (50)*0.40
        assert g._enrich_recovery_score(f, rs) == pytest.approx(50.0)


class TestStage1:
    def test_liquidity_gate_fails(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.2)])
        passed = g._stage1_recovery_eligibility(["AAA"], DATE, "EARLY_RECOVERY", None)
        assert passed == []

    def test_liquidity_boundary_strict(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.3), snap_row("BBB", 0.31)])
        passed = g._stage1_recovery_eligibility(["AAA", "BBB"], DATE, "x", None)
        assert [c for c, _ in passed] == ["BBB"]  # 0.3 fails (strict >)

    def test_missing_volume_ratio_fails(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [("AAA", DATE, json.dumps({"other": 1}))])
        passed = g._stage1_recovery_eligibility(["AAA"], DATE, "x", None)
        assert passed == []

    def test_deteriorating_hard_reject(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.5)])
        g.frm_scorer = FakeFRM(results={
            "AAA": SimpleNamespace(
                revision_direction="deteriorating", score=20.0,
                earnings_yoy_current=-0.10, earnings_yoy_previous=0.05,
                earnings_acceleration=10.0, margin_stabilization=30.0,
            )
        })
        passed = g._stage1_recovery_eligibility(["AAA"], DATE, "x", "EP1")
        assert passed == []
        entry = g._funnel_log_buffer[0]
        assert entry["rejection_reason"] == "DETERIORATING"
        assert entry["rejection_stage"] == "stage1"
        assert entry["stage1_liquidity_pass"] == 1

    def test_pass_populates_features(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.5)])
        passed = g._stage1_recovery_eligibility(["AAA"], DATE, "x", None)
        assert len(passed) == 1
        code, f = passed[0]
        assert code == "AAA"
        assert f.liquidity_pass is True
        assert f.frm_direction == "improving"
        assert f.frm_score == 70.0
        assert f.earnings_acceleration == pytest.approx(0.10)  # 0.20 - 0.10
        assert f.recovery_score == pytest.approx(63.0)  # stage1 formula
        assert f.candidate_stage == "stage1_pass"

    def test_earnings_acceleration_none_when_yoy_none(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.5)])
        g.frm_scorer = FakeFRM(default=SimpleNamespace(
            revision_direction="stable", score=50.0,
            earnings_yoy_current=None, earnings_yoy_previous=0.05,
            earnings_acceleration=50.0, margin_stabilization=50.0,
        ))
        _, f = g._stage1_recovery_eligibility(["AAA"], DATE, "x", None)[0]
        assert f.earnings_acceleration is None

    def test_funnel_log_low_liquidity(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.1)])
        g._stage1_recovery_eligibility(["AAA"], DATE, "x", "EP1")
        entry = g._funnel_log_buffer[0]
        assert entry["episode_id"] == "EP1"
        assert entry["stock_code"] == "AAA"
        assert entry["stage1_pass"] == 0
        assert entry["rejection_reason"] == "LOW_LIQUIDITY"
        assert entry["stage1_volume_ratio"] == 0.1

    def test_no_episode_no_buffer(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.5)])
        g._stage1_recovery_eligibility(["AAA"], DATE, "x", None)
        assert g._funnel_log_buffer == []


class TestStage2:
    def _stage1_pair(self):
        return [("AAA", feat())]

    def test_no_hard_gate_quirk(self, gen):
        """QUIRK (pinned): RS_HARD_GATE_THRESHOLD=0.0 is defined but UNUSED
        since v3.3 — even deeply negative rs_vs_sector passes Stage 2."""
        g, _, _ = gen
        g.rs_scorer = FakeRS()
        g.rs_scorer.compute = lambda code, td: SimpleNamespace(
            rs_vs_sector=-0.50, sector_vs_market=-0.30, score=10.0, data_available=True
        )
        passed = g._stage2_relative_strength(self._stage1_pair(), DATE, None)
        assert len(passed) == 1
        assert passed[0][1].relative_strength == -0.50

    def test_ege_maps_to_recovery_score(self, gen):
        g, _, _ = gen
        g.ege_engine = FakeEGE(gaps={"AAA": 0.4})
        _, f = g._stage2_relative_strength(self._stage1_pair(), DATE, None)[0]
        assert f.recovery_score == pytest.approx(70.0)  # 0.4*50 + 50

    def test_ege_none_neutral_50(self, gen):
        g, _, _ = gen
        g.ege_engine = FakeEGE(gaps={"AAA": None})
        _, f = g._stage2_relative_strength(self._stage1_pair(), DATE, None)[0]
        assert f.recovery_score == 50.0

    def test_features_and_stage_marker(self, gen):
        g, _, _ = gen
        _, f = g._stage2_relative_strength(self._stage1_pair(), DATE, None)[0]
        assert f.relative_strength == 0.05
        assert f.sector_strength == 0.02
        assert f.rs_score == 65.0
        assert f.candidate_stage == "stage2_pass"

    def test_update_log_creates_entry_when_missing(self, gen):
        """QUIRK (pinned): _update_funnel_log falls back to creating a new
        entry when no stage1 entry exists for the episode+code."""
        g, _, _ = gen
        g._stage2_relative_strength(self._stage1_pair(), DATE, "EP1")
        assert len(g._funnel_log_buffer) == 1
        entry = g._funnel_log_buffer[0]
        assert entry["stage2_pass"] == 1
        assert entry["stage2_rs_vs_sector"] == 0.05
        assert "stage1_pass" not in entry  # new entry has no stage1 fields


class TestStage3:
    def _pairs(self, codes):
        return [(c, feat()) for c in codes]

    def test_anomaly_none_rejected(self, gen):
        g, _, _ = gen
        g.anomaly_detector = FakeAnomaly()
        out = g._stage3_mispricing(self._pairs(["AAA"]), DATE, 50, "EP1")
        assert out == []
        entry = g._funnel_log_buffer[0]
        assert entry["stage3_pass"] == 0
        assert entry["rejection_reason"] == "NO_MISPRICING"

    def test_anomaly_attached_and_marked(self, gen):
        g, _, _ = gen
        anomaly = MispricingObject(code="AAA", trade_date=DATE, divergence_score=0.42)
        g.anomaly_detector = FakeAnomaly({"AAA": anomaly})
        out = g._stage3_mispricing(self._pairs(["AAA"]), DATE, 50, "EP1")
        assert out == [anomaly]
        assert anomaly.v3_features is not None
        assert anomaly.v3_features.candidate_stage == "stage3_pass"
        entry = g._funnel_log_buffer[0]
        assert entry["stage3_pass"] == 1
        assert entry["final_pass"] == 1
        assert entry["stage3_divergence_score"] == 0.42

    def test_sorted_by_divergence_desc_and_top_n(self, gen):
        g, _, _ = gen
        anomalies = {
            "AAA": MispricingObject(code="AAA", trade_date=DATE, divergence_score=0.1),
            "BBB": MispricingObject(code="BBB", trade_date=DATE, divergence_score=0.9),
            "CCC": MispricingObject(code="CCC", trade_date=DATE, divergence_score=0.5),
        }
        g.anomaly_detector = FakeAnomaly(anomalies)
        out = g._stage3_mispricing(self._pairs(["AAA", "BBB", "CCC"]), DATE, 2, None)
        assert [a.code for a in out] == ["BBB", "CCC"]  # desc, top_n=2


class TestFunnelLog:
    def test_update_matches_episode_and_code(self, gen):
        g, _, _ = gen
        g._buffer_funnel_log("EP1", DATE, "AAA", stage1_pass=1)
        g._buffer_funnel_log("EP1", DATE, "BBB", stage1_pass=1)
        g._update_funnel_log("EP1", DATE, "BBB", stage2_pass=1)
        assert g._funnel_log_buffer[0].get("stage2_pass") is None
        assert g._funnel_log_buffer[1]["stage2_pass"] == 1

    def test_flush_inserts_and_clears(self, gen):
        g, _, shadow_db = gen
        g._buffer_funnel_log(
            "EP1", DATE, "AAA", stage1_pass=1, stage1_liquidity_pass=1,
            stage1_volume_ratio=0.5, rejection_stage=None,
        )
        g._flush_funnel_log()
        assert g._funnel_log_buffer == []
        rows = funnel_rows(shadow_db)
        assert len(rows) == 1
        assert rows[0]["stock_code"] == "AAA"
        assert rows[0]["stage1_pass"] == 1
        assert rows[0]["stage1_volume_ratio"] == 0.5
        assert rows[0]["rejection_stage"] is None

    def test_flush_empty_buffer_noop(self, gen):
        g, _, shadow_db = gen
        g._flush_funnel_log()
        assert funnel_rows(shadow_db) == []


class TestGenerate:
    def test_empty_universe_returns_empty(self, gen):
        g, _, _ = gen
        assert g.generate(DATE, "EARLY_RECOVERY", universe=[]) == []

    def test_universe_none_uses_snapshot(self, gen):
        g, eval_db, _ = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.5)])
        g.anomaly_detector = FakeAnomaly({
            "AAA": MispricingObject(code="AAA", trade_date=DATE, divergence_score=0.3)
        })
        out = g.generate(DATE, "EARLY_RECOVERY")
        assert [a.code for a in out] == ["AAA"]

    def test_full_funnel_integration(self, gen):
        g, eval_db, shadow_db = gen
        insert_snapshot(eval_db, [
            snap_row("ILLIQUID", 0.1),
            snap_row("DETERIORATING_CO", 0.5),
            snap_row("WINNER", 0.5),
        ])
        g.frm_scorer = FakeFRM(results={
            "DETERIORATING_CO": SimpleNamespace(
                revision_direction="deteriorating", score=20.0,
                earnings_yoy_current=-0.1, earnings_yoy_previous=0.0,
                earnings_acceleration=10.0, margin_stabilization=30.0,
            )
        })
        g.anomaly_detector = FakeAnomaly({
            "WINNER": MispricingObject(code="WINNER", trade_date=DATE,
                                       divergence_score=0.7)
        })
        out = g.generate(DATE, "EARLY_RECOVERY", episode_id="EP9")
        assert [a.code for a in out] == ["WINNER"]
        assert out[0].v3_features.candidate_stage == "stage3_pass"

        rows = funnel_rows(shadow_db)
        by_code = {r["stock_code"]: r for r in rows}
        assert by_code["ILLIQUID"]["rejection_reason"] == "LOW_LIQUIDITY"
        assert by_code["DETERIORATING_CO"]["rejection_reason"] == "DETERIORATING"
        assert by_code["WINNER"]["final_pass"] == 1
        assert g._funnel_log_buffer == []  # flushed

    def test_no_episode_no_log_writes(self, gen):
        g, eval_db, shadow_db = gen
        insert_snapshot(eval_db, [snap_row("AAA", 0.5)])
        g.anomaly_detector = FakeAnomaly({
            "AAA": MispricingObject(code="AAA", trade_date=DATE, divergence_score=0.3)
        })
        g.generate(DATE, "x")
        assert funnel_rows(shadow_db) == []
