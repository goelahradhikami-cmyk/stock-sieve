# v3.2 A/B Attribution Comparison Report
# Commit 6-S.13.6
# Date: 2026-07-21
# Situation: B

---

## Three-Group Comparison

| Group | N | residual_alpha mean | median | >0 rate | market_beta | sector_beta | beta_sum |
|-------|---|---------------------|--------|---------|-------------|-------------|----------|
| Group A (v2_anomaly) | 22 | -0.0571 | -0.0721 | 18.2% | -0.0238 | -0.0102 | -0.0340 |
| Group B (v3_recovery, no RS) | 60 | +0.0252 | +0.0141 | 58.3% | +0.0357 | +0.0126 | +0.0484 |
| Group C (v3_recovery_rs, full) | 134 | -0.0342 | -0.0480 | 32.8% | -0.0108 | -0.0095 | -0.0203 |
| All v3 (B+C) | 194 | -0.0158 | -0.0230 | 40.7% | +0.0036 | -0.0026 | +0.0009 |

## Gate Evaluation

| Gate | Target | V2 | V3 | Result |
|------|--------|----|----|--------|
| gate1_residual_alpha | > -2% | -0.0571 | -0.0158 | ✅ PASS |
| gate2_positive_rate | > 30% | 18.2% | 40.7% | ✅ PASS |
| gate3_beta_reduction | v3 beta_sum < v2 beta_sum | -0.0340 | +0.0009 | ❌ FAIL |

## Situation Assessment

**Situation B: -2% < alpha < 0: direction right, but Stage 3 mispricing detector is weak. Optimize Stage 3.**

residual_alpha: V2 -0.0571 -> V3 -0.0158 (delta +0.0413)
positive rate:  V2 18.2% -> V3 40.7%
beta exposure:  V2 -0.0340 -> V3 +0.0009

## Interpretation

V3 improved direction (FRM) and reduced beta exposure (RS), but residual_alpha is still negative. The Stage 3 mispricing detector (anomaly) is the remaining weakness. Next: optimize Stage 3 or redesign anomaly detection.

## A/B Isolation (FRM vs RS contribution)

Group B (FRM only, no RS): residual_alpha = +0.0252
Group C (FRM + RS):        residual_alpha = -0.0342
RS contribution:           -0.0593
RS gate does NOT add value (or makes it worse).
