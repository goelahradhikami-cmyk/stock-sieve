"""
Evaluation Database — Memory hub for the Stock Sieve system.

Facade module: re-exports ``EvaluationDB``, ``compute_personality_score``,
``compute_regime_adjusted_score``, and ``with_conn`` for backward
compatibility. The actual implementation is split across three modules:

  * ``evaluation_schema``   — DDL + DDL_V21 constants (11 base tables + v2.1)
  * ``evaluation_crud``      — ``with_conn`` decorator + ``EvaluationCRUDMixin``
                               (all insert / query / committee / quant methods)
  * ``evaluation_migration`` — ``EvaluationMigrationMixin`` (migrate_v2_1,
                               migrate_v2_3, migrate_committee_decisions_v2_1_1)

All existing ``from src.data.evaluation_db import …`` paths continue to work
unchanged — this module re-exports every public symbol that was previously
defined here.
"""

import os
import sqlite3

from .evaluation_crud import EvaluationCRUDMixin, logger, with_conn  # noqa: F401
from .evaluation_migration import EvaluationMigrationMixin
from .evaluation_schema import DDL, DDL_V21  # noqa: F401 (re-export)


class EvaluationDB(EvaluationCRUDMixin, EvaluationMigrationMixin):
    """SQLite-based evaluation database.

    Combines CRUD methods (insert / query / committee / quant-auditor) from
    ``EvaluationCRUDMixin`` and migration methods from
    ``EvaluationMigrationMixin``. Only ``__init__``, ``init_db``, and
    ``connect`` are defined here; everything else is inherited.
    """

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db_path = db_path
        # Guard against ":memory:" and paths without a directory component
        # (os.path.dirname returns "" for both, which makedirs rejects).
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    @with_conn
    def init_db(self, conn):
        """Create all tables if they don't exist."""
        conn.executescript(DDL)
        conn.commit()

    def connect(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


# ═══════════════════════════════════════════════════════════
# personality_score calculation (module-level functions)
# ═══════════════════════════════════════════════════════════


def compute_personality_score(
    total_return: float, sharpe: float, max_drawdown: float, calibration: float, thesis: float
) -> float:
    """Compute the composite personality_score per evolution_engine §1.

    personality_score = return×0.3 + sharpe×0.25 + drawdown×0.2
                      + calibration×0.15 + thesis×0.1

    All inputs normalized 0-1.
    """
    # Normalize to 0-1
    ret_norm = min(1.0, max(0.0, total_return / 0.50))  # 50% return = 1.0
    sharpe_norm = min(1.0, max(0.0, sharpe / 2.0))  # Sharpe 2.0 = 1.0
    dd_norm = min(1.0, max(0.0, 1.0 - abs(max_drawdown) / 0.40))  # 0% DD = 1.0, 40% DD = 0.0
    cal_norm = min(1.0, max(0.0, calibration))  # already 0-1
    thesis_norm = min(1.0, max(0.0, thesis))  # already 0-1

    return (
        ret_norm * 0.30 + sharpe_norm * 0.25 + dd_norm * 0.20 + cal_norm * 0.15 + thesis_norm * 0.10
    )


def compute_regime_adjusted_score(base_score: float, regime: str) -> float:
    """Apply regime multiplier per evolution_engine §2."""
    multipliers = {"bull": 1.0, "rotation": 1.0, "bear": 1.5, "crisis": 2.0}
    return base_score * multipliers.get(regime, 1.0)


__all__ = [
    "EvaluationDB",
    "with_conn",
    "compute_personality_score",
    "compute_regime_adjusted_score",
    "DDL",
    "DDL_V21",
]
