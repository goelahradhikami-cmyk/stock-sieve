# EGE Validation Report
# Commit 6-S.15.3
# Date: 2026-07-21

---

## Experiment 1: Gap Decile Test

| Decile | Gap Range | N | Residual Mean | Positive Rate |
|--------|-----------|---|---------------|---------------|
| D1 | [-2.585, -1.300] | 19 | -0.0180 | 31.6% |
| D2 | [-1.280, -0.849] | 19 | -0.0722 | 15.8% |
| D3 | [-0.805, -0.471] | 19 | +0.0430 | 57.9% |
| D4 | [-0.464, -0.149] | 19 | -0.0127 | 36.8% |
| D5 | [-0.138, +0.010] | 19 | -0.0024 | 52.6% |
| D6 | [+0.017, +0.121] | 19 | -0.0177 | 36.8% |
| D7 | [+0.142, +0.361] | 19 | +0.0190 | 57.9% |
| D8 | [+0.379, +0.650] | 19 | +0.0217 | 52.6% |
| D9 | [+0.672, +1.749] | 19 | -0.0351 | 31.6% |
| D10 | [+1.759, +179.223] | 21 | -0.0874 | 28.6% |

**❌ Gate 1 (D10 > D1): FAIL**

## Experiment 2: FRM Preservation

| Group | N | Residual Mean | Positive Rate |
|-------|---|---------------|---------------|
| FRM-only | 192 | -0.0169 | 40.1% |
| EGE top-30% | 57 | -0.0340 | 36.8% |
| EGE bottom-30% | 57 | -0.0157 | 35.1% |

**❌ Gate 2 (FRM preservation): FAIL**

## Experiment 3: RS Ablation Regression Check

- Correlation gap_score vs RS: -0.0509
- EGE top-30% avg RS: 0.09036674739089452
- EGE bottom-30% avg RS: 0.09951033430686858

**✅ Gate 3 (RS inversion): PASS**

## Summary

| Gate | Result |
|------|--------|
| Gate 1 (Gap discrimination) | ❌ FAIL |
| Gate 2 (FRM preservation) | ❌ FAIL |
| Gate 3 (RS inversion) | ✅ PASS |

**1/3 gates PASS. See analysis above.**