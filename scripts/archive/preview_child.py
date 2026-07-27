"""
Child Agent Preview Lab — Compare child vs parent using real historical K-line.

Usage:
    python scripts/preview_child.py momentum_gen1_4727 momentum_chaser_v1 2026-06-15
"""

import os
import sqlite3
import sys
from datetime import date, timedelta

import numpy as np

from src.data.provider import MarketDataProvider
import contextlib


def preview_child_vs_parent(child_id, parent_id, signal_date_str):
    provider = MarketDataProvider()

    # Connect to evaluation.db (relative to project root; this script lives in scripts/)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(project_root, "data", "evaluation.db")
    db = sqlite3.connect(db_path)

    signal_date = date.fromisoformat(signal_date_str)
    eval_date = signal_date + timedelta(days=20)

    # 1. Load genomes
    child_row = db.execute(
        "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id=? AND status='candidate'",
        (child_id,),
    ).fetchone()
    if not child_row:
        # Try active/frozen
        child_row = db.execute(
            "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id=? ORDER BY birth_date DESC LIMIT 1",
            (child_id,),
        ).fetchone()
    parent_row = db.execute(
        "SELECT genome_yaml FROM agent_genome_snapshots WHERE agent_id=? ORDER BY birth_date DESC LIMIT 1",
        (parent_id,),
    ).fetchone()

    if not child_row:
        print(f"❌ Child not found: {child_id}")
        print(
            '   Run evolution first: python -c "from src.evolution.engine_v1 import EvolutionEngineV1; EvolutionEngineV1(dry_run=False).run_cycle()"'
        )
        return
    if not parent_row:
        print(f"❌ Parent not found: {parent_id}")
        return

    import yaml

    child_genome = yaml.safe_load(child_row[0]) or {}
    parent_genome = yaml.safe_load(parent_row[0]) or {}

    # 2. Genome Diff
    print("=" * 70)
    print("GENOME DIFF（因子权重变化）")
    print("=" * 70)
    child_fm = child_genome.get("factor_model", {})
    parent_fm = parent_genome.get("factor_model", {})
    diff_count = 0
    for family in ["quality", "value", "growth", "momentum", "risk", "sentiment"]:
        pw = (
            parent_fm.get(family, {}).get("weight", 0)
            if isinstance(parent_fm.get(family), dict)
            else 0
        )
        cw = (
            child_fm.get(family, {}).get("weight", 0)
            if isinstance(child_fm.get(family), dict)
            else 0
        )
        if abs(cw - pw) > 0.001:
            diff_count += 1
            print(f"  {family}: {pw:.3f} → {cw:.3f} (Δ{cw - pw:+.3f})")
    if diff_count == 0:
        print("  ⚠️ 无因子权重变化！Mutation 可能未生效")

    # Identity diff
    child_iden = child_genome.get("investment_identity", {}).get("dimensions", {})
    parent_iden = parent_genome.get("investment_identity", {}).get("dimensions", {})
    print("\nIDENTITY DIFF（投资风格偏移）")
    for dim in [
        "valuation",
        "quality",
        "growth",
        "momentum",
        "macro",
        "contrarian",
        "patience",
        "concentration",
    ]:
        pv = parent_iden.get(dim, 50)
        cv = child_iden.get(dim, 50)
        if abs(cv - pv) > 2:
            print(f"  {dim}: {pv} → {cv} (Δ{cv - pv:+d})")

    # 3. Stock universe — use built-in list if snapshot unavailable
    stocks = None
    with contextlib.suppress(Exception):
        stocks = db.execute(
            "SELECT security_id FROM universe_snapshot WHERE trade_date=? LIMIT 100",
            (signal_date.isoformat(),),
        ).fetchall()

    if not stocks:
        from src.data.security_master import SecurityMaster

        master = SecurityMaster()
        df = master.get_active_universe()
        stock_codes = df["code"].tolist()[:50]
        stock_list = [f"{c}.{'SH' if c.startswith('6') else 'SZ'}" for c in stock_codes]
    else:
        stock_list = [s[0] for s in stocks[:100]]

    print(f"\n回测参数: {len(stock_list)} stocks | {signal_date} → {eval_date} (20天)")

    # 4. Simulate picks
    parent_picks = simulate_picks(parent_genome, stock_list, provider, signal_date)
    child_picks = simulate_picks(child_genome, stock_list, provider, signal_date)

    # 5. Forward returns with real K-line
    parent_ret = calc_forward_return(
        [s for s, _ in parent_picks[:5]], provider, signal_date, eval_date
    )
    child_ret = calc_forward_return(
        [s for s, _ in child_picks[:5]], provider, signal_date, eval_date
    )

    print("\n" + "=" * 70)
    print("PERFORMANCE COMPARE（top5 等权 20日收益）")
    print("=" * 70)
    print(f"  Parent ({parent_id}): {parent_ret * 100:+.2f}%")
    print(f"  Child  ({child_id}): {child_ret * 100:+.2f}%")
    print(f"  差异: {(child_ret - parent_ret) * 100:+.2f}%")

    # 6. Pick overlap
    parent_top = [s for s, _ in parent_picks[:5]]
    child_top = [s for s, _ in child_picks[:5]]
    overlap = set(parent_top) & set(child_top)
    print(f"\n  Parent top5: {parent_top}")
    print(f"  Child  top5: {child_top}")
    print(f"  重叠: {len(overlap)}/5")
    if len(overlap) >= 5:
        print("  ⚠️ 选股完全一致，行为未变化")

    db.close()
    return parent_ret, child_ret


def simulate_picks(genome, stock_list, provider, signal_date):
    """Score stocks using genome factor weights + real price data."""
    fm = genome.get("factor_model", {})
    weights = {}
    for family, config in fm.items():
        if isinstance(config, dict):
            weights[family] = config.get("weight", 0)

    scores = []
    for sid in stock_list:
        code = sid.split(".")[0] if "." in sid else sid
        try:
            kline = provider.get_daily_kline(code, signal_date.isoformat(), signal_date.isoformat())
            if kline.empty:
                continue

            # Compute simple factor scores from K-line
            close = kline["close"].values if "close" in kline.columns else []
            if len(close) < 5:
                continue

            # Momentum
            mom_1m = (close[-1] / close[-min(21, len(close))] - 1) if len(close) >= 21 else 0
            # Volatility
            rets = np.diff(close[-20:]) / close[-21:-1] if len(close) >= 21 else [0]
            vol = np.std(rets) * np.sqrt(252) if len(rets) > 1 else 0.25
            # PE (from Tencent)
            pe = 20  # default

            total = 0
            total += weights.get("momentum", 0) * (mom_1m * 100)
            total += weights.get("quality", 0) * 50  # placeholder
            total += weights.get("value", 0) * (50 if pe < 20 else 30)
            total += weights.get("growth", 0) * 50
            total -= weights.get("risk", 0) * (vol * 100)
            total += weights.get("sentiment", 0) * 50

            scores.append((sid, total))
        except Exception:
            continue

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores


def calc_forward_return(picks, provider, signal_date, eval_date):
    """Equal-weight forward return using real adj_close."""
    if not picks:
        return 0
    returns = []
    for sid in picks:
        code = sid.split(".")[0] if "." in sid else sid
        try:
            kline = provider.get_daily_kline(code, signal_date.isoformat(), eval_date.isoformat())
            if not kline.empty and len(kline) >= 2:
                close_col = "adj_close" if "adj_close" in kline.columns else "close"
                start_p = float(kline.iloc[0][close_col])
                end_p = float(kline.iloc[-1][close_col])
                if start_p > 0:
                    returns.append((end_p - start_p) / start_p)
        except Exception:
            pass
    return float(np.mean(returns)) if returns else 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child_id = sys.argv[1] if len(sys.argv) > 1 else "momentum_gen1_4727"
    parent_id = sys.argv[2] if len(sys.argv) > 2 else "momentum_chaser_v1"
    signal_date = sys.argv[3] if len(sys.argv) > 3 else "2026-06-15"
    preview_child_vs_parent(child_id, parent_id, signal_date)
