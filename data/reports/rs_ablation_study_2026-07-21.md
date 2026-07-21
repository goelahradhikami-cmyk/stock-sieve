# RS Ablation Study Report
# Commit 6-S.14.2
# Date: 2026-07-21

---

## Experiment 1: RS Quintile Analysis

Test: does weak RS (price not yet reacted) produce higher alpha than strong RS?

| Quintile | RS Range | N | Residual Mean | Positive Rate |
|----------|----------|---|---------------|---------------|
| Q1 | [+0.0015, +0.0193] | 26 | -0.0447 | 26.9% |
| Q2 | [+0.0196, +0.0378] | 26 | -0.0403 | 26.9% |
| Q3 | [+0.0380, +0.0634] | 26 | +0.0137 | 42.3% |
| Q4 | [+0.0637, +0.1181] | 26 | -0.0488 | 34.6% |
| Q5 | [+0.1211, +0.5189] | 30 | -0.0485 | 33.3% |

**Hypothesis A verdict: SUPPORTED**

## Experiment 2: RS Threshold Sweep

| Threshold | N | Residual Mean | Positive Rate |
|-----------|---|---------------|---------------|
| RS > -0.10 | 134 | -0.0342 | 32.8% |
| RS > -0.05 | 134 | -0.0342 | 32.8% |
| RS > -0.02 | 134 | -0.0342 | 32.8% |
| RS > +0.00 | 134 | -0.0342 | 32.8% |
| RS > +0.02 | 107 | -0.0321 | 33.6% |
| RS > +0.05 | 72 | -0.0315 | 37.5% |
| RS > +0.10 | 37 | -0.0590 | 32.4% |

**Optimal: RS > +0.05 (residual -0.0315)**

## Experiment 3: FRM × RS 2D Matrix

**Best cell: weak_Q3 (residual +0.0395)**

## Experiment 4: Early vs Late RS

- Correlation RS_20d vs residual: -0.2663
- Correlation RS_60d vs residual: -0.2002
- Verdict: RS_20d_NEGATIVE_CORRELATION

## Experiment 5: Sample Composition

Verdict: B_BEATS_C_WITHIN_FRM_BUCKET

## Conclusion

See stdout for full analysis. Key finding determines whether RS gate should be removed, softened, or reversed.