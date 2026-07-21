"""
Stock Sieve CLI — Command-line interface.

Usage:
    python -m src.cli factor 600519
    python -m src.cli screen --personality value_purist --top 10
    python -m src.cli evolve --simulate
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import get_logger

logger = get_logger(__name__)


def cmd_factor(args):
    """Display all factors for a stock."""
    print(f"🔬 Computing factors for {args.code}...")
    print("(Factor engine ready — needs data provider integration for live data)")
    print()

    from src.factors.engine import FactorEngine
    engine = FactorEngine()
    for family in engine.get_family_names():
        print(f"  [{family}]")
        factors = [f for f in engine.FACTOR_FAMILIES[family]]
        for f in factors:
            print(f"    {f['name']:20s} — {f['description']}")


def cmd_screen(args):
    """Screen stocks using a personality."""
    print(f"🧠 Screening with personality: {args.personality}")
    print(f"   Top {args.top} picks")
    try:
        from src.daily_run import daily_run
        daily_run(sample_size=args.top)
    except Exception as e:
        logger.warning("cli: screen failed: %s", e)


def cmd_evolve(args):
    """Run evolution cycle."""
    from src.data.evaluation_db import EvaluationDB
    from src.evolution.engine_v1 import EvolutionEngineV1

    db = EvaluationDB()
    db.init_db()
    db.migrate_v2_1()
    db.migrate_committee_decisions_v2_1_1()
    db.migrate_v2_3()

    print("🧬 Evolution Engine running...")

    if args.simulate:
        engine = EvolutionEngineV1(dry_run=True)
        engine.MIN_SAMPLES = 3
        result = engine.run_cycle()
        print(f"   Dry-run: {result.get('eliminated_candidates', [])} candidates")
    else:
        from src.evolution.risk_genome import KillSwitch
        ks = KillSwitch()
        if not ks.can_evolve():
            print(f"   🛑 Evolution blocked: governance state = {ks.current_state() if hasattr(ks,'current_state') else 'N/A'}")
            return
        engine = EvolutionEngineV1(dry_run=False)
        engine.MIN_SAMPLES = 5
        result = engine.run_cycle()
        print(f"   New agents: {result.get('new_agents', [])}")
        print(f"   Eliminated: {result.get('eliminated', [])}")


def cmd_init(args):
    """Initialize the system."""
    from src.data.evaluation_db import EvaluationDB

    db = EvaluationDB()
    db.init_db()
    db.migrate_v2_1()
    print("✅ Evaluation database initialized (13 tables)")
    print("✅ 8 founder genomes available in config/personalities/")
    print()
    print("Next steps:")
    print("  1. streamlit run src/ui/app.py     — Launch dashboard")
    print("  2. python -m src.cli factor 600519  — Test factor engine")


def cmd_fuse(args):
    """Fuse two agent genomes."""
    import os

    import yaml

    config_dir = os.path.join(os.path.dirname(__file__), "..", "config", "personalities")
    path_a = os.path.join(config_dir, f"{args.parent_a}.yaml")
    path_b = os.path.join(config_dir, f"{args.parent_b}.yaml")

    if not os.path.exists(path_a):
        print(f"❌ Parent A genome not found: {path_a}")
        return
    if not os.path.exists(path_b):
        print(f"❌ Parent B genome not found: {path_b}")
        return

    with open(path_a, encoding="utf-8") as f:
        ga = yaml.safe_load(f)
    with open(path_b, encoding="utf-8") as f:
        gb = yaml.safe_load(f)

    alpha = max(0.3, min(0.7, args.alpha))
    dims = ["valuation", "quality", "growth", "momentum", "macro", "contrarian", "patience", "concentration"]

    print(f"🧬 Fusing {args.parent_a} × {args.parent_b} (α={alpha:.2f})")
    print()
    print("Child identity vector:")
    for d in dims:
        a_val = ga.get("investment_identity", {}).get("dimensions", {}).get(d, 50)
        b_val = gb.get("investment_identity", {}).get("dimensions", {}).get(d, 50)
        child_val = int(a_val * alpha + b_val * (1 - alpha))
        bar = "█" * (child_val // 5)
        print(f"  {d:15s}: {bar:20s} {child_val}")

    print()
    print("Submit to sandbox: streamlit run src/ui/app.py → Genome Fusion Studio")


def cmd_export(args):
    """Export reports."""
    from src.data.evaluation_db import EvaluationDB
    from src.utils.report_exporter import ReportExporter

    db = EvaluationDB()
    db.init_db()

    exporter = ReportExporter(db)

    if args.type == "committee":
        path = exporter.export_committee_report(
            start_date=args.start_date,
            end_date=args.end_date,
            format=args.format,
            output_path=args.output,
        )
    elif args.type == "performance":
        path = exporter.export_performance_report(
            format=args.format,
            output_path=args.output,
        )
    elif args.type == "thesis":
        if not args.stock:
            print("❌ --stock is required for thesis trace export")
            return
        path = exporter.export_thesis_trace(
            stock_code=args.stock,
            format=args.format,
            output_path=args.output,
        )
    elif args.type == "evolution":
        path = exporter.export_evolution_log(
            format=args.format,
            output_path=args.output,
        )

    print(f"✅ Report exported: {path}")


def cmd_status(args):
    """Show system status."""
    from src.data.evaluation_db import EvaluationDB

    db = EvaluationDB()
    db.init_db()

    conn = db.connect()
    agent_count = conn.execute(
        "SELECT COUNT(*) FROM agent_genome_snapshots WHERE status='active'"
    ).fetchone()[0]
    committee_count = conn.execute(
        "SELECT COUNT(*) FROM committee_decisions"
    ).fetchone()[0]
    evaluation_count = conn.execute(
        "SELECT COUNT(*) FROM evaluation_results"
    ).fetchone()[0]
    conn.close()

    print("🧬 Stock Sieve System Status")
    print("=" * 40)
    print(f"  活跃 Agent:     {agent_count}")
    print(f"  委员会会议:     {committee_count}")
    print(f"  评估记录:       {evaluation_count}")
    print("  协议栈:         v1.1 | Genome v3.2 | Committee v1.0.1")
    print("  面板:           streamlit run src/ui/app.py")
    print("  CLI:            python -m src.cli --help")


def cmd_reconcile(args):
    """Atomic decision reconciliation — stitch the 6 decision tables."""
    from src.audit.reconciliation import ReconciliationBuilder
    from src.data.evaluation_db import EvaluationDB

    db = EvaluationDB()
    db.init_db()
    db.migrate_v2_1()
    db.migrate_committee_decisions_v2_1_1()
    db.migrate_v2_3()
    db.migrate_v2_5_reconciliation()

    if args.decision is not None:
        row = ReconciliationBuilder.get_decision(db.db_path, args.decision)
        if row is None:
            print(f"❌ No reconciliation row for research_decision_id={args.decision}")
            return
        print(f"🔍 Reconciliation — research_decision_id={args.decision}")
        print("=" * 56)
        for k in (
            "research_decision_id", "agent_id", "security_id", "entry_date",
            "has_signal_snapshot", "has_committee", "committee_verdict",
            "has_portfolio", "portfolio_action", "has_execution",
            "exec_fill_price", "exec_quantity", "has_eval",
            "eval_horizon_days", "eval_alpha_vs_market", "eval_alpha_error",
            "net_alpha_after_cost", "cost_drag_pct", "pipeline_stage_reached",
            "three_price_mismatch_flag", "eval_portfolio_link_broken",
            "counted_in_fitness", "fitness_invisible_reason",
            "signal_snapshot_missing_reason", "committee_missing_reason",
            "portfolio_missing_reason", "execution_missing_reason",
            "eval_missing_reason", "reconciliation_version",
        ):
            print(f"  {k:32s}: {row.get(k)}")
        flags = row.get("anomaly_flags")
        if isinstance(flags, str):
            try:
                flags = __import__("json").loads(flags)
            except (ValueError, TypeError):
                pass
        print(f"  {'anomaly_flags':32s}: {flags}")
        return

    if args.funnel:
        funnel = ReconciliationBuilder.get_funnel(db.db_path)
        print("📊 Decision Reconciliation Funnel")
        print("=" * 56)
        print(f"  Total reconciliation rows : {funnel['total']}")
        print("  ── per-stage independent counts (non-nested) ──")
        for stage, label in [
            ("research", "Research"),
            ("signal_snapshot", "Signal Snapshot"),
            ("committee", "Committee"),
            ("portfolio", "Portfolio"),
            ("execution", "Execution"),
            ("evaluation", "T+N Evaluation"),
        ]:
            print(f"    {label:18s}: {funnel['stages'].get(stage, 0)}")
        print("  ── pipeline_stage_reached distribution ──")
        for stage in sorted(funnel["stage_distribution"].keys()):
            print(f"    stage {stage}: {funnel['stage_distribution'][stage]}")
        return

    if args.range:
        if ":" not in args.range:
            print("❌ --range expects 'START:END' (e.g. 2026-01-01:2026-07-17)")
            return
        start, end = args.range.split(":", 1)
        n = ReconciliationBuilder.reconcile_range(db.db_path, start.strip(), end.strip())
        print(f"✅ Reconciled {n} decisions in range {start}..{end}")
        return

    # default: full rebuild
    n = ReconciliationBuilder.reconcile_all(db.db_path)
    print(f"✅ Reconciled all {n} research decisions")


def main():
    parser = argparse.ArgumentParser(
        prog="stock-sieve",
        description="Stock Sieve — Multi-Personality Evolutionary Stock Selection Engine",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Initialize database and system")

    # factor
    f = sub.add_parser("factor", help="Compute factors for a stock")
    f.add_argument("code", help="Stock code (e.g. 600519)")

    # screen
    s = sub.add_parser("screen", help="Screen stocks with a personality")
    s.add_argument("--personality", "-p", default="value_purist", help="Personality name")
    s.add_argument("--top", "-n", type=int, default=10, help="Number of top picks")

    # evolve
    e = sub.add_parser("evolve", help="Run evolution cycle")
    e.add_argument("--simulate", action="store_true", help="Use simulated data")
    e.add_argument("--agent", "-a", help="Specific agent to evolve")

    # fuse
    fu = sub.add_parser("fuse", help="Fuse two agent genomes")
    fu.add_argument("--parent-a", "-a", required=True, help="Parent A agent ID")
    fu.add_argument("--parent-b", "-b", required=True, help="Parent B agent ID")
    fu.add_argument("--alpha", type=float, default=0.5, help="Fusion ratio (0.3-0.7)")

    # export
    ex = sub.add_parser("export", help="Export reports")
    ex.add_argument("--type", "-t", required=True,
                     choices=["committee", "performance", "thesis", "evolution"],
                     help="Report type")
    ex.add_argument("--format", "-f", default="md", choices=["md", "xlsx"],
                     help="Export format")
    ex.add_argument("--output", "-o", help="Output file path")
    ex.add_argument("--stock", help="Stock code (for thesis trace)")
    ex.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    ex.add_argument("--end-date", help="End date (YYYY-MM-DD)")

    # status
    sub.add_parser("status", help="Show system status")

    # reconcile
    rc = sub.add_parser("reconcile", help="Atomic decision reconciliation (6 tables → 1 row/decision)")
    rc.add_argument("--range", help="Date range 'START:END' (e.g. 2026-01-01:2026-07-17)")
    rc.add_argument("--decision", type=int, help="Inspect one research_decision_id")
    rc.add_argument("--funnel", action="store_true", help="Print funnel overview")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "factor":
        cmd_factor(args)
    elif args.command == "screen":
        cmd_screen(args)
    elif args.command == "evolve":
        cmd_evolve(args)
    elif args.command == "fuse":
        cmd_fuse(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "reconcile":
        cmd_reconcile(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
