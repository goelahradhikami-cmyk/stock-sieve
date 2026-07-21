"""
Backfill Crowding Snapshot - Commit 6-S.17.4 (v3.5.1 Phase 1).

Populates the crowding_snapshot table for Group A (FRM-only) candidates.
This is the diagnostic data layer that Exp9/10/11 will consume to
distinguish H3-A (uncertainty premium) from H3-B (crowding avoidance).

Strategy:
  1. For each Group A candidate (security_id, trade_date), compute
     4 crowding feature groups from TDX kline BEFORE trade_date.
  2. Store RAW features (not just composite) for post-hoc decomposition.
  3. Compute cross-sectional crowding_score_v1 after all raw features
     are stored (zscore sum, diagnostic only).

Vintage safety (CRITICAL):
  All features from data BEFORE trade_date. No lookahead. Uses same
  LocalDataProvider source as event_reaction.py.

Usage:
    python scripts/backfill_crowding.py                # Group A
    python scripts/backfill_crowding.py --code 600519  # single stock
    python scripts/backfill_crowding.py --validate     # coverage + distribution
"""

from __future__ import annotations

import os
import sys
import sqlite3
import argparse

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.crowding_calculator import CrowdingCalculator
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations", "crowding_snapshot.sql",
)


def apply_schema(cache: sqlite3.Connection) -> None:
    """Apply crowding_snapshot.sql (idempotent)."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        cache.executescript(f.read())
    cache.commit()


def backfill_group_a() -> None:
    """Backfill crowding features for Group A (FRM-only) candidates."""
    calc = CrowdingCalculator(cache_db=CACHE_DB)
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row
    cache = sqlite3.connect(CACHE_DB)
    apply_schema(cache)

    candidates = shadow.execute(
        """SELECT DISTINCT security_id, trade_date
           FROM shadow_candidates_v3
           WHERE residual_alpha IS NOT NULL AND rs_data_available = 0
           ORDER BY trade_date"""
    ).fetchall()
    print(f"Group A candidates to process: {len(candidates)}", flush=True)

    written = 0
    skipped = 0
    insufficient = 0
    batch = []

    for i, cand in enumerate(candidates):
        code = cand["security_id"]
        trade_date = cand["trade_date"]

        # Skip if already backfilled
        existing = cache.execute(
            "SELECT 1 FROM crowding_snapshot WHERE security_id = ? AND trade_date = ?",
            (code, trade_date),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        result = calc.compute(code, trade_date)

        # Check if we got enough features
        feature_count = sum(1 for f in [result.return_20d, result.return_60d,
                                         result.realized_vol_20d, result.volume_ratio,
                                         result.abnormal_volume, result.price_gap]
                           if f is not None)
        if feature_count < 3:
            insufficient += 1
            continue

        # Compute turnover_percentile (cross-sectional)
        result.turnover_percentile = calc.compute_turnover_percentile(code, trade_date)

        batch.append(_result_to_tuple(result))
        written += 1

        if len(batch) >= 50:
            _commit_batch(cache, batch)
            print(f"  ... {i+1}/{len(candidates)} processed, {written} written", flush=True)

    _commit_batch(cache, batch)

    # Compute cross-sectional crowding_score_v1 (zscore sum)
    _compute_composite_scores(cache)

    cache.commit()
    cache.close()
    shadow.close()

    print(f"\n=== Backfill Complete ===", flush=True)
    print(f"  candidates:    {len(candidates)}", flush=True)
    print(f"  written:       {written}", flush=True)
    print(f"  skipped:       {skipped} (already backfilled)", flush=True)
    print(f"  insufficient:  {insufficient} (< 3 features)", flush=True)


def _result_to_tuple(result) -> tuple:
    return (
        result.security_id, result.trade_date,
        result.return_20d, result.return_60d,
        result.turnover_percentile, result.volume_ratio,
        result.realized_vol_20d,
        result.abnormal_volume, result.price_gap,
        result.market_cap, result.float_mcap,
        None,  # crowding_score_v1 computed in _compute_composite_scores
    )


def _commit_batch(cache: sqlite3.Connection, batch: list) -> None:
    if not batch:
        return
    cache.executemany(
        """INSERT OR REPLACE INTO crowding_snapshot
           (security_id, trade_date,
            return_20d, return_60d, turnover_percentile, volume_ratio,
            realized_vol_20d, abnormal_volume, price_gap,
            market_cap, float_mcap, crowding_score_v1)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        batch,
    )
    cache.commit()
    batch.clear()


def _compute_composite_scores(cache: sqlite3.Connection) -> None:
    """Compute cross-sectional crowding_score_v1 (equal-weight zscore sum).

    Diagnostic only. Not a production score. Uses zscore of:
      turnover_percentile, return_20d, realized_vol_20d, abnormal_volume
    """
    rows = cache.execute(
        """SELECT id, turnover_percentile, return_20d,
                  realized_vol_20d, abnormal_volume
           FROM crowding_snapshot"""
    ).fetchall()

    if not rows:
        return

    # Collect non-null arrays for zscore computation
    ids = []
    features = {f: [] for f in ["turnover", "ret20", "vol", "abnvol"]}
    for r in rows:
        ids.append(r[0])
        features["turnover"].append(r[1])
        features["ret20"].append(r[2])
        features["vol"].append(r[3])
        features["abnvol"].append(r[4])

    def zscore(vals):
        arr = np.array([v if v is not None else np.nan for v in vals], dtype=float)
        mean = np.nanmean(arr)
        std = np.nanstd(arr)
        if std == 0 or np.isnan(std):
            return np.zeros(len(arr))
        return (arr - mean) / std

    z_turnover = zscore(features["turnover"])
    z_ret20 = zscore(features["ret20"])
    z_vol = zscore(features["vol"])
    z_abnvol = zscore(features["abnvol"])

    # Composite = sum of available zscores (nan -> 0)
    for i, id_ in enumerate(ids):
        score = 0.0
        count = 0
        for z in [z_turnover[i], z_ret20[i], z_vol[i], z_abnvol[i]]:
            if not np.isnan(z):
                score += z
                count += 1
        # Normalize by count (average zscore, not sum, for comparability)
        composite = score / count if count > 0 else None
        cache.execute(
            "UPDATE crowding_snapshot SET crowding_score_v1 = ? WHERE id = ?",
            (float(composite) if composite is not None else None, id_),
        )

    cache.commit()
    print(f"  Computed crowding_score_v1 for {len(ids)} rows", flush=True)


def validate_distribution() -> None:
    """Coverage + distribution report for crowding_snapshot."""
    cache = sqlite3.connect(CACHE_DB)
    cache.row_factory = sqlite3.Row
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row

    print("=" * 70, flush=True)
    print("CROWDING SNAPSHOT VALIDATION", flush=True)
    print("=" * 70, flush=True)

    total = cache.execute("SELECT COUNT(*) FROM crowding_snapshot").fetchone()[0]
    print(f"\n  Total crowding_snapshot rows: {total}", flush=True)

    if total == 0:
        print("  (table empty - run scripts/backfill_crowding.py first)", flush=True)
        return

    # Feature coverage
    features = [
        ("return_20d", "return_20d IS NOT NULL"),
        ("return_60d", "return_60d IS NOT NULL"),
        ("turnover_percentile", "turnover_percentile IS NOT NULL"),
        ("volume_ratio", "volume_ratio IS NOT NULL"),
        ("realized_vol_20d", "realized_vol_20d IS NOT NULL"),
        ("abnormal_volume", "abnormal_volume IS NOT NULL"),
        ("price_gap", "price_gap IS NOT NULL"),
        ("market_cap", "market_cap IS NOT NULL"),
        ("crowding_score_v1", "crowding_score_v1 IS NOT NULL"),
    ]
    print(f"\n  Feature coverage:", flush=True)
    for label, cond in features:
        n = cache.execute(
            f"SELECT COUNT(*) FROM crowding_snapshot WHERE {cond}"
        ).fetchone()[0]
        pct = 100.0 * n / total if total else 0
        print(f"    {label:25s}: {n:3d} / {total} ({pct:5.1f}%)", flush=True)

    # Distribution of key features
    print(f"\n  Feature distributions:", flush=True)
    for label, col in [("return_20d", "return_20d"), ("return_60d", "return_60d"),
                        ("realized_vol_20d", "realized_vol_20d"),
                        ("volume_ratio", "volume_ratio"),
                        ("abnormal_volume", "abnormal_volume"),
                        ("crowding_score_v1", "crowding_score_v1")]:
        row = cache.execute(
            f"""SELECT AVG({col}) AS mean, MIN({col}) AS min, MAX({col}) AS max,
                       COUNT({col}) AS n
                FROM crowding_snapshot WHERE {col} IS NOT NULL"""
        ).fetchone()
        if row and row[3] > 0:
            print(f"    {label:25s}: mean={row[0]:.4f}  min={row[1]:.4f}  max={row[2]:.4f}  n={row[3]}",
                  flush=True)

    # Group A coverage
    ga = shadow.execute(
        "SELECT COUNT(DISTINCT security_id || '|' || trade_date) FROM shadow_candidates_v3 "
        "WHERE residual_alpha IS NOT NULL AND rs_data_available = 0"
    ).fetchone()[0]
    covered = 0
    ga_rows = shadow.execute(
        "SELECT DISTINCT security_id, trade_date FROM shadow_candidates_v3 "
        "WHERE residual_alpha IS NOT NULL AND rs_data_available = 0"
    ).fetchall()
    for r in ga_rows:
        exists = cache.execute(
            "SELECT 1 FROM crowding_snapshot WHERE security_id = ? AND trade_date = ?",
            (r["security_id"], r["trade_date"]),
        ).fetchone()
        if exists:
            covered += 1
    print(f"\n  Group A coverage: {covered} / {ga} ({100.0*covered/ga:.1f}%)" if ga else "",
          flush=True)

    # crowding_score_v1 quintile vs alpha (preview for Exp9)
    print(f"\n  PREVIEW: crowding_score_v1 quintile vs residual_alpha", flush=True)
    print(f"    (full analysis in Exp9, this is a sanity check)", flush=True)
    # Join to shadow_candidates_v3 for residual_alpha
    rows = []
    for r in ga_rows:
        cs = cache.execute(
            "SELECT crowding_score_v1 FROM crowding_snapshot "
            "WHERE security_id = ? AND trade_date = ?",
            (r["security_id"], r["trade_date"]),
        ).fetchone()
        ra = shadow.execute(
            "SELECT residual_alpha FROM shadow_candidates_v3 "
            "WHERE security_id = ? AND trade_date = ? AND residual_alpha IS NOT NULL "
            "AND rs_data_available = 0 LIMIT 1",
            (r["security_id"], r["trade_date"]),
        ).fetchone()
        if cs and cs[0] is not None and ra and ra[0] is not None:
            rows.append((cs[0], ra[0]))

    if len(rows) >= 10:
        rows.sort(key=lambda x: x[0])
        n = len(rows)
        q = n // 5
        print(f"    {'quintile':10s} {'n':>4} {'crowding_range':>20} {'alpha':>8} {'positive':>9}", flush=True)
        for qi in range(5):
            start = qi * q
            end = (qi + 1) * q if qi < 4 else n
            sub = rows[start:end]
            if not sub:
                continue
            alphas = [a for _, a in sub]
            crowds = [c for c, _ in sub]
            pos = 100.0 * sum(1 for a in alphas if a > 0) / len(alphas)
            print(f"    Q{qi+1:1d}         {len(sub):4d}  [{min(crowds):+.3f},{max(crowds):+.3f}]   "
                  f"{np.mean(alphas)*100:+6.2f}%  {pos:7.1f}%", flush=True)

    cache.close()
    shadow.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill crowding_snapshot table")
    parser.add_argument("--code", help="Single stock code (debugging)")
    parser.add_argument("--validate", action="store_true", help="Run validation report only")
    args = parser.parse_args()

    if args.validate:
        validate_distribution()
    elif args.code:
        calc = CrowdingCalculator(cache_db=CACHE_DB)
        cache = sqlite3.connect(CACHE_DB)
        apply_schema(cache)
        shadow = sqlite3.connect(SHADOW_DB)
        shadow.row_factory = sqlite3.Row
        r = shadow.execute(
            "SELECT trade_date FROM shadow_candidates_v3 WHERE security_id = ? "
            "AND residual_alpha IS NOT NULL AND rs_data_available = 0 LIMIT 1",
            (args.code,),
        ).fetchone()
        if not r:
            print(f"No Group A candidate for {args.code}", flush=True)
            return
        result = calc.compute(args.code, r["trade_date"])
        print(f"\n=== Crowding for {args.code} @ {r['trade_date']} ===", flush=True)
        print(f"  return_20d:        {result.return_20d}", flush=True)
        print(f"  return_60d:        {result.return_60d}", flush=True)
        print(f"  realized_vol_20d:  {result.realized_vol_20d}", flush=True)
        print(f"  volume_ratio:      {result.volume_ratio}", flush=True)
        print(f"  abnormal_volume:   {result.abnormal_volume}", flush=True)
        print(f"  price_gap:         {result.price_gap}", flush=True)
        print(f"  market_cap:        {result.market_cap}", flush=True)
    else:
        backfill_group_a()
        validate_distribution()


if __name__ == "__main__":
    main()
