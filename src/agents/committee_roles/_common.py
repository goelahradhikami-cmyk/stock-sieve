"""Investment Committee 角色模块共享工具。

集中处理评分函数中反复出现的两类兼容性问题：
1. ValidationResult.counter_warnings 的 severity 在不同来源下可能是
   字符串("high"/"medium"/"low")、0-1 浮点、0-100 浮点，甚至缺失。
   normalize_severity() 统一归一到 0-1 浮点，供角色函数计分。
2. thesis 在代码中可能是 ThesisObject（dataclass）也可能是 dict，
   as_thesis_dict() 统一为 dict，使评分函数可用 thesis.get(...) 风格。
"""

from typing import Any

# 字符串严重度 → 0-1 浮点
SEVERITY_MAP = {"high": 1.0, "medium": 0.6, "low": 0.3}


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """将数值限制在 [lo, hi] 区间。"""
    return max(lo, min(hi, float(v)))


def normalize_severity(warn: dict) -> float:
    """将 counter_warning 的 severity 归一化为 0-1 浮点。

    兼容：
      - 字符串: "high"/"medium"/"low" → 1.0/0.6/0.3
      - 0-1 浮点: 直接取用
      - 0-100 浮点: 除以 100
      - 缺失/未知: 默认 0.3（中等偏低）
    """
    if not isinstance(warn, dict):
        return 0.3
    s = warn.get("severity", "medium")
    if isinstance(s, bool):
        return 0.3
    if isinstance(s, (int, float)):
        return clamp(s if s <= 1.0 else s / 100.0, 0.0, 1.0)
    if isinstance(s, str):
        return SEVERITY_MAP.get(s.lower().strip(), 0.3)
    return 0.3


def warning_matches(warn: dict, *keywords: str) -> bool:
    """检查 warning 的命名/分类字段是否命中任一关键词（不区分大小写）。"""
    if not isinstance(warn, dict):
        return False
    blob = " ".join(
        str(warn.get(k, ""))
        for k in ("rule_name", "pattern_name", "category", "name", "type", "description")
    ).lower()
    return any(k.lower() in blob for k in keywords)


def as_thesis_dict(thesis: Any) -> dict:
    """将 ThesisObject（dataclass）或 dict 统一为 dict。

    评分函数据此可用 thesis.get("family") 等字典风格访问，
    同时兼容 spec 伪代码与真实 dataclass 两种输入。
    """
    if thesis is None:
        return {}
    if isinstance(thesis, dict):
        return thesis
    out: dict = {}
    for attr in (
        "thesis_id",
        "family",
        "pattern",
        "claim",
        "evidence",
        "catalyst",
        "invalidation",
        "horizon",
        "sector",
        "confidence_contribution",
        "catalyst_horizon_months",
    ):
        if hasattr(thesis, attr):
            out[attr] = getattr(thesis, attr)
    return out


def as_float(v: Any, default: float = 0.0) -> float:
    """安全转为浮点，失败返回 default。"""
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default
