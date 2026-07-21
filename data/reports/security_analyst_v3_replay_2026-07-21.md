# Security Analyst v3 Replay Report
# Commit 6-S.13.5
# Date: 2026-07-21
# Verdict: V3.1 PASS (FRM direction corrected) - v3.2 not yet

---

## v3.1: FRM Direction (target: improving > 85%)

V1 selected: {'deteriorating': 58, 'stable': 10, 'improving': 27}
V3 top-5:    {'improving': 44, 'stable': 6}

| Metric | V1 | V3 |
|--------|----|----|
| improving rate | 28.4% | 88.0% |

**✅ v3.1 GATE: True** (target >85%)

## v3.2: Residual Alpha (target: > -2%)

| Metric | V1 selected | V3 top-5 |
|--------|-------------|----------|

V3 candidates in original pool (have attribution): 0
V3 candidates NOT in original pool (no attribution): 50


## Funnel Log Summary

```
Total funnel entries: 8549
  stage1/DETERIORATING: 4330
  stage2/WEAK_RS: 1885
  stage3/NO_MISPRICING: 22
  stage1/LOW_LIQUIDITY: 13
  final_pass (all 3 stages): 2299
```

## Interpretation

V3 corrected FRM direction: 28.4% -> 88.0% improving. The Stage 1 hard gate (reject deteriorating) successfully filters out stocks with declining earnings.
