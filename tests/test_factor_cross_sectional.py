"""Factor cross-sectional normalization tests.

The factor engine used to score each family from the stock's *raw* factor value
(via a naive `raw*100 if abs(raw)<=1` scaling) and never fed the cross-sectional
percentile/z_score (computed by `compute_cross_sectional`) back into the scores.
That made the composite scores scale-sensitive and not comparable across the
universe. These tests pin the corrected behaviour.
"""
import math
import sys
import types

# ── Offline stubs for pandas/numpy (engine imports them at module top) ──
if "pandas" not in sys.modules:
    pd = types.ModuleType("pandas")
    pd.isna = lambda x: x is None or (isinstance(x, float) and math.isnan(x))
    pd.DataFrame = object  # only referenced in type hints
    sys.modules["pandas"] = pd

if "numpy" not in sys.modules:
    np = types.ModuleType("numpy")

    class _Arr:
        def __init__(self, data):
            self.data = list(data)

        def mean(self):
            return sum(self.data) / len(self.data) if self.data else 0.0

        def std(self):
            if len(self.data) < 2:
                return 0.0
            m = self.mean()
            return (sum((x - m) ** 2 for x in self.data) / len(self.data)) ** 0.5

        def __lt__(self, other):
            return _BoolArr([x < other for x in self.data])

    class _BoolArr:
        def __init__(self, data):
            self.data = [bool(x) for x in data]

        def mean(self):
            return sum(self.data) / len(self.data) if self.data else 0.0

    np.array = lambda lst, *a, **k: _Arr(lst)
    np.mean = lambda x, *a, **k: _Arr(x).mean() if hasattr(x, "__iter__") else x
    sys.modules["numpy"] = np

from src.factors.engine import FactorEngine, FactorResult, CompositeResult


def _stock(code, roe, pe, rsi=None):
    """Build a minimal CompositeResult with quality(higher) + value(lower) factors."""
    facs = [
        FactorResult(name="roe", family="quality", raw_value=roe),
        FactorResult(name="pe_ttm", family="value", raw_value=pe),
    ]
    if rsi is not None:
        facs.append(FactorResult(name="rsi_14", family="sentiment", raw_value=rsi))
    return CompositeResult(code=code, date="2026-01-01", factors=facs)


def test_cross_sectional_ranks_higher_better():
    """Higher ROE → higher quality_score, ranked against the universe."""
    fe = FactorEngine()
    aaa, bbb, ccc = _stock("AAA", 0.30, 10), _stock("BBB", 0.15, 20), _stock("CCC", 0.05, 50)
    fe.compute_cross_sectional([aaa, bbb, ccc])

    assert aaa.quality_score > bbb.quality_score > ccc.quality_score
    assert 0.0 <= ccc.quality_score <= 100.0
    # percentile = fraction of stocks with a *strictly* lower raw value
    assert abs(aaa.factors[0].percentile - 2 / 3) < 1e-9
    assert abs(bbb.factors[0].percentile - 1 / 3) < 1e-9
    assert aaa.factors[0].percentile == 0.0 or ccc.factors[0].percentile == 0.0


def test_cross_sectional_ranks_lower_better():
    """Lower PE → higher value_score (direction-aware)."""
    fe = FactorEngine()
    aaa, bbb, ccc = _stock("AAA", 0.30, 10), _stock("BBB", 0.15, 20), _stock("CCC", 0.05, 50)
    fe.compute_cross_sectional([aaa, bbb, ccc])

    assert aaa.value_score > bbb.value_score > ccc.value_score
    # AAA has the lowest PE (10) among [10, 20, 50] → best value score
    assert aaa.value_score == 100.0


def test_neutral_factor_contributes_flat_50():
    """A neutral-direction factor carries no rank → flat 50 mid-score."""
    fe = FactorEngine()
    a, b = _stock("A", 0.30, 10, rsi=80), _stock("B", 0.05, 50, rsi=20)
    fe.compute_cross_sectional([a, b])

    assert a.sentiment_score == 50.0
    assert b.sentiment_score == 50.0


def test_single_stock_not_overwritten():
    """With <2 stocks there is no cross-section; fallback scores stay intact."""
    fe = FactorEngine()
    one = _stock("SOLO", 0.20, 15)
    out = fe.compute_cross_sectional([one])
    assert out[0].quality_score == 50.0  # left as single-stock default, no crash


def test_compute_universe_wires_two_passes():
    """compute_universe runs compute_single_stock per stock then normalizes."""
    fe = FactorEngine()
    seen = []
    fe.compute_single_stock = lambda code, financial_data, price_data, market_data=None: (
        seen.append(code) or _stock(code, financial_data["roe"], financial_data["pe_ttm"])
    )
    inputs = [
        {"code": "AAA", "financial_data": {"roe": 0.30, "pe_ttm": 10}, "price_data": None},
        {"code": "BBB", "financial_data": {"roe": 0.15, "pe_ttm": 20}, "price_data": None},
    ]
    out = fe.compute_universe(inputs)

    assert seen == ["AAA", "BBB"]                     # per-stock pass ran
    assert out[0].quality_score > out[1].quality_score  # cross-section applied
