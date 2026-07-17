"""Quant Auditor — Alpha 统计纯度与持续性审查。

评分维度（spec §5.4），全部依赖 EvaluationDB 的三个查询方法：
  - get_alpha_persistence(agent_id, months)  研究员历史 Alpha 持续性
  - get_pattern_ic(pattern, months)          该 pattern 的因子 IC 近期表现
  - get_pattern_sample_size(pattern)         pattern 样本量（过拟合风险）

evaluation_db 为 None 时返回中性 100（没有数据不惩罚也不奖励）。
"""

from ._common import clamp


def score_quant(research_agent_id, thesis_pattern, evaluation_db) -> float:
    """检验 Alpha 的统计纯度与持续性。返回 0-100。"""
    if evaluation_db is None:
        return 100.0

    score = 100.0

    # 1. 研究员历史 Alpha 持续性
    alpha_persistence = _safe_float(
        evaluation_db.get_alpha_persistence(research_agent_id, months=12)
    )
    if alpha_persistence < 0.5:
        score -= 30
    elif alpha_persistence < 0.7:
        score -= 15

    # 2. 该 thesis_pattern 的因子 IC 近期表现
    pattern_ic = _safe_float(evaluation_db.get_pattern_ic(thesis_pattern, months=6))
    if pattern_ic < 0:
        score -= 25
    elif pattern_ic < 0.05:
        score -= 10

    # 3. 过拟合风险（样本量过小）
    sample_size = _safe_int(evaluation_db.get_pattern_sample_size(thesis_pattern))
    if sample_size < 10:
        score -= 20
    elif sample_size < 20:
        score -= 10

    return round(clamp(score), 1)


def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except (TypeError, ValueError):
        return default
