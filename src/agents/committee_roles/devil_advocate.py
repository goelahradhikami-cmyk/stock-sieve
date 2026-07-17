"""Devil's Advocate — 攻击核心假设，评估存活概率。

返回 (survival_probability, attack_points)：
  - survival_probability: 0-100，越低表示核心假设越容易被推翻
  - attack_points: 攻击锚点列表（强制作为 LLM 陈述的生成锚，禁止编造新维度）

兼容性说明：counter_warnings 的 severity 可能是字符串/数值，统一经
normalize_severity 归一到 0-1 后再比较。
"""

from ._common import as_thesis_dict, clamp, normalize_severity

ABSOLUTE_WORDS = ["必然", "绝无", "唯一", "永远", "绝对", "必定", "100%", "无风险", "不可能", "guaranteed", "certainly", "impossible"]
QUANT_OPS = ("<", ">", "==", "<=", ">=", "≤", "≥", "低于", "高于", "小于", "大于", "以下", "以上")


def score_devil_advocate(thesis, validation_result=None, factor_snapshot=None):
    """攻击核心假设，返回 (survival_probability, attack_points)。"""
    thesis = as_thesis_dict(thesis)
    survival = 100.0
    attack_points = []

    # 1. 绝对化断言
    claim = (thesis.get("claim", "") or "").lower()
    found_absolute = [w for w in ABSOLUTE_WORDS if w.lower() in claim]
    if found_absolute:
        survival -= 20
        attack_points.append(
            f"ASSERTION_TOO_ABSOLUTE: 投资假设使用了过于绝对的断言: {found_absolute}"
        )

    # 2. 证据链薄弱
    evidence = thesis.get("evidence", []) or []
    if len(evidence) < 3:
        survival -= 15
        attack_points.append(
            f"INSUFFICIENT_EVIDENCE: 证据不足({len(evidence)}条)，核心主张可被替代解释推翻"
        )

    # 3. 证伪条件可量化性
    invalidation = thesis.get("invalidation", []) or []
    if not invalidation:
        survival -= 25
        attack_points.append("NO_INVALIDATION: 未提供证伪条件，Thesis 不可被客观推翻")
    else:
        conds = []
        for inv in invalidation:
            if isinstance(inv, dict):
                conds.append(str(inv.get("condition", "")))
            else:
                conds.append(str(inv))
        quantifiable = [c for c in conds if any(op in c for op in QUANT_OPS)]
        if not quantifiable:
            survival -= 25
            attack_points.append(
                "UNQUANTIFIABLE_INVALIDATION: 证伪条件不可量化，假设缺乏客观检验标准"
            )
        elif any("主观" in c for c in conds):
            survival -= 10
            attack_points.append(
                "SUBJECTIVE_INVALIDATION: 部分证伪条件依赖主观判断，可验证性存疑"
            )

    # 4. 与已知反证/失败模式冲突
    for warn in _get_warnings(validation_result):
        sev = normalize_severity(warn)
        if sev > 0.7:
            survival -= sev * 30
            rule_name = warn.get("rule_name", warn.get("pattern_name", "unknown"))
            attack_points.append(
                f"HISTORICAL_PATTERN_CONFLICT: 与已知反证'{rule_name}'高度匹配(severity={sev:.1f})"
            )

    return round(clamp(survival), 1), attack_points


def _get_warnings(validation_result):
    if validation_result is None:
        return []
    if isinstance(validation_result, dict):
        return validation_result.get("counter_warnings", []) or []
    return getattr(validation_result, "counter_warnings", []) or []
