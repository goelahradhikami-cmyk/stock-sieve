"""
Failure Classifier — Rule-based failure type detection from evaluation data.

Commit 4: Post-Mortem Engine v1.0
"""


class FailureClassifier:
    """Classify investment failures from evaluation + attribution data."""

    def classify(self, evaluation: dict, attribution: dict) -> list[dict]:
        """Analyze evaluation and attribution, return list of failure dicts.

        Each failure dict: {type, severity, evidence}
        """
        failures = []

        # ── 1. Market/regime failure ─────────────────────
        alpha_mkt = evaluation.get("alpha_vs_market", 0) or 0
        stock_ret = evaluation.get("stock_return", 0) or 0
        if alpha_mkt < -0.15 and stock_ret < 0:
            failures.append({
                "type": "market_regime_failure",
                "severity": 0.8,
                "evidence": {
                    "alpha_vs_market": alpha_mkt,
                    "stock_return": stock_ret,
                }
            })

        # ── 2. Sector selection failure ──────────────────
        alpha_sec = evaluation.get("alpha_vs_sector", 0) or 0
        if alpha_sec < -0.1:
            failures.append({
                "type": "sector_selection_failure",
                "severity": 0.7,
                "evidence": {"alpha_vs_sector": alpha_sec}
            })

        # ── 3. Stock selection failure ───────────────────
        sel_alpha = attribution.get("selection_alpha",
                    attribution.get("stock_alpha", 0)) or 0
        if sel_alpha < -0.1:
            failures.append({
                "type": "stock_selection_failure",
                "severity": 0.8,
                "evidence": {"selection_alpha": sel_alpha}
            })

        # ── 4. Execution failure ─────────────────────────
        exec_cost = attribution.get("execution_cost", 0) or 0
        if exec_cost > 0.02:
            failures.append({
                "type": "execution_failure",
                "severity": 0.4,
                "evidence": {"execution_cost": exec_cost}
            })

        # ── 5. Timing: early entry ────────────────────────
        max_dd = evaluation.get("max_drawdown_during", 0) or 0
        net_ret = evaluation.get("net_return", evaluation.get("stock_return", 0)) or 0
        if max_dd < -0.2 and net_ret > 0:
            failures.append({
                "type": "timing_failure_early",
                "severity": 0.6,
                "evidence": {
                    "max_drawdown_during": max_dd,
                    "net_return": net_ret,
                }
            })

        # ── 6. Timing: late exit ──────────────────────────
        eval_type = evaluation.get("evaluation_type", "ENTRY")
        exit_opp = evaluation.get("exit_opportunity_cost", 0) or 0
        if eval_type == "EXIT" and exit_opp > 0.15:
            failures.append({
                "type": "timing_failure_late_exit",
                "severity": 0.6,
                "evidence": {"exit_opportunity_cost": exit_opp}
            })

        return failures
