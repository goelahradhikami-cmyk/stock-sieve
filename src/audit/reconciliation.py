"""
Atomic Decision Reconciliation — decision_reconciliation table.

Stitches the 6 horizontally-sharded decision tables into ONE row per
``research_decision_id`` so every stage of the pipeline (Research → Signal
Snapshot → Committee → Portfolio → Execution → T+N Evaluation) and the derived
metrics (alpha drift, three-price cross-check, cost-drag net alpha, evolution
visibility) are answered from a single place.

Design goals (see docs/specs/reconciliation_table_v1.md):
  * One row per research_decision_id (1:1, no synthetic PK).
  * Every stage has a has_* (0/1) flag + a *_missing_reason enum string.
  * Read-only LEFT JOINs over the 6 source tables; the source schemas are NEVER
    modified. The historical ``evaluation_results.portfolio_decision_id`` bug is
    only *flagged* (eval_portfolio_link_broken), not fixed.
  * Evolution's fitness window is read-only replicated (engine_v1: eval_date
    within 1 year AND agent has >= MIN_SAMPLES evals in that window).

IMPORTANT (import-cycle safety): this module imports ONLY the standard library
at module load. ``from src.data.evaluation_db import EvaluationDB`` is imported
lazily *inside* reconcile_all / reconcile_range so that
``evaluation_db → evaluation_migration → reconciliation → evaluation_db`` never
forms a cycle at import time.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import date, datetime

# Schema / evolution constants (kept here so evaluation_migration can import the
# DDL without pulling in anything heavier).
RECONCILIATION_VERSION = "1.0"

# engine_v1._calculate_fitness requires >= MIN_SAMPLES evaluations within the
# 1-year window before an agent is scored. Replicated read-only here.
EVOLUTION_MIN_SAMPLES = 10
EVOLUTION_WINDOW_DAYS = 365

# Verdicts that count as "committee approved" in the daily pipeline.
APPROVE_VERDICTS = ("APPROVE", "APPROVE_WITH_CONDITIONS")


DDL_RECONCILIATION = """
CREATE TABLE IF NOT EXISTS decision_reconciliation (
    -- A. Anchor
    research_decision_id   INTEGER PRIMARY KEY,
    decision_hash          TEXT,
    agent_id               TEXT,
    security_id            TEXT,
    entry_date             DATE,

    -- B. Research (always present)
    rd_alpha_score         REAL,
    rd_confidence          REAL,
    rd_entry_price         REAL,
    rd_genome_hash         TEXT,

    -- C. Signal Snapshot mirror (drift detection)
    has_signal_snapshot    INTEGER DEFAULT 0,
    ss_alpha_score         REAL,
    ss_confidence          REAL,
    alpha_drift            REAL,
    confidence_drift       REAL,
    signal_snapshot_missing_reason TEXT,

    -- D. Committee
    has_committee          INTEGER DEFAULT 0,
    committee_verdict      TEXT,
    committee_weighted_score REAL,
    committee_position_cap REAL,
    committee_missing_reason TEXT,

    -- E. Portfolio
    has_portfolio          INTEGER DEFAULT 0,
    portfolio_action       TEXT,
    portfolio_final_weight REAL,
    portfolio_base_weight  REAL,
    portfolio_kelly_weight REAL,
    portfolio_missing_reason TEXT,

    -- F. Execution
    has_execution          INTEGER DEFAULT 0,
    exec_fill_price        REAL,
    exec_quantity          INTEGER,
    exec_total_cost        REAL,
    exec_slippage          REAL,
    price_slippage_vs_signal REAL,
    execution_missing_reason TEXT,

    -- G. T+N Evaluation (may be multiple horizons per rid; scalars use the
    --    primary row = largest horizon with a non-null alpha_vs_market)
    has_eval               INTEGER DEFAULT 0,
    eval_horizon_days      INTEGER,
    eval_stock_return      REAL,
    eval_alpha_vs_market   REAL,
    eval_verdict           TEXT,
    eval_alpha_error       REAL,
    eval_missing_reason    TEXT,

    -- H. Derived atomic metrics
    net_alpha_after_cost   REAL,
    cost_drag_pct          REAL,
    pipeline_stage_reached INTEGER DEFAULT 1,
    three_price_mismatch_flag INTEGER DEFAULT 0,
    eval_portfolio_link_broken INTEGER DEFAULT 0,

    -- I. Evolution association
    counted_in_fitness     INTEGER DEFAULT 0,
    fitness_invisible_reason TEXT,

    -- J. Metadata
    anomaly_flags          TEXT,
    reconciled_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reconciliation_version TEXT DEFAULT '1.0'
);
CREATE INDEX IF NOT EXISTS idx_dr_agent_date ON decision_reconciliation(agent_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_dr_stage ON decision_reconciliation(pipeline_stage_reached);
CREATE INDEX IF NOT EXISTS idx_dr_has_eval ON decision_reconciliation(has_eval);
CREATE INDEX IF NOT EXISTS idx_dr_hash ON decision_reconciliation(decision_hash);
"""


def _to_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_div(num, den):
    if num is None or den in (None, 0):
        return None
    try:
        return num / den
    except ZeroDivisionError:
        return None


class ReconciliationBuilder:
    """Build and upsert one atomic reconciliation row per research_decision_id."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    # ──────────────────────────────────────────────────────
    # Single-decision build + upsert
    # ──────────────────────────────────────────────────────

    def build_for_decision(self, research_decision_id: int) -> dict | None:
        """Compute every atomic field for one research_decision_id.

        Returns None if the research_decision does not exist. All reason enums
        and derived metrics are computed here (single source of truth).
        """
        rd = self.conn.execute(
            "SELECT * FROM research_decisions WHERE id=?", (research_decision_id,)
        ).fetchone()
        if rd is None:
            return None
        rd = dict(rd)

        ss = self.conn.execute(
            "SELECT * FROM signal_snapshot WHERE research_decision_id=?",
            (research_decision_id,),
        ).fetchone()
        ss = dict(ss) if ss else None

        cd = self.conn.execute(
            "SELECT * FROM committee_decisions WHERE research_decision_id=?",
            (research_decision_id,),
        ).fetchone()
        cd = dict(cd) if cd else None

        pd = self.conn.execute(
            "SELECT * FROM portfolio_decisions WHERE research_decision_id=?",
            (research_decision_id,),
        ).fetchone()
        pd = dict(pd) if pd else None

        pe = self.conn.execute(
            "SELECT * FROM portfolio_execution WHERE research_decision_id=?",
            (research_decision_id,),
        ).fetchone()
        pe = dict(pe) if pe else None

        evals = self.conn.execute(
            "SELECT * FROM evaluation_results WHERE research_decision_id=? "
            "ORDER BY horizon_days DESC",
            (research_decision_id,),
        ).fetchall()
        evals = [dict(e) for e in evals]
        # Primary eval = largest horizon with a non-null alpha_vs_market.
        primary_eval = None
        for e in evals:
            if e.get("alpha_vs_market") is not None:
                primary_eval = e
                break
        if primary_eval is None and evals:
            primary_eval = evals[0]

        return self._assemble(rd, ss, cd, pd, pe, evals, primary_eval)

    def _assemble(self, rd, ss, cd, pd, pe, evals, primary_eval) -> dict:
        agent_id = rd.get("agent_id")
        rd_alpha = _to_float(rd.get("alpha_score"))
        rd_conf = _to_float(rd.get("confidence"))
        rd_entry = _to_float(rd.get("entry_price"))

        # ── C. Signal snapshot / drift ────────────────────
        has_signal = 1 if ss else 0
        ss_alpha = _to_float(ss.get("alpha_score")) if ss else None
        ss_conf = _to_float(ss.get("confidence")) if ss else None
        alpha_drift = (
            (rd_alpha - ss_alpha) if (rd_alpha is not None and ss_alpha is not None) else None
        )
        conf_drift = (rd_conf - ss_conf) if (rd_conf is not None and ss_conf is not None) else None
        # signal_snapshot is only written on the APPROVE path; if the
        # decision never reached it, it is 'not_reached'.
        signal_missing = None if has_signal else "not_reached"

        # ── D. Committee ──────────────────────────────────
        has_committee = 1 if cd else 0
        committee_verdict = cd.get("verdict") if cd else None
        committee_weighted = _to_float(cd.get("weighted_score")) if cd else None
        # NOTE: committee_decisions has no `position_cap` column; the committee's
        # position-cap decision lives in `position_cap_modifier`.
        committee_position_cap = _to_float(cd.get("position_cap_modifier")) if cd else None
        # A research_decision with alpha<4 is never inserted, so the only
        # way to have an rd row but no committee row is the validator
        # BLOCKing before the committee insert.
        committee_missing = None if has_committee else "validator_block"

        # ── E. Portfolio ──────────────────────────────────
        has_portfolio = 1 if pd else 0
        if pd:
            portfolio_action = (
                pe.get("action")
                if pe
                else (
                    "BUY"
                    if _to_float(pd.get("final_weight")) and _to_float(pd.get("final_weight")) > 0
                    else "HOLD"
                )
            )
            portfolio_final = _to_float(pd.get("final_weight"))
            portfolio_base = _to_float(pd.get("base_weight"))
            portfolio_kelly = _to_float(pd.get("kelly_weight"))
            portfolio_missing = None
        else:
            portfolio_action = None
            portfolio_final = portfolio_base = portfolio_kelly = None
            if has_committee and committee_verdict not in APPROVE_VERDICTS:
                portfolio_missing = "verdict_reject"
            elif has_committee:
                # Committee approved but no portfolio_decision persisted
                # (e.g. empty PositionDecision / zero order qty upstream).
                portfolio_missing = "approved_but_no_portfolio"
            else:
                portfolio_missing = "no_decision"

        # ── F. Execution ──────────────────────────────────
        has_execution = 1 if pe else 0
        if pe:
            exec_fill = _to_float(pe.get("fill_price"))
            exec_qty = pe.get("quantity")
            exec_cost = _to_float(pe.get("total_cost"))
            exec_slip = _to_float(pe.get("slippage"))
            execution_missing = None
        else:
            exec_fill = exec_qty = exec_cost = exec_slip = None
            execution_missing = "no_portfolio" if not has_portfolio else "zero_lot"

        price_slip_vs_signal = (
            _safe_div((exec_fill - rd_entry), rd_entry)
            if (has_execution and rd_entry not in (None, 0))
            else None
        )

        # ── G. T+N Evaluation ─────────────────────────────
        has_eval = 1 if evals else 0
        if primary_eval is not None:
            eval_horizon = primary_eval.get("horizon_days")
            eval_stock_ret = _to_float(primary_eval.get("stock_return"))
            eval_alpha = _to_float(primary_eval.get("alpha_vs_market"))
            eval_verdict = primary_eval.get("verdict")
            eval_alpha_error = _to_float(primary_eval.get("alpha_error"))
            eval_link_broken = 1 if primary_eval.get("portfolio_decision_id") is None else 0
            eval_missing = None
        else:
            eval_horizon = eval_stock_ret = eval_alpha = eval_verdict = eval_alpha_error = None
            eval_link_broken = 0
            eval_missing = "pending_t_plus_n"

        # ── H. Derived atomic metrics ──────────────────────
        cost_drag = (
            _safe_div(exec_cost, (exec_fill * exec_qty))
            if (has_execution and exec_fill not in (None, 0) and exec_qty)
            else None
        )
        net_alpha = (
            (eval_alpha - cost_drag)
            if (has_eval and has_execution and eval_alpha is not None and cost_drag is not None)
            else None
        )

        # pipeline_stage_reached: max milestone reached (eval is an independent
        # milestone — a BLOCKed decision can still be T+N evaluated).
        stage = 1  # research always present
        if has_signal:
            stage = max(stage, 2)
        if has_committee:
            stage = max(stage, 3)
        if has_portfolio:
            stage = max(stage, 4)
        if has_execution:
            stage = max(stage, 5)
        if has_eval:
            stage = max(stage, 6)

        # three_price_mismatch: signal price (rd.entry_price) vs fill price.
        # NOTE: the evaluator reuses research_decisions.entry_price as the
        # "evaluation entry price", so the only independent comparison is
        # signal vs fill.
        three_price_mismatch = 0
        if (
            has_execution
            and rd_entry not in (None, 0)
            and exec_fill is not None
            and abs((exec_fill - rd_entry) / rd_entry) > 0.01
        ):
            three_price_mismatch = 1

        # ── I. Evolution visibility (read-only replication of engine_v1) ──
        counted, fitness_reason = self._evolution_visibility(
            agent_id, rd.get("entry_date"), primary_eval
        )

        # ── J. Anomaly flags ───────────────────────────────
        anomalies = []
        if alpha_drift is not None and abs(alpha_drift) > 1:
            anomalies.append("alpha_drift>1")
        if conf_drift is not None and abs(conf_drift) > 1:
            anomalies.append("confidence_drift>1")
        if three_price_mismatch:
            anomalies.append("price_mismatch")
        if eval_link_broken:
            anomalies.append("eval_portfolio_link_broken")
        if has_portfolio and not has_committee:
            anomalies.append("portfolio_without_committee")
        if has_committee and committee_verdict not in APPROVE_VERDICTS and has_portfolio:
            anomalies.append("committee_reject_but_traded")
        if has_committee and committee_verdict in APPROVE_VERDICTS and not has_portfolio:
            anomalies.append("approved_but_no_portfolio")
        if (
            has_committee
            and committee_verdict in APPROVE_VERDICTS
            and has_portfolio
            and not has_execution
        ):
            anomalies.append("approved_but_not_executed")
        if has_execution and exec_qty in (None, 0):
            anomalies.append("zero_lot")
        if net_alpha is not None and net_alpha < 0:
            anomalies.append("negative_net_alpha_after_cost")

        return {
            # A
            "research_decision_id": rd.get("id"),
            "decision_hash": rd.get("decision_hash"),
            "agent_id": agent_id,
            "security_id": rd.get("security_id"),
            "entry_date": rd.get("entry_date"),
            # B
            "rd_alpha_score": rd_alpha,
            "rd_confidence": rd_conf,
            "rd_entry_price": rd_entry,
            "rd_genome_hash": rd.get("genome_hash"),
            # C
            "has_signal_snapshot": has_signal,
            "ss_alpha_score": ss_alpha,
            "ss_confidence": ss_conf,
            "alpha_drift": alpha_drift,
            "confidence_drift": conf_drift,
            "signal_snapshot_missing_reason": signal_missing,
            # D
            "has_committee": has_committee,
            "committee_verdict": committee_verdict,
            "committee_weighted_score": committee_weighted,
            "committee_position_cap": committee_position_cap,
            "committee_missing_reason": committee_missing,
            # E
            "has_portfolio": has_portfolio,
            "portfolio_action": portfolio_action,
            "portfolio_final_weight": portfolio_final,
            "portfolio_base_weight": portfolio_base,
            "portfolio_kelly_weight": portfolio_kelly,
            "portfolio_missing_reason": portfolio_missing,
            # F
            "has_execution": has_execution,
            "exec_fill_price": exec_fill,
            "exec_quantity": exec_qty,
            "exec_total_cost": exec_cost,
            "exec_slippage": exec_slip,
            "price_slippage_vs_signal": price_slip_vs_signal,
            "execution_missing_reason": execution_missing,
            # G
            "has_eval": has_eval,
            "eval_horizon_days": eval_horizon,
            "eval_stock_return": eval_stock_ret,
            "eval_alpha_vs_market": eval_alpha,
            "eval_verdict": eval_verdict,
            "eval_alpha_error": eval_alpha_error,
            "eval_missing_reason": eval_missing,
            # H
            "net_alpha_after_cost": net_alpha,
            "cost_drag_pct": cost_drag,
            "pipeline_stage_reached": stage,
            "three_price_mismatch_flag": three_price_mismatch,
            "eval_portfolio_link_broken": eval_link_broken,
            # I
            "counted_in_fitness": counted,
            "fitness_invisible_reason": fitness_reason,
            # J
            "anomaly_flags": anomalies,
            "reconciled_at": datetime.now().isoformat(),
            "reconciliation_version": RECONCILIATION_VERSION,
        }

    def _evolution_visibility(self, agent_id, entry_date, primary_eval):
        """Read-only replication of engine_v1._calculate_fitness visibility.

        An evaluation counts toward fitness only if:
          * has_eval = 1 (evaluation exists),
          * its eval_date is within EVOLUTION_WINDOW_DAYS of today,
          * the agent has >= EVOLUTION_MIN_SAMPLES evaluations in that window.
        """
        if not has_eval_flag(primary_eval):
            return 0, "no_eval"
        eval_date = primary_eval.get("eval_date")
        if eval_date is None:
            return 0, "no_eval"
        try:
            ed = date.fromisoformat(str(eval_date)[:10])
        except (ValueError, TypeError):
            return 0, "no_eval"
        if (date.today() - ed).days > EVOLUTION_WINDOW_DAYS:
            return 0, "out_of_window"
        # Agent-level sample count within the 1-year window.
        cnt = self.conn.execute(
            """
            SELECT COUNT(*) FROM evaluation_results er
            JOIN research_decisions rd ON er.research_decision_id = rd.id
            WHERE rd.agent_id = ? AND er.eval_date >= date('now', ?)
            """,
            (agent_id, f"-{EVOLUTION_WINDOW_DAYS} days"),
        ).fetchone()[0]
        if (cnt or 0) < EVOLUTION_MIN_SAMPLES:
            return 0, "cold_start"
        return 1, None

    def upsert(self, row: dict) -> None:
        """Idempotent upsert of one reconciliation row (INSERT OR REPLACE)."""
        anomaly = row.get("anomaly_flags")
        if isinstance(anomaly, list):
            anomaly = json.dumps(anomaly, ensure_ascii=False)
        row = dict(row)
        row["anomaly_flags"] = anomaly

        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        values = [row[c] for c in cols]
        self.conn.execute(
            f"INSERT OR REPLACE INTO decision_reconciliation ({col_list}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()

    # ──────────────────────────────────────────────────────
    # Batch / CLI helpers
    # ──────────────────────────────────────────────────────

    @classmethod
    def _connect(cls, db_path: str) -> sqlite3.Connection:
        # Lazy import to avoid an import cycle at module load time.
        from src.data.evaluation_db import EvaluationDB

        db = EvaluationDB(db_path=db_path)
        return db.connect()

    @classmethod
    def reconcile_all(cls, db_path: str) -> int:
        """Rebuild the entire reconciliation table from the 6 source tables."""
        conn = cls._connect(db_path)
        try:
            rids = [
                r[0]
                for r in conn.execute("SELECT id FROM research_decisions ORDER BY id").fetchall()
            ]
            builder = cls(conn)
            for rid in rids:
                row = builder.build_for_decision(rid)
                if row:
                    builder.upsert(row)
            return len(rids)
        finally:
            conn.close()

    @classmethod
    def reconcile_range(cls, db_path: str, start_date: str, end_date: str) -> int:
        """Rebuild reconciliation rows for research_decisions in a date range."""
        conn = cls._connect(db_path)
        try:
            rids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM research_decisions WHERE date(entry_date) BETWEEN ? AND ? ORDER BY id",
                    (start_date, end_date),
                ).fetchall()
            ]
            builder = cls(conn)
            for rid in rids:
                row = builder.build_for_decision(rid)
                if row:
                    builder.upsert(row)
            return len(rids)
        finally:
            conn.close()

    @classmethod
    def get_funnel(cls, db_path: str) -> dict:
        """Return per-stage independent counts + pipeline_stage_reached dist."""
        conn = cls._connect(db_path)
        try:
            if not _table_exists(conn, "decision_reconciliation"):
                return {"stages": {}, "stage_distribution": {}, "total": 0}
            stages = {
                "research": conn.execute("SELECT COUNT(*) FROM decision_reconciliation").fetchone()[
                    0
                ],
                "signal_snapshot": conn.execute(
                    "SELECT COUNT(*) FROM decision_reconciliation WHERE has_signal_snapshot=1"
                ).fetchone()[0],
                "committee": conn.execute(
                    "SELECT COUNT(*) FROM decision_reconciliation WHERE has_committee=1"
                ).fetchone()[0],
                "portfolio": conn.execute(
                    "SELECT COUNT(*) FROM decision_reconciliation WHERE has_portfolio=1"
                ).fetchone()[0],
                "execution": conn.execute(
                    "SELECT COUNT(*) FROM decision_reconciliation WHERE has_execution=1"
                ).fetchone()[0],
                "evaluation": conn.execute(
                    "SELECT COUNT(*) FROM decision_reconciliation WHERE has_eval=1"
                ).fetchone()[0],
            }
            dist_rows = conn.execute(
                "SELECT pipeline_stage_reached, COUNT(*) FROM decision_reconciliation "
                "GROUP BY pipeline_stage_reached ORDER BY pipeline_stage_reached"
            ).fetchall()
            dist = {r[0]: r[1] for r in dist_rows}
            total = conn.execute("SELECT COUNT(*) FROM decision_reconciliation").fetchone()[0]
            return {"stages": stages, "stage_distribution": dist, "total": total}
        finally:
            conn.close()

    @classmethod
    def get_decision(cls, db_path: str, research_decision_id: int) -> dict | None:
        conn = cls._connect(db_path)
        try:
            if not _table_exists(conn, "decision_reconciliation"):
                return None
            row = conn.execute(
                "SELECT * FROM decision_reconciliation WHERE research_decision_id=?",
                (research_decision_id,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            if isinstance(d.get("anomaly_flags"), str):
                with contextlib.suppress(ValueError, TypeError):
                    d["anomaly_flags"] = json.loads(d["anomaly_flags"])
            return d
        finally:
            conn.close()


def has_eval_flag(primary_eval) -> bool:
    return primary_eval is not None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None
