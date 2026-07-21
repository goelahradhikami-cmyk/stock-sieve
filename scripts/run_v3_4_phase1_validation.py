"""
Sustainability Phase 1 Validation - Commit 6-S.16.1 (v3.4 Phase 1).

Two Phase 1 deliverables in one script:

  1. Vintage Leakage Test (BLOCKER)
     Verifies available_date <= as_of_date for every row in
     earnings_sustainability. Any row where available_date > as_of_date
     means future earnings were used -> data integrity failure.
     This must PASS before Phase 1.5 Ablation can run.

  2. Distribution Report
     Counts how many v3 candidates pass each sustainability sub-component,
     to sanity-check filter aggressiveness before the Phase 1.5 alpha test.
     Reports: total / alignment_pass / consistency_pass / margin_pass /
     final_pass, with breakdown by failure_reason.

Does NOT test alpha. That is Phase 1.5 (run_v3_4_sustainability_ablation.py).
Baseline for Phase 1.5 is FRM-only (+2.52%, v3.2.1 Group B), NOT EGE.

Usage:
    python scripts/run_v3_4_phase1_validation.py
    python scripts/run_v3_4_phase1_validation.py --detail   # per-industry breakdown
"""

from __future__ import annotations

import os
import sys
import sqlite3
import argparse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"


def leakage_test(cache: sqlite3.Connection) -> bool:
    """Verify available_date <= as_of_date for every row.

    This is the vintage-safety gate. Any violation means future earnings
    were used to compute a past sustainability signal -> leakage.
    """
    print("=" * 70, flush=True)
    print("TEST 1: Vintage Leakage Test (BLOCKER)", flush=True)
    print("=" * 70, flush=True)

    # Check 1: available_date <= as_of_date (primary vintage gate)
    rows = cache.execute(
        "SELECT security_id, available_date, as_of_date, report_date "
        "FROM earnings_sustainability "
        "WHERE available_date IS NOT NULL AND as_of_date IS NOT NULL "
        "AND available_date > as_of_date"
    ).fetchall()
    violations_primary = len(rows)
    if violations_primary > 0:
        print(f"  FAIL: {violations_primary} rows have available_date > as_of_date", flush=True)
        for r in rows[:5]:
            print(f"    {r[0]}: available={r[1]} as_of={r[2]} report={r[3]}", flush=True)
        return False
    print(f"  Check 1 PASS: 0 rows with available_date > as_of_date", flush=True)

    # Check 2: report_date <= available_date + 200 days (sanity: A-share Q3
    # reports (Sep 30) are often announced in Oct of the FOLLOWING year as
    # part of the annual report cycle - this is normal, not leakage.
    # 200d threshold catches only gross corruption.)
    rows = cache.execute(
        "SELECT security_id, report_date, available_date "
        "FROM earnings_sustainability "
        "WHERE report_date IS NOT NULL AND available_date IS NOT NULL "
        "AND date(report_date, '+200 days') < available_date"
    ).fetchall()
    violations_report = len(rows)
    if violations_report > 10:  # allow a few edge cases (restatements)
        print(f"  WARN: {violations_report} rows have available_date > report_date + 200d", flush=True)
        for r in rows[:5]:
            print(f"    {r[0]}: report={r[1]} available={r[2]}", flush=True)
    else:
        print(f"  Check 2 PASS: {violations_report} rows with abnormal announcement delay (<=10 ok)", flush=True)

    # Check 3: every row has a non-null sustainability_pass (or explicit INSUFFICIENT_DATA)
    null_pass = cache.execute(
        "SELECT COUNT(*) FROM earnings_sustainability WHERE sustainability_pass IS NULL "
        "AND (failure_reason IS NULL OR failure_reason != 'INSUFFICIENT_DATA')"
    ).fetchone()[0]
    if null_pass > 0:
        print(f"  WARN: {null_pass} rows have null sustainability_pass without INSUFFICIENT_DATA", flush=True)
    else:
        print(f"  Check 3 PASS: all rows have explicit pass/fail or INSUFFICIENT_DATA", flush=True)

    print(f"\n  LEAKAGE TEST VERDICT: {'PASS' if violations_primary == 0 else 'FAIL'}", flush=True)
    return violations_primary == 0


def distribution_report(cache: sqlite3.Connection, shadow: sqlite3.Connection,
                        detail: bool = False) -> None:
    """Report sustainability pass/fail distribution for v3 candidates.

    Joins earnings_sustainability (cache.db) to shadow_candidates_v3
    (shadow_trading.db) via Python-side join, since the two tables live
    in different databases. This is a sanity check on filter
    aggressiveness, NOT an alpha test.
    """
    print("\n" + "=" * 70, flush=True)
    print("TEST 2: Distribution Report (sanity check, NOT alpha test)", flush=True)
    print("=" * 70, flush=True)

    total = cache.execute("SELECT COUNT(*) FROM earnings_sustainability").fetchone()[0]
    print(f"\n  Total earnings_sustainability rows: {total}", flush=True)

    if total == 0:
        print("  (table empty - run scripts/backfill_sustainability.py first)", flush=True)
        return

    # Sub-component pass rates
    metrics = [
        ("alignment_pass", "alignment_flag = 1"),
        ("consistency_pass", "consistency_flag = 1"),
        ("margin_norm_pass", "margin_normalization_flag = 1"),
        ("sustainability_pass", "sustainability_pass = 1"),
    ]
    print(f"\n  Sub-component pass rates:", flush=True)
    for label, cond in metrics:
        n = cache.execute(
            f"SELECT COUNT(*) FROM earnings_sustainability WHERE {cond}"
        ).fetchone()[0]
        pct = 100.0 * n / total if total else 0
        print(f"    {label:25s}: {n:5d} / {total} ({pct:5.1f}%)", flush=True)

    # Failure reason breakdown
    print(f"\n  Failure reason breakdown (among sustainability_pass = 0):", flush=True)
    reasons = cache.execute(
        "SELECT failure_reason, COUNT(*) AS n FROM earnings_sustainability "
        "WHERE sustainability_pass = 0 OR sustainability_pass IS NULL "
        "GROUP BY failure_reason ORDER BY n DESC"
    ).fetchall()
    for reason, n in reasons:
        pct = 100.0 * n / total if total else 0
        print(f"    {reason or 'NULL':25s}: {n:5d} ({pct:5.1f}%)", flush=True)

    # v3 candidate coverage: Python-side join across cache.db + shadow_trading.db
    print(f"\n  v3 candidate coverage:", flush=True)
    v3_candidates = shadow.execute(
        "SELECT DISTINCT security_id, trade_date FROM shadow_candidates_v3"
    ).fetchall()
    v3_total = len(v3_candidates)
    v3_keys = {(r["security_id"], r["trade_date"]) for r in v3_candidates}

    sustain_rows = cache.execute(
        "SELECT security_id, as_of_date, sustainability_pass FROM earnings_sustainability"
    ).fetchall()
    sustain_map = {(r["security_id"], r["as_of_date"]): r["sustainability_pass"]
                   for r in sustain_rows}

    v3_with_sustain = sum(1 for k in v3_keys if k in sustain_map)
    v3_pass = sum(1 for k in v3_keys
                  if k in sustain_map and sustain_map[k] == 1)
    coverage = 100.0 * v3_with_sustain / v3_total if v3_total else 0
    print(f"    v3 candidates total:        {v3_total}", flush=True)
    print(f"    with sustainability data:   {v3_with_sustain} ({coverage:5.1f}%)", flush=True)

    # Among v3 candidates with data, pass rate
    if v3_with_sustain > 0:
        v3_pass_pct = 100.0 * v3_pass / v3_with_sustain
        print(f"    sustainability_pass = 1:    {v3_pass} ({v3_pass_pct:5.1f}% of covered)", flush=True)

        print(f"\n  Phase 1.5 readiness (Gate B baseline = FRM-only +2.52%):", flush=True)
        print(f"    N(FRM+Sustain) = {v3_pass}  (need >= 30 for statistical power, Gate D)", flush=True)
        if v3_pass < 30:
            print(f"    WARNING: N < 30. Phase 1.5 alpha test will be underpowered.", flush=True)
            print(f"    Consider relaxing thresholds (tunable via stored raw values)", flush=True)
            print(f"    or extending backfill to more episodes.", flush=True)
        else:
            print(f"    N >= 30: Phase 1.5 Ablation has sufficient statistical power.", flush=True)

    # Per-industry breakdown (optional)
    if detail:
        print(f"\n  Per-industry breakdown:", flush=True)
        rows = cache.execute(
            """SELECT industry, COUNT(*) AS n,
                      SUM(CASE WHEN sustainability_pass = 1 THEN 1 ELSE 0 END) AS pass_n
               FROM earnings_sustainability
               WHERE industry IS NOT NULL
               GROUP BY industry ORDER BY n DESC LIMIT 20"""
        ).fetchall()
        print(f"    {'industry':30s} {'n':>6} {'pass':>6} {'rate':>7}", flush=True)
        for ind, n, pn in rows:
            rate = 100.0 * pn / n if n else 0
            print(f"    {ind:30s} {n:6d} {pn:6d} {rate:6.1f}%", flush=True)

    # profit_elasticity sanity (industry differences expected)
    print(f"\n  profit_elasticity distribution (industry differences expected):", flush=True)
    pe = cache.execute(
        """SELECT
             AVG(profit_elasticity) AS mean,
             MIN(profit_elasticity) AS min,
             MAX(profit_elasticity) AS max,
             COUNT(*) AS n
           FROM earnings_sustainability
           WHERE profit_elasticity IS NOT NULL
             AND ABS(profit_elasticity) < 100  -- exclude extreme outliers"""
    ).fetchone()
    if pe and pe[3] > 0:
        print(f"    mean={pe[0]:.2f}  min={pe[1]:.2f}  max={pe[2]:.2f}  n={pe[3]}", flush=True)
        print(f"    (raw stored - industry standardization deferred to v3.4.1)", flush=True)


def main():
    parser = argparse.ArgumentParser(description="v3.4 Phase 1 validation (leakage + distribution)")
    parser.add_argument("--detail", action="store_true", help="Per-industry breakdown")
    args = parser.parse_args()

    cache = sqlite3.connect(CACHE_DB)
    cache.row_factory = sqlite3.Row
    shadow = sqlite3.connect(SHADOW_DB)
    shadow.row_factory = sqlite3.Row

    ok = leakage_test(cache)
    distribution_report(cache, shadow, detail=args.detail)

    cache.close()
    shadow.close()

    print("\n" + "=" * 70, flush=True)
    if ok:
        print("PHASE 1 VERDICT: LEAKAGE PASS - ready for Phase 1.5 Ablation", flush=True)
        print("Next: python scripts/run_v3_4_sustainability_ablation.py", flush=True)
    else:
        print("PHASE 1 VERDICT: LEAKAGE FAIL - BLOCKER. Fix available_date gating.", flush=True)
    print("=" * 70, flush=True)


if __name__ == "__main__":
    main()
