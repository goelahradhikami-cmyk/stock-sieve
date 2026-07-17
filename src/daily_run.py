"""
Daily Runner Script — Automates: sync → screen → evaluate → post-mortem.

Usage:
    python -m src.daily_run
    python -m src.daily_run --sample 5    # Screen top 5 stocks
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import sqlite3
from datetime import date, timedelta

import yaml

from src.agents.committee_agent import CommitteeAgent
from src.agents.portfolio_agent import PortfolioAgent, PortfolioState
from src.agents.research_agent import ResearchAgent
from src.data.db import close_all
from src.data.evaluation_db import EvaluationDB
from src.data.financials import FinancialDataProvider
from src.data.index_provider import IndexDataProvider
from src.data.local_provider import LocalDataProvider
from src.data.market_brain import MarketBrain
from src.data.provider import DataProvider, MarketDataProvider
from src.data.security_master import SecurityMaster
from src.data.universe_filter import UniverseFilter
from src.evaluation.batch_runner import BatchEvaluationRunner
from src.factors.engine import FactorEngine
from src.postmortem.engine import PostMortemEngine
from src.utils.logger import get_logger
from src.validation.rule_registry import RuleRegistry
from src.validation.thesis_validator import ThesisValidator

logger = get_logger(__name__)


def load_genomes():
    config_dir = os.path.join(os.path.dirname(__file__), "..", "config", "personalities")
    genomes = []
    for f in sorted(os.listdir(config_dir)):
        if f.endswith(".yaml"):
            with open(os.path.join(config_dir, f), encoding="utf-8") as fh:
                genome = yaml.safe_load(fh)
                genomes.append({"name": f.replace(".yaml", ""), "yaml": yaml.dump(genome, allow_unicode=True)})
    return genomes


# Per-stock analysis is O(agents). Active agents accumulate every evolution
# round, so cap how many participate per stock. 0 = no cap (use all).
MAX_ACTIVE_AGENTS = 12


def load_active_agents(db, max_agents: int = MAX_ACTIVE_AGENTS):
    """Load active agent genomes from the evolution lineage (agent_genome_snapshots).

    Single source of truth for which agents participate in the per-stock pipeline.
    Founders are seeded into the DB by _seed_founders(); evolved children are written
    there by engine_v1._activate_agent(). Reading from the DB closes the
    evolution -> decision loop so that children actually analyze stocks.
    Falls back to the static YAML configs only if the DB has no active agents.

    When more than `max_agents` are active, apply a budget that balances
    exploitation (agents with the best historical alpha) and exploration
    (newly evolved children that have no track record yet). Reserving
    exploration slots is essential: otherwise fresh children — which start
    with zero evaluations — would always rank last and never accumulate the
    track record they need to survive the next evolution cycle.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT agent_id, genome_yaml, birth_date FROM agent_genome_snapshots WHERE status='active'"
        ).fetchall()
        if not rows:
            return load_genomes()
        if max_agents <= 0 or len(rows) <= max_agents:
            return [{"name": r[0], "yaml": r[1]} for r in rows]

        # Historical performance per agent (avg alpha + sample count).
        perf = {}
        for aid, _, _ in rows:
            pr = conn.execute(
                """SELECT AVG(alpha_vs_market), COUNT(*) FROM evaluation_results
                   WHERE research_decision_id IN (
                       SELECT id FROM research_decisions WHERE agent_id = ?
                   )""",
                (aid,),
            ).fetchone()
            perf[aid] = (pr[0] if pr and pr[0] is not None else None, pr[1] if pr else 0)
    finally:
        conn.close()

    evaluated = [r for r in rows if perf[r[0]][1] > 0]
    unevaluated = [r for r in rows if perf[r[0]][1] == 0]
    evaluated.sort(key=lambda r: perf[r[0]][0], reverse=True)          # best alpha first
    unevaluated.sort(key=lambda r: (r[2] or ""), reverse=True)          # newest children first

    explore_slots = max(1, max_agents // 4)                             # reserve ~25% for exploration
    chosen = evaluated[: max(0, max_agents - explore_slots)]
    chosen += unevaluated[: max_agents - len(chosen)]
    if len(chosen) < max_agents:                                        # few children -> backfill with proven agents
        chosen += [r for r in evaluated if r not in chosen][: max_agents - len(chosen)]

    return [{"name": r[0], "yaml": r[1]} for r in chosen]


def _compute_market_regime(idx_905, idx_852, mb):
    """Market regime via MarketBrain, with graceful offline fallback.

    Returns (regime_type, risk_score, pe_percentile). When no index data is
    available (offline / provider failure) we fall back to a neutral
    'rotation'/50.0 regime instead of crashing.
    """
    if idx_905 is None or getattr(idx_905, 'empty', True):
        idx_data = idx_852
    else:
        idx_data = idx_905
    if idx_data is None or getattr(idx_data, 'empty', True):
        return ("rotation", 50.0, 0.55)
    regime = mb.classify(idx_data)
    return (
        regime.regime_type,
        float(regime.risk_score),
        regime.market_pe_percentile if regime.market_pe_percentile is not None else 0.55,
    )


def _skip_due_to_missing_data(fin_data, price_data):
    """Decide whether a stock must be skipped because real inputs are missing.

    Previously, missing financials or price series were silently replaced with
    fabricated values (hardcoded roe=0.15, random-walk K-line). That poisoned the
    per-stock factors and, downstream, the evolution engine's fitness scores.
    Now we skip the stock instead so the closed loop only ever trains on real data.

    Real price data is the only *hard* requirement — technical factors (momentum
    / volatility / RSI / volume) are computed from it. Missing financials are NOT
    fabricated: the factor engine leaves the value / quality / growth families at
    a neutral 50 when their raw inputs are None. That is honest (a stock is ranked
    on the factors we actually have) rather than poisoning fitness with guesses.
    """
    if price_data is None or getattr(price_data, "empty", True):
        return True
    return False


def _seed_founders(db):
    """Persist founder genomes to agent_genome_snapshots if not already there."""
    config_dir = os.path.join(os.path.dirname(__file__), "..", "config", "personalities")
    conn = db.connect()
    existing = conn.execute("SELECT COUNT(*) FROM agent_genome_snapshots WHERE status='active'").fetchone()[0]
    if existing >= 8:
        conn.close()
        return

    import hashlib as _hashlib

    import yaml as _yaml
    for f in sorted(os.listdir(config_dir)):
        if not f.endswith('.yaml'):
            continue
        with open(os.path.join(config_dir, f), encoding='utf-8') as fh:
            genome = _yaml.safe_load(fh)
        ident = genome.get('identity', {})
        agent_id = ident.get('agent_id', f.replace('.yaml', ''))
        genome_yaml = _yaml.dump(genome, allow_unicode=True)
        genome_hash = _hashlib.sha256(genome_yaml.encode()).hexdigest()[:16]

        row = conn.execute("SELECT id FROM agent_genome_snapshots WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            db.insert_genome_snapshot(
                agent_id=agent_id, strategy_genus=ident.get('strategy_genus', '?'),
                strategy_species=ident.get('strategy_species', '?'),
                generation=ident.get('generation', 1), parent_agent_id=None,
                genome_hash=genome_hash, genome_yaml=genome_yaml,
                birth_date='2026-07-14', mutation_reason='founder', status='active'
            )
    conn.close()
    count = db.connect().execute("SELECT COUNT(*) FROM agent_genome_snapshots WHERE status='active'").fetchone()[0]
    print(f"   Founders: {count} active")


def daily_run(sample_size: int = 0):
    """Run daily pipeline. sample_size=0 means all stocks in universe."""
    today = date.today()
    print(f"\n{'='*60}")
    print(f"📅 Daily Run — {today.isoformat()}")
    print(f"{'='*60}")

    db = EvaluationDB()
    db.init_db()
    db.migrate_v2_1()
    db.migrate_committee_decisions_v2_1_1()
    db.migrate_v2_3()

    # ── Seed founder genomes if needed ──────────────────
    _seed_founders(db)

    # ── Sync index snapshot ─────────────────────────────
    print("\n1. Index sync...")
    idx = IndexDataProvider()
    idx.sync_all((today - timedelta(days=7)).isoformat(), today.isoformat())

    # ── 2. Universe ─────────────────────────────────────
    print("\n2. Stock universe...")
    master = SecurityMaster()
    raw = master.get_active_universe()
    filt = UniverseFilter(min_avg_amount_20d=0, use_dynamic_liquidity=False)
    universe = filt.filter(raw, today)
    codes = universe['code'].tolist()
    if sample_size > 0:
        codes = codes[:sample_size]
    print(f"   Universe: {len(codes)} stocks")
    # Name fallback: the security_master already stores real names (synced from
    # the local vipdoc + Tencent). Use it so a transient quote miss never drops
    # a stock just because its live snapshot name came back empty.
    name_map = dict(zip(universe['code'].astype(str), universe['name']))

    # ── 3. Per-stock analysis ───────────────────────────
    provider = DataProvider()
    mkt = MarketDataProvider()
    local = LocalDataProvider()   # offline K-line straight from local TDX .day
    fin = FinancialDataProvider()
    fe = FactorEngine()
    genomes = load_active_agents(db)
    print(f"   Active agents (evolution lineage): {len(genomes)}")
    registry = RuleRegistry(db)
    validator = ThesisValidator(db, registry)
    committee = CommitteeAgent(db)

    # ── Market regime (real, via MarketBrain) ───────────
    # Computed once per run (market-wide state). runner.py already does this
    # correctly; daily_run previously hardcoded rotation/50, which fed a fake
    # regime into every research/committee decision.
    mb = MarketBrain()
    idx_905 = mkt.get_daily_kline('000905',
        (today - timedelta(days=365)).isoformat(), today.isoformat())
    idx_852 = mkt.get_daily_kline('000852',
        (today - timedelta(days=365)).isoformat(), today.isoformat())
    regime_type, regime_risk, regime_pe_pct = _compute_market_regime(idx_905, idx_852, mb)
    print(f"   Regime: {regime_type}, Risk: {regime_risk:.0f}")

    total_decisions = 0

    # ── Factor pass: compute raw composites for the whole universe first ──
    # Factors must be computed for every stock *before* scoring, so the
    # cross-sectional normalization (percentile/z_score → family scores) can
    # rank each stock relative to the current universe instead of against an
    # absolute, scale-sensitive value.
    start_date = (today - timedelta(days=365)).isoformat()
    factors_by_code: dict[str, tuple] = {}
    for code in codes:
        try:
            # Price series is the *hard* requirement, and it now comes straight
            # from the local TDX .day files (fully offline, no proxy). The live
            # snapshot (name/PE/PB) is best-effort: a quote miss must NOT drop
            # the stock — we fall back to the security_master name.
            price_data = local.get_daily_kline(code, start_date, today.isoformat())
            if _skip_due_to_missing_data(None, price_data):
                continue

            snap = provider.get_stock_snapshot(code)
            if not snap.name:
                snap.name = name_map.get(str(code), code)
            # Fill last close from local data if the live quote had no price.
            if not snap.price and not price_data.empty:
                snap.price = float(price_data.iloc[-1]["close"])

            fin_data = fin.get_financial_dict(code)
            # Missing financials are NOT fabricated: the factor engine leaves the
            # value/quality/growth families at a neutral 50 when raw inputs are
            # None, so the stock is still ranked on the technical factors we have.

            factors_by_code[code] = (fe.compute_single_stock(code, fin_data, price_data), snap)
        except Exception as e:
            logger.warning("daily_run: factor computation failed for %s: %s", code, e)
            continue

    # ── Cross-sectional normalization across the universe ──
    if factors_by_code:
        fe.compute_cross_sectional([f for f, _ in factors_by_code.values()])
        print(f"   ✅ 横截面标准化完成：{len(factors_by_code)} 只成分股纳入相对评分")

    for code in codes:
        item = factors_by_code.get(code)
        if item is None:
            continue
        factors, snap = item
        from src.data import MarketSnapshot, StockSnapshot
        market_snap = MarketSnapshot(date=today.isoformat(), regime_type=regime_type, risk_score=regime_risk)
        stock_snap = StockSnapshot(code=code, name=snap.name, price=snap.price,
                                   pe_ttm=snap.pe_ttm, pb=snap.pb, float_mcap=snap.float_mcap)

        for g in genomes:  # All active agents from evolution lineage
            try:
                agent = ResearchAgent(g["yaml"])
                sa = agent.analyze(market_snap, stock_snap, factors)
                if not sa or sa.alpha_score < 4:
                    continue

                dh = hashlib.sha256(f"{sa.agent_id}|{code}|{today.isoformat()}".encode()).hexdigest()[:16]
                ih = hashlib.sha256(f"{code}|{today.isoformat()}".encode()).hexdigest()[:16]

                try:
                    rid = db.insert_research_decision(
                        agent_id=sa.agent_id,
                        genome_hash=sa.decision_fingerprint.get("genome_hash", "unknown"),
                        security_id=code,
                        thesis={
                            "thesis_id": f"auto_{code}_{today.isoformat()}",
                            "family": sa.thesis.family if sa.thesis else "value",
                            "pattern": sa.thesis.pattern if sa.thesis else "auto",
                            "claim": sa.thesis.claim[:100] if sa.thesis else "",
                            "evidence": sa.thesis.evidence if sa.thesis else [],
                            "catalyst": "", "invalidation": sa.thesis.invalidation if sa.thesis else [],
                            "horizon": "12_months",
                        },
                        alpha_score=sa.alpha_score, confidence=sa.confidence,
                        factor_snapshot={
                            "quality": factors.quality_score, "value": factors.value_score,
                            "growth": factors.growth_score, "momentum": factors.momentum_score
                        },
                        risk_assessment=sa.risk_assessment,
                        entry_price=snap.price or 100, entry_date=today.isoformat(),
                        decision_hash=dh, input_hash=ih,
                    )
                except sqlite3.IntegrityError:
                    # Idempotent: decision_hash already exists (re-run same day), skip gracefully
                    continue

                # Committee
                val = validator.validate(rid)
                if val.routing_action == "BLOCK":
                    continue

                market_dict = {"regime_type": regime_type, "risk_score": regime_risk, "market_pe_percentile": regime_pe_pct}
                dec = committee.review(sa, val, market_dict, {"sector_weights": {}, "positions": []})
                db.insert_committee_decision(dec)

                if dec.verdict in ("APPROVE", "APPROVE_WITH_CONDITIONS"):
                    pa = PortfolioAgent()
                    pdec = pa.construct_portfolio([sa], PortfolioState(), market_dict)
                    # Save signal snapshot
                    try:
                        from src.evaluation.batch_runner import save_signal_snapshot
                        conn = db.connect()
                        save_signal_snapshot(conn, rid, sa,
                            {"quality": factors.quality_score, "value": factors.value_score},
                            market_regime=regime_type)
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        logger.warning("daily_run: save_signal_snapshot failed for %s: %s", code, e)

                total_decisions += 1

            except Exception as e:
                logger.warning("daily_run: per-agent analysis failed for %s/%s: %s", code, g.get("name", "?"), e)
                continue

    # ── 4. Evaluation backfill ──────────────────────────
    print(f"\n4. T+N Evaluation ({total_decisions} new decisions)...")
    evaluator = BatchEvaluationRunner()
    pending = evaluator.run_pending()

    # ── 5. Post-Mortem ──────────────────────────────────
    print("\n5. Post-Mortem...")
    pm = PostMortemEngine()
    pm_count = pm.run_daily()

    # ── 6. Memory Extraction (Commit 6-E) ────────────────
    print("\n6. Memory Extraction...")
    try:
        from src.memory.extractor import ExperienceExtractor
        mem_ext = ExperienceExtractor()
        mem_count = mem_ext.extract_daily(limit=500)
        print(f"   📚 Memory: {mem_count} experiences extracted")
    except Exception as e:
        mem_count = 0
        logger.warning("daily_run: memory extraction skipped: %s", e)

    # ── 8. Evolution cycle (weekly) ──────────────────────
    print("\n8. Evolution check...")
    try:
        from src.evolution.engine_v1 import EvolutionEngineV1
        from src.evolution.risk_genome import KillSwitch
        ks = KillSwitch()
        if ks.can_evolve():
            engine = EvolutionEngineV1(dry_run=False)
            engine.MIN_SAMPLES = 5
            # Close the loop: feed post-mortem-derived mutations
            # (failure → mutation → evolution) into child genome generation.
            mutations = pm.collect_recent_mutations(lookback_months=6) if pm else []
            evo_result = engine.run_cycle(
                pending_mutations=mutations if mutations else None
            )
            suffix = f" (seeded {len(mutations)} post-mortem mutations)" if mutations else ""
            print(f"   🧬 Evolution: {evo_result.get('new_agents', [])} new, {evo_result.get('eliminated', [])} eliminated{suffix}")
        else:
            print(f"   🛑 Evolution paused (governance: {ks.current_state() if hasattr(ks, 'current_state') else 'N/A'})")
    except Exception as e:
        logger.warning("daily_run: evolution skipped: %s", e)

    # ── 7. Summary ──────────────────────────────────────
    evaluation_count = db.connect().execute("SELECT COUNT(*) FROM evaluation_results").fetchone()[0]
    db.connect().close()

    print(f"\n{'='*60}")
    print("✅ Daily run complete")
    print(f"   Decisions: {total_decisions}")
    print(f"   New evaluations: {pending}")
    print(f"   Total evaluations: {evaluation_count}")
    print(f"   Post-mortem patterns: {pm_count}")
    print(f"{'='*60}")

    close_all()  # Flush all managed sqlite connections at pipeline end.


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Stock Sieve Daily Runner")
    p.add_argument("--sample", "-n", type=int, default=3, help="Number of stocks to screen")
    args = p.parse_args()
    daily_run(args.sample)
