"""Valuation Reviewer — 安全边际与估值合理性审查。

评分维度（spec §5.1）：
  - 估值分位数（pe_percentile）过高 → 扣分
  - 自由现金流收益率（fcf_yield）过低 → 扣分
  - Validator 标记的估值类反证警告 → 按 severity 扣分
  - 成长股允许适度估值溢价（+10）

核心原则：缺失的因子数据视为中性，不惩罚（只惩罚已知的负面信号）。
"""

from ._common import as_float, as_thesis_dict, clamp, normalize_severity, warning_matches


def score_valuation(thesis, factor_snapshot, validation_result) -> float:
    """评估安全边际是否充足。返回 0-100（越高越安全）。"""
    thesis = as_thesis_dict(thesis)
    fs = factor_snapshot or {}
    score = 100.0

    # 1. 估值分位数（缺失视为中性 0.5）
    pe_pct = fs.get("pe_percentile")
    pe_pct = 0.5 if pe_pct is None else as_float(pe_pct, 0.5)
    if pe_pct > 0.9:
        score -= 30
    elif pe_pct > 0.7:
        score -= 15

    # 2. 自由现金流收益率（缺失视为中性，不惩罚）
    fcf = fs.get("fcf_yield")
    if fcf is not None:
        fcf_yield = as_float(fcf, 0.0)
        if fcf_yield < 0.02:
            score -= 20
        elif fcf_yield < 0.05:
            score -= 10

    # 3. Validator 中的估值类反证警告
    for warn in _get_warnings(validation_result):
        if warning_matches(
            warn, "valuat", "估值", "pe", "pb", "margin", "安全边际", "溢价"
        ):
            score -= normalize_severity(warn) * 20

    # 4. 成长股估值容忍度稍高
    if thesis.get("family") == "growth":
        score += 10

    return round(clamp(score), 1)


def _get_warnings(validation_result):
    if validation_result is None:
        return []
    if isinstance(validation_result, dict):
        return validation_result.get("counter_warnings", []) or []
    return getattr(validation_result, "counter_warnings", []) or []
