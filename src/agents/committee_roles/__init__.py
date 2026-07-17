# Stock Sieve — Investment Committee 角色评分子包 (Phase 5A-3)
#
# 五个评分角色均为纯规则引擎函数，不依赖 LLM：
#   - score_valuation   估值审查员
#   - score_industry    行业趋势审查员
#   - score_risk        风控官
#   - score_quant       量化审计员
#   - score_devil_advocate 魔鬼代言人（返回 (survival, attacks)）
#
# _common 提供兼容工具：severity 归一化、thesis 字典化、数值安全转换。

from ._common import (
    as_float,
    as_thesis_dict,
    clamp,
    normalize_severity,
    warning_matches,
)
from .devil_advocate import score_devil_advocate
from .industry_reviewer import score_industry
from .quant_auditor import score_quant
from .risk_controller import score_risk
from .valuation_reviewer import score_valuation

__all__ = [
    "score_valuation",
    "score_industry",
    "score_risk",
    "score_quant",
    "score_devil_advocate",
    "normalize_severity",
    "warning_matches",
    "as_thesis_dict",
    "as_float",
    "clamp",
]
