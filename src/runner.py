"""
Daily Runner — Full investment pipeline from data to decisions.

Usage:
    python -m src.runner              # Run full pipeline on sample universe
    python -m src.runner --code 600519  # Analyze single stock
    python -m src.runner --universe csi300  # Screen CSI 300

Data flow:
    DataProvider → FactorEngine → ResearchAgent(s) → ThesisValidator
    → InvestmentCommittee → PortfolioAgent → evaluation_db
"""

import argparse
import hashlib
import os
import sys
from datetime import date, datetime, timedelta

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.committee_agent import CommitteeAgent, apply_committee_decision
from src.agents.portfolio_agent import PortfolioAgent, PortfolioState
from src.agents.research_agent import ResearchAgent
from src.data.db import close_all
from src.data.evaluation_db import EvaluationDB
from src.data.financial_provider import get_financial_provider
from src.data.market_brain import MarketBrain
from src.data.provider import DataProvider
from src.execution.simulator import ExecutionSimulator
from src.factors.engine import FactorEngine
from src.utils.logger import get_logger
from src.validation.rule_registry import RuleRegistry
from src.validation.thesis_validator import ThesisValidator

logger = get_logger(__name__)

import numpy as np
import pandas as pd

# ═══════════════════════════════════════════════════════════
# Universe — loaded from security_master
# ═══════════════════════════════════════════════════════════

SAMPLE_UNIVERSE = None  # Will be loaded dynamically


def get_universe() -> list[str]:
    """Get active stock universe from security_master."""
    try:
        from src.data.security_master import SecurityMaster
        from src.data.universe_filter import UniverseFilter
        master = SecurityMaster()
        df = master.get_active_universe()
        # NOTE: match daily_run's universe params. security_master has
        # avg_amount_20d / list_days == 0 for every row (never populated), so
        # the default UniverseFilter(min_avg_amount_20d=5000) would reject the
        # entire universe and leave tradable_universe empty/garbage.
        f = UniverseFilter(min_avg_amount_20d=0, use_dynamic_liquidity=False)
        clean = f.apply(df)
        return clean["code"].tolist()
    except Exception as e:
        # Fallback to hardcoded sample
        logger.warning("runner: load active universe failed, using hardcoded sample: %s", e)
        return [
            "600519", "000858", "300750", "601318", "000333",
            "600036", "002415", "600276", "601888", "002594",
        ]


def load_genomes() -> list[dict]:
    """Load all founder genome YAMLs."""
    config_dir = os.path.join(
        os.path.dirname(__file__), "..", "config", "personalities"
    )
    genomes = []
    for f in sorted(os.listdir(config_dir)):
        if f.endswith(".yaml"):
            with open(os.path.join(config_dir, f), encoding="utf-8") as fh:
                genome = yaml.safe_load(fh)
                genomes.append({
                    "name": f.replace(".yaml", ""),
                    "yaml": yaml.dump(genome, allow_unicode=True),
                })
    return genomes


def run_single_stock(code: str):
    """Run full pipeline on a single stock — for testing/debugging."""
    print(f"\n{'='*60}")
    print(f"🔬 Running full pipeline on {code}")
    print(f"{'='*60}")

    db = EvaluationDB()
    db.init_db()
    db.migrate_v2_1()
    db.migrate_committee_decisions_v2_1_1()
    db.migrate_v2_3()

    provider = DataProvider()
    fin = get_financial_provider()

    # ── 1. Fetch data ────────────────────────────────────
    print(f"\n1. Fetching data for {code}...")

    # Quote
    snap = provider.get_stock_snapshot(code)
    print(f"   Stock: {snap.name}, Price: {snap.price}, PE: {snap.pe_ttm}, PB: {snap.pb}")

    # Financials
    fin_data = fin.get_financial_dict(code)
    print(f"   ROE: {fin_data.get('roe')}, Gross Margin: {fin_data.get('gross_margin')}")
    print(f"   Revenue Growth: {fin_data.get('revenue_growth_1y')}, Net Margin: {fin_data.get('net_margin')}")

    if not fin_data.get("roe"):
        print("   ⚠️ No financial data available — mootdx may not be installed or data unavailable")
        print("   Install: pip install mootdx")
        print("   Falling back to mock financial data...")
        fin_data = {
            "roe": 0.30, "roic": 0.25, "gross_margin": 0.92, "net_margin": 0.52,
            "pe_ttm": snap.pe_ttm, "pb": snap.pb, "debt_to_equity": 0.3,
            "revenue_growth_1y": 0.15, "earnings_growth_1y": 0.18,
        }

    # Price history — try real K-line first, fallback to mock
    from src.data.provider import MarketDataProvider
    mkt = MarketDataProvider()
    price_data = mkt.get_daily_kline(
        code,
        start_date=(date.today() - timedelta(days=365)).isoformat(),
        end_date=date.today().isoformat(),
    )
    if price_data.empty:
        print("   ⚠️ No K-line data — using mock prices")
        dates = pd.date_range(end=date.today(), periods=120, freq='B')
        if snap.price:
            price_data = pd.DataFrame({
                'date': dates,
                'close': snap.price * (1 + np.random.normal(0.0005, 0.02, 120)).cumprod(),
                'volume': np.random.uniform(1e6, 1e8, 120),
            })
        else:
            price_data = pd.DataFrame({
                'date': dates,
                'close': 100 * (1 + np.random.normal(0.001, 0.02, 120)).cumprod(),
                'volume': np.random.uniform(1e6, 1e7, 120),
            })
    else:
        print(f"   K-line: {len(price_data)} bars loaded")

    # ── 2. Factor computation ────────────────────────────
    print("\n2. Computing factors...")
    fe = FactorEngine()
    factors = fe.compute_single_stock(code, fin_data, price_data)
    print(f"   Quality: {factors.quality_score:.0f}, Value: {factors.value_score:.0f}")
    print(f"   Growth: {factors.growth_score:.0f}, Momentum: {factors.momentum_score:.0f}")
    print(f"   Risk: {factors.risk_score:.0f}")

    # ── 3. Market state ──────────────────────────────────
    print("\n3. Market state...")
    mb = MarketBrain()
    # Use real index K-line if available, fallback mock
    idx_data = mkt.get_daily_kline('000905',  # CSI 500 works with mootdx
        start_date=(date.today() - timedelta(days=365)).isoformat(),
        end_date=date.today().isoformat())
    if idx_data.empty:
        idx_data = mkt.get_daily_kline('000852',
            start_date=(date.today() - timedelta(days=365)).isoformat(),
            end_date=date.today().isoformat())
    if idx_data.empty:
        idx_data = pd.DataFrame({
            'date': pd.date_range(end=date.today(), periods=120, freq='B'),
            'close': 3500 * (1 + np.random.normal(0.0003, 0.015, 120)).cumprod(),
            'volume': np.random.uniform(1e10, 1e11, 120),
        })
        print("   Market index: mock (no real data)")
    else:
        print(f"   Market index: {len(idx_data)} bars loaded")
    regime = mb.classify(idx_data)
    market_dict = {
        "regime_type": regime.regime_type,
        "risk_score": regime.risk_score,
        "market_pe_percentile": 0.55,
    }
    print(f"   Regime: {regime.regime_type}, Risk: {regime.risk_score:.0f}")

    # ── 4. Research Agents ───────────────────────────────
    print("\n4. Research Agents analyzing...")
    genomes = load_genomes()
    analyses = []

    for g in genomes[:4]:  # Use first 4 agents for speed
        agent = ResearchAgent(g["yaml"])
        from src.data import MarketSnapshot, StockSnapshot
        market_snap = MarketSnapshot(
            date=date.today().isoformat(),
            regime_type=regime.regime_type,
            risk_score=regime.risk_score,
        )
        stock_snap = StockSnapshot(
            code=code,
            name=snap.name or code,
            price=snap.price,
            pe_ttm=snap.pe_ttm,
            pb=snap.pb,
            float_mcap=snap.float_mcap,
        )
        sa = agent.analyze(market_snap, stock_snap, factors)
        if sa and sa.alpha_score > 3:
            analyses.append(sa)
            print(f"   {g['name']}: α={sa.alpha_score:.1f}, conf={sa.confidence:.1f}, thesis={sa.thesis.pattern if sa.thesis else 'N/A'}")

    if not analyses:
        print("   ⚠️ No agent produced a valid analysis (all alpha ≤ 3)")
        return

    # ── 5. Save to DB ────────────────────────────────────
    print("\n5. Saving to database...")

    best = max(analyses, key=lambda x: x.alpha_score)
    decision_hash = hashlib.sha256(
        f"{best.agent_id}|{code}|{datetime.now().isoformat()}|{best.alpha_score}".encode()
    ).hexdigest()[:16]
    input_hash = hashlib.sha256(f"{code}|{datetime.now().isoformat()}".encode()).hexdigest()[:16]

    rid = db.insert_research_decision(
        agent_id=best.agent_id,
        genome_hash=best.decision_fingerprint.get("genome_hash", "unknown"),
        security_id=code,
        thesis={
            "thesis_id": best.thesis.thesis_id if best.thesis else f"auto_{code}",
            "family": best.thesis.family if best.thesis else "value",
            "pattern": best.thesis.pattern if best.thesis else "unknown",
            "claim": best.thesis.claim if best.thesis else "",
            "evidence": best.thesis.evidence if best.thesis else [],
            "catalyst": best.thesis.catalyst if best.thesis else "",
            "invalidation": best.thesis.invalidation if best.thesis else [],
            "horizon": best.thesis.horizon if best.thesis else "12_months",
        },
        alpha_score=best.alpha_score,
        confidence=best.confidence,
        factor_snapshot={
            "quality": factors.quality_score,
            "value": factors.value_score,
            "growth": factors.growth_score,
            "momentum": factors.momentum_score,
            "risk": factors.risk_score,
        },
        risk_assessment=best.risk_assessment,
        entry_price=snap.price or 100,
        entry_date=date.today().isoformat(),
        decision_hash=decision_hash,
        input_hash=input_hash,
    )
    print(f"   ✅ research_decision id={rid}")

    # ── 6. Thesis Validator ──────────────────────────────
    print("\n6. Thesis Validator...")
    registry = RuleRegistry(db)
    validator = ThesisValidator(db, registry)
    val_result = validator.validate(rid)
    print(f"   Verdict: {val_result.verdict}, Overall: {val_result.overall_score:.0f}/100")
    print(f"   Evidence: {val_result.evidence_score:.0f}, Counter-risk: {val_result.counter_evidence_risk:.0f}")
    print(f"   Effective confidence: {best.confidence} → {val_result.effective_confidence}")

    # ── 7. Investment Committee ──────────────────────────
    print("\n7. Investment Committee...")
    committee = CommitteeAgent(db)
    decision = committee.review(
        best, val_result, market_dict,
        {"sector_weights": {}, "positions": []}
    )
    print(f"   Verdict: {decision.verdict} (ws={decision.weighted_score:.1f})")
    print(f"   Valuation:{decision.valuation_score:.0f} Industry:{decision.industry_score:.0f} Risk:{decision.risk_score:.0f}")
    print(f"   Quant:{decision.quant_score:.0f} Devil:{decision.devil_advocate_score:.0f}")
    print(f"   Position cap: {decision.position_cap_modifier}, Confidence mod: {decision.confidence_modifier}")

    # Save committee decision using built-in helper
    db.insert_committee_decision(decision)
    print("   ✅ Committee decision saved")

    # ── 8. Portfolio ─────────────────────────────────────
    print("\n8. Portfolio...")
    pa = PortfolioAgent()
    state = PortfolioState()
    pdec = pa.construct_portfolio([best], state, market_dict)
    # Apply committee position cap modifier
    pdec.apply_committee_cap(getattr(decision, "position_cap_modifier", 1.0))

    if pdec.decisions:
        d = pdec.decisions[0]
        print(f"   Action: {d.action}, Target weight: {d.target_weight:.4f}")

        # Persist portfolio decision
        pd_id = db.insert_portfolio_decision(
            research_decision_id=rid,
            agent_id=best.agent_id,
            decision=pdec,
            market_regime=market_dict.get("regime_type"),
            market_risk_score=market_dict.get("risk_score"),
            decision_date=date.today().isoformat(),
        )
        print(f"   ✅ portfolio_decision id={pd_id}")

        # Simulate order execution
        sim = ExecutionSimulator()
        port_value = state.cash_balance
        target_value = port_value * d.target_weight
        order_qty = int(target_value / (snap.price or 100) / 100) * 100  # round to lots
        if order_qty > 0:
            exec_result = sim.simulate_portfolio_decision(
                decision={
                    "stock_code": code,
                    "action": d.action if d.action in ("BUY", "ADD", "SELL", "REDUCE") else "BUY",
                    "quantity": order_qty,
                    "portfolio_decision_id": pd_id,
                },
                current_price=snap.price or 100,
            )
            # Persist execution
            db.insert_portfolio_execution(
                portfolio_decision_id=pd_id,
                research_decision_id=rid,
                agent_id=best.agent_id,
                execution_result=exec_result,
            )
            fp = exec_result.get("fill_price", 0)
            tc = exec_result.get("total_cost", 0)
            print(f"   📊 Fill: ¥{fp:.2f} × {order_qty} shares | Cost: ¥{tc:.2f}")
            print(f"      Slippage: {exec_result.get('slippage', 0):.4%} | Mode: {exec_result.get('execution_mode', 'PAPER')}")

    # ── 9. Evaluation + Post-Mortem ──────────────────────
    print("\n9. Evaluation & Post-Mortem...")
    try:
        from src.evaluation.evaluation_engine import EvaluationEngine
        eval_engine = EvaluationEngine(db)
        eval_result = eval_engine.evaluate(rid, horizon_days=1)  # Immediate eval for demo
        if eval_result:
            eval_engine.save_to_db(eval_result)
            print(f"   Return: {eval_result.gross_return:+.2%}, Alpha: {eval_result.alpha_vs_market:+.2%}")

        from src.postmortem.engine import PostMortemEngine
        pm = PostMortemEngine()
        pm_count = pm.run_daily()
        print(f"   Post-mortem: {pm_count} patterns")
    except Exception as e:
        logger.warning("runner: evaluation/post-mortem skipped: %s", e)

    print(f"\n{'='*60}")
    print(f"✅ Pipeline complete for {code} ({snap.name or code})")
    print("   View results: streamlit run src/ui/app.py")
    print(f"{'='*60}")


def run_batch(codes: list[str], top_n: int = 10):
    """Run pipeline on multiple stocks."""
    print(f"\n{'='*60}")
    print(f"🔬 Running batch pipeline on {len(codes)} stocks")
    print(f"{'='*60}")

    db = EvaluationDB()
    db.init_db()
    db.migrate_v2_1()
    db.migrate_committee_decisions_v2_1_1()
    db.migrate_v2_3()

    results = []
    for code in codes:
        try:
            print(f"\n--- {code} ---")
            # Simplified: just run single stock and collect results
            # In production, this would batch queries for efficiency
            provider = DataProvider()
            snap = provider.get_stock_snapshot(code)
            if snap.name:
                results.append({"code": code, "name": snap.name, "pe": snap.pe_ttm})
                print(f"   {snap.name}: PE={snap.pe_ttm}, PB={snap.pb}")
            else:
                print("   ⚠️ No data")
        except Exception as e:
            logger.warning("runner: batch fetch failed for %s: %s", code, e)

    print(f"\n✅ Batch complete: {len(results)}/{len(codes)} stocks fetched")
    print("   Run single stock analysis: python -m src.runner --code 600519")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Sieve Daily Runner")
    parser.add_argument("--code", "-c", help="Single stock code to analyze")
    parser.add_argument("--universe", "-u", nargs="*", help="Stock universe (codes)")
    parser.add_argument("--top", "-n", type=int, default=10, help="Top N picks")
    parser.add_argument("--sample", action="store_true", help="Use sample universe")
    args = parser.parse_args()

    try:
        if args.code:
            run_single_stock(args.code)
        elif args.universe:
            run_batch(args.universe, args.top)
        elif args.sample:
            run_batch(get_universe(), args.top)
        else:
            print("Stock Sieve Runner")
            print("  --code 600519     Analyze single stock")
            print("  --sample          Run on sample universe (10 blue chips)")
            print("  --universe 600519 000858   Run on custom list")
    finally:
        close_all()  # Flush all managed sqlite connections at pipeline end.
