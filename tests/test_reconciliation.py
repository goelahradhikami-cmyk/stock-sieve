"""
Tests for src/audit/reconciliation.py — atomic decision reconciliation.

Builds a throwaway EvaluationDB, seeds 6 funnel shapes, and asserts the derived
fields, *_missing_reason enums, net cost-adjusted alpha, anomaly flags, and
idempotent upsert behavior.
"""

import sqlite3

import pytest

from src.audit.reconciliation import ReconciliationBuilder, RECONCILIATION_VERSION
from src.data.evaluation_db import EvaluationDB


# ────────────────────────────────────────────────────────────
# Fixture DB
# ────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "eval_test.db")
    db = EvaluationDB(db_path=path)
    db.init_db()
    db.migrate_v2_1()
    db.migrate_committee_decisions_v2_1_1()
    db.migrate_v2_3()
    db.migrate_v2_5_reconciliation()
    conn = db.connect()
    # evaluation_results.alpha_error / predicted_alpha are added at runtime by
    # BatchEvaluationRunner; ensure the column exists for the test DB.
    try:
        conn.execute("ALTER TABLE evaluation_results ADD COLUMN alpha_error REAL")
        conn.execute("ALTER TABLE evaluation_results ADD COLUMN predicted_alpha REAL")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    return db


def _mk_research(conn, rid_tag, alpha=8.0, conf=7.0, entry_price=100.0, entry_date="2025-01-02"):
    c = conn.execute(
        """INSERT INTO research_decisions
           (agent_id, genome_hash, security_id, thesis_id, thesis_claim,
            thesis_invalidation, alpha_score, confidence, factor_snapshot,
            decision_hash, input_hash, entry_price, entry_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "agent_test",
            "gh_test",
            f"600{rid_tag}.SH",
            "auto_x",
            "claim",
            "inv",
            alpha,
            conf,
            "{}",
            f"dh_{rid_tag}",
            f"ih_{rid_tag}",
            entry_price,
            entry_date,
        ),
    )
    conn.commit()
    return c.lastrowid


def _mk_signal(conn, rid, alpha=8.0, conf=7.0):
    conn.execute(
        """INSERT INTO signal_snapshot
           (research_decision_id, signal_date, security_id, agent_id,
            factor_values, alpha_score, confidence, entry_date)
           VALUES (?,?,?,?,?,?,?,?)""",
        (rid, "2025-01-02", "600000.SH", "agent_test", "{}", alpha, conf, "2025-01-02"),
    )
    conn.commit()


def _mk_committee(conn, rid, verdict="APPROVE", weighted=80.0, cap_mod=1.0):
    conn.execute(
        """INSERT INTO committee_decisions
           (committee_id, research_decision_id, verdict, weighted_score,
            position_cap_modifier)
           VALUES (?,?,?,?,?)""",
        (f"cm_{rid}", rid, verdict, weighted, cap_mod),
    )
    conn.commit()


def _mk_portfolio(conn, rid, final_weight=0.05, base=0.05, kelly=0.03):
    conn.execute(
        """INSERT INTO portfolio_decisions
           (research_decision_id, policy_id, agent_id, base_weight,
            kelly_weight, regime_multiplier, final_weight, decision_date)
           VALUES (?,?,?,?,?,?,?,?)""",
        (rid, "default", "agent_test", base, kelly, 1.0, final_weight, "2025-01-02"),
    )
    conn.commit()


def _mk_execution(conn, rid, fill=101.0, qty=100, cost=50.0, action="BUY"):
    conn.execute(
        """INSERT INTO portfolio_execution
           (portfolio_decision_id, research_decision_id, agent_id, security_id,
            action, fill_price, quantity, total_cost, execution_date)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (1, rid, "agent_test", "600000.SH", action, fill, qty, cost, "2025-01-02"),
    )
    conn.commit()


def _mk_eval(
    conn,
    rid,
    alpha=0.07,
    stock=0.10,
    market=0.03,
    err=-0.01,
    horizon=20,
    eval_date="2025-02-01",
    verdict="market_alpha_positive",
):
    conn.execute(
        """INSERT INTO evaluation_results
           (research_decision_id, horizon_days, eval_date, stock_return,
            market_return, alpha_vs_market, alpha_error, verdict, evaluated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (rid, horizon, eval_date, stock, market, alpha, err, verdict, "2025-02-01"),
    )
    conn.commit()


# ────────────────────────────────────────────────────────────
# Per-funnel-shape tests
# ────────────────────────────────────────────────────────────


def test_full_chain(db):
    conn = db.connect()
    rid = _mk_research(conn, "1", alpha=8.0, conf=7.0, entry_price=100.0)
    _mk_signal(conn, rid, alpha=8.2, conf=7.0)  # drift = -0.2
    _mk_committee(conn, rid, "APPROVE", weighted=80.0, cap_mod=1.0)
    _mk_portfolio(conn, rid, final_weight=0.05)
    _mk_execution(conn, rid, fill=101.0, qty=100, cost=50.0)
    _mk_eval(conn, rid, alpha=0.07, stock=0.10, market=0.03)
    conn.close()

    rb = ReconciliationBuilder(db.connect())
    row = rb.build_for_decision(rid)
    rb.conn.close()

    assert row["has_signal_snapshot"] == 1
    assert row["has_committee"] == 1
    assert row["has_portfolio"] == 1
    assert row["has_execution"] == 1
    assert row["has_eval"] == 1
    assert row["pipeline_stage_reached"] == 6
    assert row["committee_verdict"] == "APPROVE"
    assert row["committee_position_cap"] == 1.0
    assert row["committee_weighted_score"] == 80.0
    assert row["portfolio_action"] == "BUY"
    assert row["portfolio_final_weight"] == 0.05

    # Cost drag + net alpha
    assert abs(row["cost_drag_pct"] - (50.0 / (101.0 * 100))) < 1e-9
    assert abs(row["net_alpha_after_cost"] - (0.07 - 50.0 / (101.0 * 100))) < 1e-9
    assert row["net_alpha_after_cost"] is not None

    # alpha drift captured, small so no anomaly
    assert abs(row["alpha_drift"] - (-0.2)) < 1e-9
    assert row["three_price_mismatch_flag"] == 0
    # Historical bug: portfolio_decision_id never written -> link broken
    assert row["eval_portfolio_link_broken"] == 1
    assert row["anomaly_flags"] == ["eval_portfolio_link_broken"]


def test_blocked_at_committee(db):
    conn = db.connect()
    rid = _mk_research(conn, "2")
    # No committee row -> validator block path. Eval still gets computed.
    _mk_eval(conn, rid)
    conn.close()

    rb = ReconciliationBuilder(db.connect())
    row = rb.build_for_decision(rid)
    rb.conn.close()

    assert row["has_committee"] == 0
    assert row["committee_missing_reason"] == "validator_block"
    assert row["has_portfolio"] == 0
    assert row["portfolio_missing_reason"] == "no_decision"
    assert row["has_execution"] == 0
    assert row["execution_missing_reason"] == "no_portfolio"
    assert row["has_eval"] == 1
    # eval is an independent milestone -> stage 6 even without committee
    assert row["pipeline_stage_reached"] == 6
    assert row["eval_portfolio_link_broken"] == 1


def test_approved_but_zero_lot(db):
    conn = db.connect()
    rid = _mk_research(conn, "3")
    _mk_signal(conn, rid)
    _mk_committee(conn, rid, "APPROVE")
    _mk_portfolio(conn, rid, final_weight=0.05)
    # intentionally NO execution
    _mk_eval(conn, rid)
    conn.close()

    rb = ReconciliationBuilder(db.connect())
    row = rb.build_for_decision(rid)
    rb.conn.close()

    assert row["has_portfolio"] == 1
    assert row["has_execution"] == 0
    assert row["execution_missing_reason"] == "zero_lot"
    assert "approved_but_not_executed" in row["anomaly_flags"]
    assert "eval_portfolio_link_broken" in row["anomaly_flags"]


def test_pending_t_plus_n(db):
    conn = db.connect()
    rid = _mk_research(conn, "4", entry_date="2099-01-02")  # future -> no eval yet
    _mk_signal(conn, rid)
    _mk_committee(conn, rid, "APPROVE")
    _mk_portfolio(conn, rid, final_weight=0.05)
    _mk_execution(conn, rid, fill=100.0, qty=100, cost=10.0)
    # no evaluation row (T+N not reached)
    conn.close()

    rb = ReconciliationBuilder(db.connect())
    row = rb.build_for_decision(rid)
    rb.conn.close()

    assert row["has_execution"] == 1
    assert row["has_eval"] == 0
    assert row["eval_missing_reason"] == "pending_t_plus_n"
    assert row["pipeline_stage_reached"] == 5
    # no eval -> link not applicable, no eval-driven anomaly
    assert row["eval_portfolio_link_broken"] == 0
    assert "eval_portfolio_link_broken" not in row["anomaly_flags"]
    assert "price_mismatch" not in row["anomaly_flags"]


def test_signal_drift(db):
    conn = db.connect()
    rid = _mk_research(conn, "5", alpha=8.0, conf=7.0, entry_price=100.0)
    _mk_signal(conn, rid, alpha=2.0, conf=7.0)  # drift = +6
    _mk_committee(conn, rid, "APPROVE")
    _mk_portfolio(conn, rid, final_weight=0.05)
    _mk_execution(conn, rid, fill=100.0, qty=100, cost=10.0)  # no price mismatch
    _mk_eval(conn, rid)
    conn.close()

    rb = ReconciliationBuilder(db.connect())
    row = rb.build_for_decision(rid)
    rb.conn.close()

    assert abs(row["alpha_drift"] - 6.0) < 1e-9
    assert "alpha_drift>1" in row["anomaly_flags"]
    assert row["three_price_mismatch_flag"] == 0


def test_three_price_mismatch(db):
    conn = db.connect()
    rid = _mk_research(conn, "6", alpha=8.0, conf=7.0, entry_price=100.0)
    _mk_signal(conn, rid, alpha=8.0, conf=7.0)
    _mk_committee(conn, rid, "APPROVE")
    _mk_portfolio(conn, rid, final_weight=0.05)
    _mk_execution(conn, rid, fill=110.0, qty=100, cost=10.0)  # +10% off signal
    _mk_eval(conn, rid)
    conn.close()

    rb = ReconciliationBuilder(db.connect())
    row = rb.build_for_decision(rid)
    rb.conn.close()

    assert row["three_price_mismatch_flag"] == 1
    assert "price_mismatch" in row["anomaly_flags"]
    assert abs(row["price_slippage_vs_signal"] - 0.10) < 1e-9


# ────────────────────────────────────────────────────────────
# Idempotency + funnel
# ────────────────────────────────────────────────────────────


def test_idempotent_reconcile_all(db):
    conn = db.connect()
    # Build all 6 shapes in one DB.
    r_full = _mk_research(conn, "A", entry_price=100.0)
    _mk_signal(conn, r_full, alpha=8.2)
    _mk_committee(conn, r_full)
    _mk_portfolio(conn, r_full, final_weight=0.05)
    _mk_execution(conn, r_full, fill=101.0, qty=100, cost=50.0)
    _mk_eval(conn, r_full)

    r_block = _mk_research(conn, "B")
    _mk_eval(conn, r_block)

    r_zero = _mk_research(conn, "C")
    _mk_signal(conn, r_zero)
    _mk_committee(conn, r_zero)
    _mk_portfolio(conn, r_zero, final_weight=0.05)
    _mk_eval(conn, r_zero)

    r_pending = _mk_research(conn, "D", entry_date="2099-01-02")
    _mk_signal(conn, r_pending)
    _mk_committee(conn, r_pending)
    _mk_portfolio(conn, r_pending, final_weight=0.05)
    _mk_execution(conn, r_pending, fill=100.0, qty=100, cost=10.0)

    r_drift = _mk_research(conn, "E", alpha=8.0, entry_price=100.0)
    _mk_signal(conn, r_drift, alpha=2.0)
    _mk_committee(conn, r_drift)
    _mk_portfolio(conn, r_drift, final_weight=0.05)
    _mk_execution(conn, r_drift, fill=100.0, qty=100, cost=10.0)
    _mk_eval(conn, r_drift)

    r_price = _mk_research(conn, "F", alpha=8.0, entry_price=100.0)
    _mk_signal(conn, r_price, alpha=8.0)
    _mk_committee(conn, r_price)
    _mk_portfolio(conn, r_price, final_weight=0.05)
    _mk_execution(conn, r_price, fill=110.0, qty=100, cost=10.0)
    _mk_eval(conn, r_price)
    conn.close()

    n1 = ReconciliationBuilder.reconcile_all(db.db_path)
    n2 = ReconciliationBuilder.reconcile_all(db.db_path)
    assert n1 == 6
    assert n2 == 6  # idempotent: same row count

    # Row count stable, content stable
    conn = db.connect()
    total = conn.execute("SELECT COUNT(*) FROM decision_reconciliation").fetchone()[0]
    assert total == 6
    # re-run must not duplicate rows (PK = research_decision_id)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM decision_reconciliation WHERE research_decision_id=?",
            (r_full,),
        ).fetchone()[0]
        == 1
    )
    conn.close()

    # Funnel reflects the 6 shapes
    funnel = ReconciliationBuilder.get_funnel(db.db_path)
    assert funnel["total"] == 6
    assert funnel["stages"]["research"] == 6
    assert funnel["stages"]["committee"] == 5  # one blocked
    assert funnel["stages"]["portfolio"] == 5
    assert funnel["stages"]["execution"] == 4  # r_block + r_zero have no execution
    assert funnel["stages"]["evaluation"] == 5  # one pending (future entry)
    assert funnel["stages"]["signal_snapshot"] == 5
    # distribution sums to 6
    assert sum(funnel["stage_distribution"].values()) == 6


def test_reconciliation_version_constant(db):
    assert RECONCILIATION_VERSION == "1.0"
