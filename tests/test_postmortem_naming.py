"""Resolve the historical double-``PostMortemEngine`` name collision.

Two different classes previously shared the name ``PostMortemEngine``:
  * ``src.evaluation.post_mortem.PostMortemEngine`` -- a single-decision
    analyzer (5-category / 12-subtype tree -> PostMortemResult + mutations).
  * ``src.postmortem.engine.PostMortemEngine`` -- the daily batch
    orchestrator (consumes evaluation_results -> candidate_rules_v2, the local
    postmortem DB table — distinct from EvaluationDB's candidate_rules table).

Sharing a name caused an import-shadowing hazard (runner.py imported one at
module level but used the other at runtime). The single-decision analyzer is
now renamed ``PostMortemAnalyzer``; the daily orchestrator keeps
``PostMortemEngine``.
"""

import sys
import types


def _install_stubs():
    """Make pandas/numpy importable in dependency-free environments."""
    if "pandas" not in sys.modules:
        pd = types.ModuleType("pandas")

        class _DF:
            empty = None

            def __init__(self, *a, **k):
                pass

            def iterrows(self):
                return iter([])

            def to_dict(self, *a, **k):
                return {}

        pd.DataFrame = _DF
        pd.read_sql_query = lambda *a, **k: _DF()
        sys.modules["pandas"] = pd
    if "numpy" not in sys.modules:
        sys.modules["numpy"] = types.ModuleType("numpy")


def test_two_postmortem_classes_are_distinct():
    _install_stubs()
    from src.evaluation.post_mortem import PostMortemAnalyzer
    from src.postmortem.engine import PostMortemEngine

    # Different classes, no more name collision.
    assert PostMortemAnalyzer is not PostMortemEngine

    # Analyzer: single-decision analysis API.
    assert hasattr(PostMortemAnalyzer, "run")
    assert hasattr(PostMortemAnalyzer, "run_batch")

    # Engine: daily batch orchestration API.
    assert hasattr(PostMortemEngine, "run_daily")


def test_evaluation_post_mortem_exports_analyzer_only():
    _install_stubs()
    from src.evaluation import post_mortem

    assert hasattr(post_mortem, "PostMortemAnalyzer")
    assert not hasattr(post_mortem, "PostMortemEngine")
