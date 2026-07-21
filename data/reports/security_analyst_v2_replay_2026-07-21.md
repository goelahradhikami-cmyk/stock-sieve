# Security Analyst v2 Replay Report
# Commit 6-S.12.5
# Date: 2026-07-21
# Verdict: V2 IMPROVED - but residual_alpha still negative

---

## Comparison: V1 (doctrine consensus) vs V2 (three-layer)

V2 three-layer composite: Mispricing (0.35) + Business Survival/FRM (0.35) + Recovery Confirmation (0.30).
V2 selects top-5 by composite score; V1 used doctrine consensus PASS. Both are RANKING only - BUY/BLOCK remains Market Guardian's frozen decision.

### Stock-level residual_alpha (true selection alpha, 2024-06+ only)

| Metric | V1 selected | V2 top-5 |
|--------|-------------|----------|
| N | 22 | 18 |
| mean | -0.0571 | -0.0501 |
| median | -0.0721 | -0.0646 |
| positive rate | 18.2% | 16.7% |

### Stock-level market_beta (all episodes)

| Metric | V1 selected | V2 top-5 |
|--------|-------------|----------|
| mean | +0.0453 | +0.0316 |
| median | +0.0460 | +0.0172 |

### Stock-level stock_return (all episodes)

| Metric | V1 selected | V2 top-5 |
|--------|-------------|----------|
| mean | +0.0430 | +0.0450 |
| median | +0.0401 | +0.0343 |
| positive rate | 74.7% | 69.7% |

### Verdict

**V2 IMPROVED - but residual_alpha still negative**

- ✅ residual_alpha_improvement: {'v1_mean': -0.0570957800553277, 'v2_mean': -0.05008131885444148, 'improvement': 0.007014461200886217, 'v2_positive': False, 'passed': True}
- ✅ stock_return_comparison: {'v1_mean': 0.04298649300672145, 'v2_mean': 0.045045359232061655, 'improvement': 0.0020588662253402043, 'passed': True}

### Interpretation

V2 improved residual_alpha by +0.0070 (from -0.0571 to -0.0501).
V2 improves but residual_alpha is still negative - selection layer needs deeper redesign (anomaly detection itself may be the problem).
