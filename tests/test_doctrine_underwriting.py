"""Characterization tests for DoctrineUnderwriter (6-S.2, FROZEN doctrine logic).

RECORD what the module currently does — not what it should do.
Note: where docstring and code disagree (e.g. contrarian fear threshold
0.6 in docstring vs 0.5 in code), these tests pin the CODE.

Covered surface:
  - unknown doctrine rejection
  - quality / contrarian / value: all 4 checks each, verdict bands, confidence
  - None-field handling in checks
  - underwrite_all keys, consensus bands, to_dict
"""

import pytest

from src.thesis.doctrine_underwriting import DoctrineUnderwriter, UnderwritingResult
from src.thesis.market_anomaly import MispricingObject


@pytest.fixture()
def uw():
    return DoctrineUnderwriter()


def make_anomaly(**kw) -> MispricingObject:
    base = dict(
        code="600000",
        trade_date="2023-01-31",
        price_drawdown_12m=-0.30,
        pe_compression=0.5,
        market_pessimism=0.7,
        roe=0.12,
        roe_stability=0.6,
        margin_change=0.02,
        debt_ratio=1.0,
        business_strength=0.65,
        divergence_score=0.35,
    )
    base.update(kw)
    return MispricingObject(**base)


class TestUnknownDoctrine:
    def test_reject(self, uw):
        r = uw.underwrite(make_anomaly(), "momentum_chaser")
        assert r.verdict == "REJECT"
        assert r.confidence == 0.0
        assert r.reasons == ["Unknown doctrine type"]
        assert r.doctrine_type == "momentum_chaser"


class TestQuality:
    def test_all_pass(self, uw):
        r = uw.underwrite(make_anomaly(), "quality_compounder")
        assert r.verdict == "PASS"
        assert r.confidence == 1.0
        assert len(r.reasons) == 4
        assert r.red_flags == []

    def test_verdict_bands(self, uw):
        # 2 passes -> CONDITIONAL ; confidence = 2/4
        a = make_anomaly(roe=0.01, margin_change=-0.5)  # Q1,Q2 fail
        r = uw.underwrite(a, "quality_compounder")
        assert r.verdict == "CONDITIONAL"
        assert r.confidence == 0.5
        # 1 pass -> REJECT
        a2 = make_anomaly(roe=0.01, margin_change=-0.5, debt_ratio=5.0)  # only Q4 passes
        r2 = uw.underwrite(a2, "quality_compounder")
        assert r2.verdict == "REJECT"
        assert r2.confidence == 0.25

    def test_thresholds(self, uw):
        assert (
            uw.underwrite(make_anomaly(roe=0.08), "quality_compounder").key_questions[
                "roe_healthy"
            ]["answer"]
            is False
        )  # strict >
        assert (
            uw.underwrite(make_anomaly(margin_change=-0.03), "quality_compounder").key_questions[
                "margin_stable"
            ]["answer"]
            is False
        )  # strict > -0.03
        assert (
            uw.underwrite(make_anomaly(debt_ratio=2.0), "quality_compounder").key_questions[
                "debt_manageable"
            ]["answer"]
            is False
        )  # strict <
        assert (
            uw.underwrite(make_anomaly(roe_stability=0.4), "quality_compounder").key_questions[
                "roe_stable"
            ]["answer"]
            is False
        )  # strict >

    def test_none_fields_fail_checks(self, uw):
        # margin_change None: check fails correctly via `is not None` guard...
        # but the red_flag f-string `{None:+.4f}` raises TypeError.
        # ACTUAL behavior (pinned): TypeError propagates out of underwrite.
        # Recorded as thaw candidate in docs/GUARDIAN_THAW_CANDIDATES.md.
        a = make_anomaly(margin_change=None)
        with pytest.raises(TypeError):
            uw.underwrite(a, "quality_compounder")

    def test_none_debt_also_typeerror(self, uw):
        a = make_anomaly(debt_ratio=None)
        with pytest.raises(TypeError):
            uw.underwrite(a, "quality_compounder")

    def test_none_roe_stability_also_typeerror(self, uw):
        a = make_anomaly(roe_stability=None)
        with pytest.raises(TypeError):
            uw.underwrite(a, "quality_compounder")


class TestContrarian:
    def test_all_pass(self, uw):
        r = uw.underwrite(make_anomaly(), "contrarian")
        assert r.verdict == "PASS"
        assert r.confidence == 1.0

    def test_thresholds_code_not_docstring(self, uw):
        # code: drawdown < -0.25
        assert (
            uw.underwrite(make_anomaly(price_drawdown_12m=-0.25), "contrarian").key_questions[
                "extreme_selloff"
            ]["answer"]
            is False
        )
        # code: pessimism > 0.5 (docstring says 0.6 — code wins)
        assert (
            uw.underwrite(make_anomaly(market_pessimism=0.55), "contrarian").key_questions[
                "market_fear"
            ]["answer"]
            is True
        )
        assert (
            uw.underwrite(make_anomaly(market_pessimism=0.5), "contrarian").key_questions[
                "market_fear"
            ]["answer"]
            is False
        )
        # code: strength > 0.5
        assert (
            uw.underwrite(make_anomaly(business_strength=0.5), "contrarian").key_questions[
                "fundamentals_intact"
            ]["answer"]
            is False
        )
        # code: divergence > 0.2
        assert (
            uw.underwrite(make_anomaly(divergence_score=0.2), "contrarian").key_questions[
                "clear_divergence"
            ]["answer"]
            is False
        )


class TestValue:
    def test_all_pass(self, uw):
        r = uw.underwrite(make_anomaly(), "value_purist")
        assert r.verdict == "PASS"
        assert r.confidence == 1.0

    def test_pe_none_red_flag(self, uw):
        r = uw.underwrite(make_anomaly(pe_compression=None), "value_purist")
        assert r.key_questions["pe_compressed"]["answer"] is False
        assert "PE compression unknown (no historical PE data)" in r.red_flags

    def test_thresholds(self, uw):
        assert (
            uw.underwrite(make_anomaly(pe_compression=0.7), "value_purist").key_questions[
                "pe_compressed"
            ]["answer"]
            is False
        )  # strict <
        assert (
            uw.underwrite(make_anomaly(roe=0.05), "value_purist").key_questions["still_profitable"][
                "answer"
            ]
            is False
        )  # strict >
        assert (
            uw.underwrite(make_anomaly(margin_change=-0.10), "value_purist").key_questions[
                "margin_ok"
            ]["answer"]
            is False
        )  # strict >


class TestCommittee:
    def test_underwrite_all_keys(self, uw):
        out = uw.underwrite_all(make_anomaly())
        assert set(out) == {"quality_compounder", "contrarian", "value_purist"}
        assert all(isinstance(v, UnderwritingResult) for v in out.values())

    def test_consensus_pass(self, uw):
        results = uw.underwrite_all(make_anomaly())  # all PASS
        c = uw.consensus(results)
        assert c["consensus"] == "PASS"
        assert c["pass_count"] == 3
        assert c["avg_confidence"] == 1.0
        assert c["conditional_count"] == 0

    def test_consensus_reject(self, uw):
        bad = make_anomaly(
            roe=0.0,
            margin_change=-0.9,
            debt_ratio=9.0,
            roe_stability=0.0,
            price_drawdown_12m=0.0,
            market_pessimism=0.0,
            business_strength=0.0,
            divergence_score=0.0,
            pe_compression=1.5,
        )
        results = uw.underwrite_all(bad)
        c = uw.consensus(results)
        assert c["consensus"] == "REJECT"
        assert c["reject_count"] == 3

    def test_consensus_split(self, uw):
        results = {
            "a": UnderwritingResult("a", "PASS", 0.8, [], [], {}),
            "b": UnderwritingResult("b", "REJECT", 0.6, [], [], {}),
            "c": UnderwritingResult("c", "CONDITIONAL", 0.4, [], [], {}),
        }
        c = uw.consensus(results)
        assert c["consensus"] == "SPLIT"
        assert c["conditional_count"] == 1
        assert c["avg_confidence"] == pytest.approx(0.6)

    def test_to_dict(self, uw):
        r = uw.underwrite(make_anomaly(), "quality_compounder")
        d = r.to_dict()
        assert d["doctrine"] == "quality_compounder"
        assert d["verdict"] == "PASS"
        assert d["confidence"] == 1.0
        assert "questions" in d and "red_flags" in d and "reasons" in d
