"""Industry Reviewer — 产业趋势强度与持续性审查。

评分维度（spec §5.2）：
  - 行业动量（sector_momentum）：负向扣分，强正向加分
  - 行业增长与 thesis 一致性（成长股需有收入增速支撑）
  - 催化剂时效性（越远不确定性越高）

注意：real MarketSnapshot 没有 sector_momentum 字段，委员会接受调用方
显式传入 sector_momentum dict，或从 market_snapshot["sector_momentum"] 读取。
缺失动量数据视为中性。
"""

import re

from ._common import as_float, as_thesis_dict, clamp


def score_industry(thesis, market_snapshot, factor_snapshot) -> float:
    """判断产业趋势强度与持续性。返回 0-100。"""
    thesis = as_thesis_dict(thesis)
    fs = factor_snapshot or {}
    mkt = market_snapshot or {}
    mkt = mkt if isinstance(mkt, dict) else _dataclass_to_dict(mkt)
    score = 100.0

    # 1. 行业动量
    sector = thesis.get("sector") or fs.get("sector") or ""
    sector_momentum = 0.0
    if sector:
        sm = mkt.get("sector_momentum", {}) or {}
        sector_momentum = as_float(sm.get(sector, 0.0), 0.0)

    if sector_momentum < -0.1:
        score -= 25
    elif sector_momentum < 0:
        score -= 10
    elif sector_momentum > 0.1:
        score += 10

    # 2. 行业增长与 thesis 一致性（缺失视为中性）
    rg = fs.get("revenue_growth_yoy", fs.get("revenue_growth_1y"))
    if rg is not None and thesis.get("family") == "growth":
        if as_float(rg, 0.0) < 0.1:
            score -= 20  # 成长预期与事实背离

    # 3. 催化剂时效性
    horizon = thesis.get("catalyst_horizon_months", thesis.get("horizon", 12))
    catalyst_horizon = _parse_horizon(horizon)
    if catalyst_horizon > 18:
        score -= 15  # 催化剂太远，不确定性高

    return round(clamp(score), 1)


def _parse_horizon(v) -> float:
    """解析催化剂/投资期限为月数。支持 '18_months' 字符串或数字。"""
    if v is None:
        return 12.0
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"(\d+)", str(v))
    return float(m.group(1)) if m else 12.0


def _dataclass_to_dict(obj) -> dict:
    try:
        import dataclasses
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
    except Exception:
        pass
    return {}
