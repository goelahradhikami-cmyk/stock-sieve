"""Failure → mutation → evolution closed-loop wiring.

Verifies the two ends of the loop added in the "connect the loop" batch:

  1. PostMortemEngine now runs the rich ``PostMortemAnalyzer`` on each failed
     evaluation and persists the result (including ``mutation_candidates``) to
     the ``post_mortem_analysis`` table; collection dedupes by (type, target, filter).
  2. EvolutionEngineV1 ingests those mutations: ``_apply_post_mortem_mutations``
     maps each semantic ``target`` to a small nudge of a genome
     ``investment_identity`` dimension, ignoring unknown targets; ``_mutate``
     and ``run_cycle`` accept the new ``pending_mutations`` kwarg (default
     ``None`` keeps historical behavior identical).
"""

import unittest
import tempfile
import os

from src.postmortem.engine import PostMortemEngine
from src.evaluation.post_mortem import (
    PostMortemResult,
    ErrorCategory,
    ErrorSubtype,
)
from src.evolution.engine_v1 import EvolutionEngineV1
import contextlib


class TestPostMortemPersistence(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.pm = PostMortemEngine(db_path=self.db_path)

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.pm.db.close()
        if os.path.exists(self.db_path):
            with contextlib.suppress(PermissionError):
                os.remove(self.db_path)
            # connection/AV may still hold the file on Windows

    def _make_result(self, category, subtype, mutations):
        return PostMortemResult(
            decision_id="1",
            agent_id="agentA",
            stock_code="600000",
            error_category=category,
            error_subtype=subtype,
            rule_trigger={},
            primary_cause="cause",
            lessons={"lesson": "l", "action": "a"},
            mutation_candidates=mutations,
        )

    def test_save_and_collect_dedupes(self):
        vg = {
            "type": "add_filter",
            "target": "valuation_gate",
            "filter": "pe_percentile_max",
            "threshold": 0.7,
        }
        pos = {"type": "tighten_constraint", "target": "position_sizing.single_position"}

        self.pm._save_post_mortem(
            1,
            self._make_result(
                ErrorCategory.VALUATION_ERROR, ErrorSubtype.MULTIPLE_COMPRESSION, [vg]
            ),
        )
        self.pm._save_post_mortem(
            2,
            self._make_result(
                ErrorCategory.RISK_ERROR, ErrorSubtype.POSITION_SIZE_ERROR, [vg, pos]
            ),
        )

        # two rows persisted
        rows = self.pm.db.execute("SELECT COUNT(*) FROM post_mortem_analysis").fetchone()[0]
        self.assertEqual(rows, 2)

        # collection dedupes identical (type, target, filter) → 2 distinct
        collected = self.pm.collect_recent_mutations(lookback_months=6)
        self.assertEqual(len(collected), 2)
        targets = {m["target"] for m in collected}
        self.assertEqual(targets, {"valuation_gate", "position_sizing.single_position"})

    def test_collect_empty_when_no_analysis_table(self):
        self.assertEqual(self.pm.collect_recent_mutations(), [])


class TestEvolutionMutationIngestion(unittest.TestCase):
    def setUp(self):
        # Instance without __init__ (avoids sandbox/db construction chain).
        self.eng = EvolutionEngineV1.__new__(EvolutionEngineV1)

    def _genome(self, **dims):
        base = {
            "valuation": 50,
            "quality": 50,
            "growth": 50,
            "momentum": 50,
            "macro": 50,
            "contrarian": 50,
            "patience": 50,
            "concentration": 50,
        }
        base.update(dims)
        return {
            "investment_identity": {"dimensions": base},
            "factor_model": {"value": {"weight": 0.2}},
        }

    def test_apply_nudges_known_targets_and_skips_unknown(self):
        genome = self._genome()
        mutations = [
            {"target": "valuation_gate"},
            {"target": "position_sizing.single_position"},
            {"target": "decision_graph", "filter": "trailing_stop"},
            {"target": "market_regime_adapter"},
            {"target": "unknown_subsystem"},  # must be ignored, no crash
        ]
        self.eng._apply_post_mortem_mutations(genome, mutations)
        d = genome["investment_identity"]["dimensions"]
        self.assertEqual(d["valuation"], 58)  # +8
        self.assertEqual(d["concentration"], 42)  # -8
        self.assertEqual(d["patience"], 58)  # +8 (trailing_stop)
        self.assertEqual(d["macro"], 58)  # +8
        self.assertEqual(d["quality"], 50)  # untouched by these
        # input list itself is left intact
        self.assertIn("unknown_subsystem", [m["target"] for m in mutations])

    def test_mutate_applies_pending_else_unchanged(self):
        genome = self._genome()
        self.eng._mutate(genome, pending_mutations=[{"target": "market_regime_adapter"}])
        self.assertEqual(genome["investment_identity"]["dimensions"]["macro"], 58)
        self.assertIn("value", genome["factor_model"])  # factor weights kept

        genome2 = self._genome()
        self.eng._mutate(genome2, pending_mutations=None)
        # no crash, dimensions unchanged from the 50 baseline
        self.assertEqual(genome2["investment_identity"]["dimensions"]["macro"], 50)

    def test_run_cycle_accepts_pending_mutations_kwarg(self):
        import inspect

        sig = inspect.signature(EvolutionEngineV1.run_cycle)
        self.assertIn("pending_mutations", sig.parameters)
        self.assertIsNone(sig.parameters["pending_mutations"].default)


if __name__ == "__main__":
    unittest.main()
