# v3.2 A/B Attribution Comparison Report
# Commit 6-S.13.6
# Date: 2026-07-21
# Situation: C

---

## Three-Group Comparison

| Group | N | residual_alpha mean | median | >0 rate | market_beta | sector_beta | beta_sum |
|-------|---|---------------------|--------|---------|-------------|-------------|----------|
| Group A (v2_anomaly) | 22 | -0.0571 | -0.0721 | 18.2% | -0.0238 | -0.0102 | -0.0340 |
| Group C (v3_recovery_rs, full) | 134 | -0.0430 | -0.0424 | 31.3% | -0.0108 | -0.0183 | -0.0292 |
| All v3 (B+C) | 134 | -0.0430 | -0.0424 | 31.3% | -0.0108 | -0.0183 | -0.0292 |

## Gate Evaluation

| Gate | Target | V2 | V3 | Result |
|------|--------|----|----|--------|
| gate1_residual_alpha | > -2% | -0.0571 | -0.0430 | ❌ FAIL |
| gate2_positive_rate | > 30% | 18.2% | 31.3% | ✅ PASS |
| gate3_beta_reduction | v3 beta_sum < v2 beta_sum | -0.0340 | -0.0292 | ❌ FAIL |

## Situation Assessment

**Situation C: alpha still < -2%: Stage 1/2 only filter garbage. Mispricing hypothesis itself may be wrong. Need to redesign anomaly detection.**

residual_alpha: V2 -0.0571 -> V3 -0.0430 (delta +0.0141)
positive rate:  V2 18.2% -> V3 31.3%
beta exposure:  V2 -0.0340 -> V3 -0.0292

## Interpretation

V3's Stage 1/2 filter out garbage but do not produce alpha. The mispricing hypothesis itself may be wrong. The anomaly detector's core assumption ('drawdown + cheap = mispriced') needs fundamental redesign.

## A/B Isolation (FRM vs RS contribution)

