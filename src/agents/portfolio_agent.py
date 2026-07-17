"""
Portfolio Agent — Portfolio construction & risk management.

Implements portfolio_policy_schema_v1.1.1:
  - conviction_engine: confidence → base position (§3.2)
  - position_engine: 8-step final_weight calculation (§3.3)
  - market_regime_adapter: bull/bear/crisis/rotation exposure limits (§3.5)
  - valuation_gate: PE/PB percentile circuit breakers (§3.7)
  - drawdown_reasoning: 4 decline types with differentiated responses (§4.3)
  - portfolio_memory: lessons from past errors (§5)

Role boundaries (contract §1, portfolio_policy §7):
  - Cannot modify SecurityAnalysis alpha_score
  - Cannot generate new investment theses
  - Cannot execute trades directly
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PortfolioState:
    """Current portfolio state."""
    cash_balance: float = 1_000_000.0
    positions: list[dict] = field(default_factory=list)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    last_rebalance_date: str = ""


@dataclass
class RiskPolicy:
    """Portfolio-level risk constraints."""
    max_single_position: float = 0.10        # 10% max per stock
    conviction_max: float = 0.20             # 20% max for conviction positions
    max_positions: int = 15
    min_positions: int = 5
    min_cash: float = 0.05
    max_cash: float = 0.30
    max_single_sector: float = 0.25
    max_top3_sectors: float = 0.50
    max_pairwise_correlation: float = 0.70
    max_drawdown_trigger: float = -0.20
    single_stock_dd_trigger: float = -0.25


@dataclass
class PositionDecision:
    """Single position allocation decision."""
    stock_code: str
    action: str                    # BUY / SELL / HOLD / REDUCE
    target_weight: float
    current_weight: float = 0.0
    delta_weight: float = 0.0
    execution_instruction: str = "normal"  # normal / opportunistic / urgent
    reason: str = ""
    urgency: str = "normal"
    linked_thesis_id: str | None = None
    position_engine_trace: dict = field(default_factory=dict)


@dataclass
class PortfolioDecision:
    """Complete portfolio allocation output."""
    policy_id: str
    timestamp: str
    decisions: list[PositionDecision] = field(default_factory=list)
    cash_target: float = 0.08
    expected_metrics: dict = field(default_factory=dict)
    risk_warnings: list[str] = field(default_factory=list)
    valuation_gate_status: dict = field(default_factory=dict)
    memory_flags: list[dict] = field(default_factory=list)

    def apply_committee_cap(self, cap_modifier: float) -> "PortfolioDecision":
        """按委员会决议的仓位上限修饰符，缩放所有持仓目标权重（spec §7 / §10）。

        cap_modifier: 1.0=无限制, 0.5=半仓, 0.0=清零。仅缩放，不新增/删除持仓。
        返回 self 以便链式调用。
        """
        cap = max(0.0, min(1.0, float(cap_modifier)))
        for d in self.decisions:
            d.target_weight = round(d.target_weight * cap, 4)
        return self


class PortfolioAgent:
    """AI fund manager — Portfolio role.

    Receives SecurityAnalysis[], constructs portfolio with risk controls.
    """

    # ── Conviction → base position mapping (§3.2) ──────────

    BASE_POSITION_MAP = [
        (0, 5,   0.0,  "no_trade"),
        (5, 7,   0.03, "pilot_position"),
        (7, 8.5, 0.06, "standard_position"),
        (8.5, 10, 0.10, "conviction_position"),
    ]

    # ── Alpha → expected excess return mapping (§3.3 Kelly) ─

    ALPHA_TO_RETURN = [
        (0, 5,   0.0),
        (5, 6,   0.05),
        (6, 7,   0.10),
        (7, 8,   0.15),
        (8, 9,   0.22),
        (9, 10,  0.30),
    ]

    # ── Market regime exposure limits (§3.5) ────────────────

    REGIME_CONFIG = {
        "bull":     {"max_exposure": 0.95, "conviction_multiplier": 1.0, "cash_target": 0.05},
        "bear":     {"max_exposure": 0.70, "conviction_multiplier": 0.8, "cash_target": 0.20},
        "crisis":   {"max_exposure": 0.40, "conviction_multiplier": 0.5, "cash_target": 0.30},
        "rotation": {"max_exposure": 0.85, "conviction_multiplier": 0.9, "cash_target": 0.10},
    }

    # ── Decline type classification (§4.3) ─────────────────

    DECLINE_TYPES = {
        "market_sentiment": {
            "action": "hold_or_selectively_add",
            "reason": "市场恐慌提供更好的买入价格，不应在此时被迫卖出",
        },
        "valuation_compression": {
            "action": "mandatory_review",
            "reason": "审查估值中枢下移原因后决定持有或减仓",
        },
        "thesis_damage": {
            "action": "exit_or_reduce_to_minimum",
            "reason": "Thesis是持仓的根基，假设被证伪时应退出",
        },
        "fraud_or_crisis": {
            "action": "immediate_exit",
            "reason": "信任崩塌，不设任何条件，立即退出",
        },
    }

    def __init__(self, policy_id: str = "conservative_value_fund",
                  risk_policy: RiskPolicy = None):
        self.policy_id = policy_id
        self.risk_policy = risk_policy or RiskPolicy()
        self.memory: dict = {
            "concentration_errors": [],
            "sizing_errors": [],
            "drawdown_errors": [],
            "correlation_failures": [],
        }

    def construct_portfolio(self, analyses: list,
                             state: PortfolioState,
                             market: dict,
                             market_intel: dict = None) -> PortfolioDecision:
        """Core construct_portfolio() method (contract §4.2).

        Args:
            analyses: list of SecurityAnalysis from Research Agent(s)
            state: current PortfolioState
            market: dict with regime_type, risk_score, pe_percentile, etc.
            market_intel: Commit 6-F.2 MarketIntelligenceAgent output (optional)
        """
        now = datetime.now().isoformat()

        regime = market.get("regime_type", "rotation")
        regime_cfg = self.REGIME_CONFIG.get(regime, self.REGIME_CONFIG["rotation"])
        risk_score = market.get("risk_score", 50)

        # ── 1. Rank securities (§3.1) ──────────────────────
        ranked = self._rank_securities(analyses)

        # ── 2. Compute position for each security ──────────
        decisions = []
        total_weight = 0.0

        for rank_idx, analysis in enumerate(ranked[:self.risk_policy.max_positions]):
            decision = self._compute_position(
                analysis, rank_idx, regime_cfg, risk_score, market, market_intel
            )
            decisions.append(decision)
            total_weight += decision.target_weight

        # ── 3. Apply portfolio-level constraints ───────────
        cash_target = max(
            self.risk_policy.min_cash,
            regime_cfg["cash_target"]
        )

        # Normalize if exceeding max exposure
        if total_weight > regime_cfg["max_exposure"]:
            scale = regime_cfg["max_exposure"] / total_weight
            for d in decisions:
                d.target_weight *= scale
            total_weight = regime_cfg["max_exposure"]

        # ── 4. Risk warnings ───────────────────────────────
        warnings = []
        if risk_score > 80:
            warnings.append("市场风险评分过高，建议提高现金比例")
        if len(decisions) < self.risk_policy.min_positions:
            warnings.append(f"持仓数量不足（当前{len(decisions)}，最低{self.risk_policy.min_positions}）")

        # ── 5. Memory flags (pattern matching) ─────────────
        memory_flags = self._check_memory_patterns(decisions)

        return PortfolioDecision(
            policy_id=self.policy_id,
            timestamp=now,
            decisions=decisions,
            cash_target=round(cash_target, 2),
            expected_metrics={
                "expected_volatility": 0.15,
                "expected_max_drawdown": 0.18,
                "expected_sharpe": 0.8,
            },
            risk_warnings=warnings,
            valuation_gate_status={"active": False, "applied_rules": []},
            memory_flags=memory_flags,
        )

    def _rank_securities(self, analyses: list) -> list:
        """Rank securities by composite score (§3.1 security_ranking)."""
        scored = []
        for a in analyses:
            # Weighted ranking factors
            score = (
                a.alpha_score * 0.35 +
                a.confidence * 0.25 +
                (len(a.thesis.evidence) if a.thesis else 0) * 0.20 / 5 * 10 +
                (10 - a.risk_assessment.get("expected_drawdown_12m", 0.2) * 20) * 0.20
            )
            scored.append((score, a))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [a for _, a in scored]

    def _compute_position(self, analysis, rank: int,
                           regime_cfg: dict, risk_score: float,
                           market: dict, market_intel: dict = None) -> PositionDecision:
        """8-step final_weight calculation (§3.3 position_engine) + Market Intel (Commit 6-F.2)."""

        trace = {}

        # ── Step 1: Base position from conviction ──────────
        base_weight = self._conviction_to_base(analysis.confidence)
        trace["base_weight"] = base_weight

        # ── Step 2: Kelly adjustment ──────────────────────
        kelly_weight = self._kelly_adjust(analysis.alpha_score, base_weight)
        trace["kelly_weight"] = kelly_weight

        # ── Step 3: Error cost risk penalty ───────────────
        risk_penalty = self._compute_risk_penalty(analysis)
        trace["risk_penalty"] = risk_penalty

        # ── Step 4: Market Intelligence probability-weighted (Commit 6-F.2) ──
        prob_exposure = 1.0
        if market_intel:
            probs = market_intel.get('environment', {}).get('probability', {})
            mkt_risk = market_intel.get('risk', {}).get('overall', risk_score)
            # Probability-weighted exposure
            exposure_weights = {'bull': 0.95, 'bear': 0.70, 'crisis': 0.40, 'rotation': 0.85}
            prob_exposure = sum(probs.get(s, 0) * exposure_weights.get(s, 0.5) for s in probs) if probs else 1.0
            # Risk gate discount (max 30% reduction)
            risk_gate = max(0.3, 1 - mkt_risk / 200)
            prob_exposure = prob_exposure * risk_gate
        else:
            # Fallback: use old hardcoded regime multiplier
            prob_exposure = regime_cfg["conviction_multiplier"]
        trace["prob_exposure"] = prob_exposure

        # ── Step 5: Liquidity discount ────────────────────
        liquidity_penalty = 0.0
        trace["liquidity_penalty"] = liquidity_penalty

        # ── Step 6: Valuation gate ────────────────────────
        val_gate_pct = 1.0
        pe_pct = market.get("market_pe_percentile")
        if pe_pct and pe_pct > 0.90:
            val_gate_pct = 0.5  # cap at half
        trace["valuation_gate"] = val_gate_pct

        # ── Step 7: Position cap ──────────────────────────
        position_cap = self.risk_policy.max_single_position

        # ── Step 8: Final weight ──────────────────────────
        final_weight = base_weight

        # Apply Kelly (can increase but limited to 1.5x base)
        final_weight = min(final_weight * 1.5, max(final_weight, kelly_weight))

        # Apply risk penalty
        final_weight *= (1.0 - risk_penalty)

        # Apply liquidity penalty
        final_weight *= (1.0 - liquidity_penalty)

        # Apply market intelligence probability-weighted exposure (Commit 6-F.2)
        final_weight *= prob_exposure

        # Apply valuation gate
        final_weight *= val_gate_pct

        # Apply position cap
        final_weight = min(final_weight, position_cap)

        # Rank discount: lower rank = slightly lower weight
        if rank >= 10:
            final_weight *= 0.5

        final_weight = round(final_weight, 4)
        trace["final_weight"] = final_weight

        action = "BUY" if final_weight > 0.01 else "HOLD"

        return PositionDecision(
            stock_code=analysis.stock_code,
            action=action,
            target_weight=final_weight,
            reason=f"conviction={analysis.confidence}, alpha={analysis.alpha_score}",
            linked_thesis_id=analysis.thesis.thesis_id if analysis.thesis else None,
            position_engine_trace=trace,
        )

    def _conviction_to_base(self, confidence: float) -> float:
        """Map confidence to base position weight (§3.2)."""
        for lo, hi, weight, _ in self.BASE_POSITION_MAP:
            if lo <= confidence < hi or (hi == 10 and confidence == 10):
                return weight
        return 0.0

    def _kelly_adjust(self, alpha_score: float, base_weight: float) -> float:
        """Half-Kelly adjustment (§3.3 kelly_adjustment).

        Kelly fraction = expected_excess_return / variance
        Half-Kelly = fraction * 0.5
        Capped at 0.25, floored at 0.0.
        """
        # Map alpha to expected excess return
        exp_return = 0.05  # default
        for lo, hi, ret in self.ALPHA_TO_RETURN:
            if lo <= alpha_score < hi or (hi == 10 and alpha_score == 10):
                exp_return = ret
                break

        variance = 0.15 ** 2  # assumed 15% vol
        kelly_fraction = exp_return / variance * 0.5  # Half-Kelly
        kelly_fraction = min(0.25, max(0.0, kelly_fraction))

        # Kelly can raise, but not exceed 1.5x base
        kelly_weight = base_weight * (1 + kelly_fraction)
        return min(kelly_weight, base_weight * 1.5)

    def _compute_risk_penalty(self, analysis) -> float:
        """Compute risk penalty from error_cost_vector."""
        risk_level = analysis.risk_assessment.get("idiosyncratic_risk", "medium")
        penalty_map = {"low": 0.0, "medium": 0.10, "high": 0.25}
        return penalty_map.get(risk_level, 0.10)

    def classify_drawdown(self, stock_dd: float, market_dd: float,
                           thesis_invalidated: bool,
                           fraud_suspected: bool) -> str:
        """Classify decline type per §4.3 drawdown_reasoning.

        Returns decline type key for differentiated response.
        """
        if fraud_suspected:
            return "fraud_or_crisis"
        if thesis_invalidated:
            return "thesis_damage"
        if abs(stock_dd - market_dd) < 0.05:  # correlated with market
            return "market_sentiment"
        return "valuation_compression"

    def get_drawdown_action(self, decline_type: str) -> dict:
        """Get prescribed action for a decline type."""
        return self.DECLINE_TYPES.get(decline_type, {
            "action": "mandatory_review",
            "reason": "未识别的下跌类型，需人工审查",
        })

    def _check_memory_patterns(self, decisions: list[PositionDecision]) -> list[dict]:
        """Check current portfolio against historical failure patterns (§5 pattern_matching)."""
        flags = []

        # Simple concentration check
        if len(decisions) > 0:
            top3_weight = sum(d.target_weight for d in decisions[:3])
            if top3_weight > 0.40:
                flags.append({
                    "flag_type": "concentration_risk_high",
                    "severity": "warning",
                    "recommendation": "前3大持仓占比过高，审查集中度风险",
                })

        return flags
