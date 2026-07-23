"""Risk Controller — 组合脆弱性与极端风险审查。

评分维度（spec §5.3）：
  - 行业集中度（portfolio_state.sector_weights）
  - 个股尾部风险（expected_drawdown_12m）
  - 错误代价（对 Validator 反证 severity 求和）
  - 流动性（liquidity_score）

兼容性说明：
  - SecurityAnalysis 本身没有 sector 属性，故 sector 优先取
    security_analysis.sector，缺失时回退到 factor_snapshot["sector"]。
  - counter_warnings 的 severity 可能是字符串(high/medium/low)或数值，
    统一经 normalize_severity 归一到 0-1 再求和。
"""

from ._common import as_float, clamp, normalize_severity


def score_risk(
    portfolio_state, security_analysis, validation_result, factor_snapshot=None
) -> float:
    """评估组合脆弱性与极端风险。返回 0-100（越高越安全）。"""
    fs = factor_snapshot or {}
    score = 100.0

    # 1. 行业集中度
    sector = _get_sector(security_analysis, fs)
    sector_weights = _get_sector_weights(portfolio_state)
    current_weight = sector_weights.get(sector, 0.0) if sector else 0.0
    if current_weight > 0.3:
        score -= 30
    elif current_weight > 0.2:
        score -= 20

    # 2. 个股尾部风险
    expected_dd = _get_expected_drawdown(security_analysis)
    if expected_dd > 0.3:
        score -= 25
    elif expected_dd > 0.2:
        score -= 10

    # 3. 错误代价（反证 severity 总和）
    error_cost = sum(normalize_severity(w) for w in _get_warnings(validation_result))
    score -= error_cost * 15

    # 4. 流动性（缺失视为中性 100）
    liquidity = as_float(fs.get("liquidity_score", 100), 100)
    if liquidity < 30:
        score -= 30
    elif liquidity < 50:
        score -= 15

    return round(clamp(score), 1)


def _get_sector(security_analysis, fs):
    if security_analysis is not None and hasattr(security_analysis, "sector"):
        s = security_analysis.sector
        if s:
            return s
    return fs.get("sector") or ""


def _get_sector_weights(portfolio_state):
    if portfolio_state is None:
        return {}
    if isinstance(portfolio_state, dict):
        return portfolio_state.get("sector_weights", {}) or {}
    return getattr(portfolio_state, "sector_weights", {}) or {}


def _get_expected_drawdown(security_analysis):
    ra = {}
    if security_analysis is not None:
        if hasattr(security_analysis, "risk_assessment"):
            ra = security_analysis.risk_assessment or {}
        elif isinstance(security_analysis, dict):
            ra = security_analysis.get("risk_assessment", {}) or {}
    dd = ra.get("expected_drawdown_12m")
    return abs(as_float(dd, 0.2)) if dd is not None else 0.2


def _get_warnings(validation_result):
    if validation_result is None:
        return []
    if isinstance(validation_result, dict):
        return validation_result.get("counter_warnings", []) or []
    return getattr(validation_result, "counter_warnings", []) or []
