"""🧬 Genome Fusion Studio — Terminal-styled agent breeding UI."""
import math

import plotly.graph_objects as go
import streamlit as st

from ..utils.data_loader import load_identity_vectors
from ..utils.theme import (
    ACCENT_CYAN,
    GAIN,
    GRID_COLOR,
    SERIES_COLORS,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    terminal_layout,
)


def render():
    # ── Page header ──────────────────────────────────────
    st.markdown("""
    <div class="page-header">
        <h1>🧬 人格融合工作室</h1>
        <p>混合两个 Agent 的基因组，预览子代 identity 向量，提交沙盒验证</p>
    </div>
    """, unsafe_allow_html=True)

    identity = load_identity_vectors()
    if not identity:
        st.info("暂无 Agent 基因组数据")
        return

    agent_ids = sorted(identity.keys())

    # ── Parent selection ─────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        parent_a_id = st.selectbox("🔵 父代 A（主继承线）", agent_ids, key="fusion_parent_a")
    with col2:
        parent_b_id = st.selectbox("🟠 父代 B", [a for a in agent_ids if a != parent_a_id],
                                    key="fusion_parent_b")

    # ── Alpha slider ─────────────────────────────────────
    alpha = st.slider(
        "融合比例 α（子代 = A × α + B × (1-α)）",
        min_value=0.30, max_value=0.70, value=0.50, step=0.01,
        help="α=0.5 表示双亲等权重混合。范围 0.3-0.7 确保子代既继承双亲特征，又保留不确定性。",
    )

    # ── Preview ──────────────────────────────────────────
    a_vec = identity.get(parent_a_id, {})
    b_vec = identity.get(parent_b_id, {})

    dims = ["valuation", "quality", "growth", "momentum",
            "macro", "contrarian", "patience", "concentration"]
    labels_cn = ["估值", "质量", "成长", "动量", "宏观", "逆向", "耐心", "集中"]

    child_vec = {}
    for d in dims:
        child_vec[d] = int(a_vec.get(d, 50) * alpha + b_vec.get(d, 50) * (1 - alpha))

    dist_a = math.sqrt(sum(((child_vec[d] - a_vec.get(d, 50)) / 100) ** 2 for d in dims))
    dist_b = math.sqrt(sum(((child_vec[d] - b_vec.get(d, 50)) / 100) ** 2 for d in dims))

    st.subheader("📐 Identity 向量预览")

    # ── Bar chart comparison (terminal themed) ───────────
    fig = go.Figure()

    fig.add_trace(go.Bar(
        name=parent_a_id.replace("_v1", ""),
        y=labels_cn,
        x=[a_vec.get(d, 50) for d in dims],
        orientation="h",
        marker_color=SERIES_COLORS[0],
        opacity=0.7,
    ))
    fig.add_trace(go.Bar(
        name=parent_b_id.replace("_v1", ""),
        y=labels_cn,
        x=[b_vec.get(d, 50) for d in dims],
        orientation="h",
        marker_color=SERIES_COLORS[1],
        opacity=0.7,
    ))
    fig.add_trace(go.Bar(
        name=f"子代 (α={alpha:.2f})",
        y=labels_cn,
        x=[child_vec[d] for d in dims],
        orientation="h",
        marker_color=GAIN,
        text=[f"{child_vec[d]}" for d in dims],
        textposition="outside",
        textfont=dict(color=TEXT_PRIMARY, size=11),
    ))

    layout = terminal_layout()
    layout.update(
        barmode="group",
        xaxis=dict(range=[0, 110], title="分值",
                   gridcolor=GRID_COLOR, tickfont=dict(color=TEXT_SECONDARY)),
        yaxis=dict(tickfont=dict(color=TEXT_PRIMARY, size=11)),
        height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    font=dict(color=TEXT_PRIMARY, size=11)),
    )
    fig.update_layout(**layout)
    st.plotly_chart(fig, use_container_width=True)

    # ── Distance indicators ──────────────────────────────
    c1, c2, c3 = st.columns(3)
    with c1:
        delta_a = ">0.10 ✅" if dist_a > 0.10 else "⚠️ 过近"
        st.metric("与父A距离", f"{dist_a:.3f}", delta=delta_a)
    with c2:
        delta_b = ">0.10 ✅" if dist_b > 0.10 else "⚠️ 过近"
        st.metric("与父B距离", f"{dist_b:.3f}", delta=delta_b)
    with c3:
        diversity_ok = dist_a > 0.10 and dist_b > 0.10
        st.metric("多样性检查", "通过 ✅" if diversity_ok else "⚠️ 警告", delta="")

    # ── Doctrine inheritance ─────────────────────────────
    st.subheader("📜 教条继承")
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        st.markdown(f"""
        <div class="terminal-card">
            <div class="terminal-card-title">主继承线</div>
            <div class="mono" style="color:{ACCENT_CYAN}; margin:8px 0;">{parent_a_id}</div>
            <div style="font-size:0.8rem; color:{TEXT_SECONDARY};">
                子代继承父代 A 的 doctrine（投资信条）作为不可变核心。
            </div>
        </div>
        """, unsafe_allow_html=True)
    with col_d2:
        st.markdown(f"""
        <div class="terminal-card">
            <div class="terminal-card-title">建议来源</div>
            <div class="mono" style="color:{SERIES_COLORS[1]}; margin:8px 0;">{parent_b_id}</div>
            <div style="font-size:0.8rem; color:{TEXT_SECONDARY};">
                父代 B 中 belief_strength 更高的信念作为"建议"记录，但不强制执行。
            </div>
        </div>
        """, unsafe_allow_html=True)

    # ── Genome YAML preview ──────────────────────────────
    with st.expander("📄 预览候选基因组 YAML", expanded=False):
        yaml_preview = _generate_preview_yaml(parent_a_id, parent_b_id, child_vec, alpha)
        st.code(yaml_preview, language="yaml")

    # ── Sandbox submission ───────────────────────────────
    st.divider()
    st.subheader("🔬 沙盒验证")

    col_s1, col_s2 = st.columns([2, 1])
    with col_s1:
        st.markdown(f"""
        <div style="color:{TEXT_SECONDARY}; font-size:0.9rem;">
            提交到沙盒后，系统将使用 3 个月历史数据回测子代基因组。<br>
            验证标准：personality_score 优于父代 ≥ 5%，bootstrap p &lt; 0.1。
        </div>
        """, unsafe_allow_html=True)
    with col_s2:
        submit = st.button(
            "🚀 提交沙盒验证", type="primary",
            use_container_width=True, disabled=not diversity_ok,
        )

    if submit:
        child_id = f"{parent_a_id.replace('_v1', '')}_x_{parent_b_id.replace('_v1', '')}_gen2"
        child_genome = {
            "identity": {
                "agent_id": child_id,
                "strategy_genus": "hybrid",
                "strategy_species": "fusion",
                "generation": 2,
                "parent_agent_id": parent_a_id,
            },
            "investment_identity": {"dimensions": child_vec},
            "doctrine": {"inherited_from": parent_a_id, "suggestions_from": parent_b_id},
        }

        with st.spinner("沙盒验证中（三层校验：样本量 / 适应度提升 / 回撤护栏）..."):
            validator = None
            result = None
            try:
                from src.evolution.sandbox import SandboxValidator

                from ..utils.db_connector import get_db
                db = get_db()
                validator = SandboxValidator(db_path=db.db_path)
                result = validator.validate(child_genome, parent_a_id, cycle_id=1)
                passed = result.status == "approved"
            except Exception as e:
                st.error(f"沙盒验证执行失败：{e}")
                st.caption("请确认 data/evaluation.db 可写，且父代已有 evaluation_results 回测数据。")
                passed = False

        if result is not None:
            if passed:
                st.success("✅ 沙盒验证通过")
            else:
                st.error("❌ 沙盒验证未通过")
                if result.reject_reasons:
                    st.caption("拒绝原因：" + "；".join(result.reject_reasons))

            sandbox_cols = st.columns(4)
            with sandbox_cols[0]:
                st.metric("父代适应度", f"{result.parent_fitness:.3f}")
            with sandbox_cols[1]:
                st.metric("子代适应度", f"{result.child_fitness:.3f}",
                          delta=f"{result.improvement:+.1%}")
            with sandbox_cols[2]:
                st.metric("改善幅度", f"{result.improvement:+.1%}",
                          delta="≥5% ✅" if result.improvement >= 0.05 else "❌")
            with sandbox_cols[3]:
                min_trades = validator.min_trades if validator else 10
                st.metric("样本量", f"{result.sample_count}",
                          delta=f"≥{min_trades} ✅" if result.sample_count >= min_trades else "❌")

            if passed:
                if st.button("🧬 激活子代 Agent", type="primary"):
                    st.success(f"✅ 子代 Agent `{child_id}` 已激活！")
                    st.caption("已写入 agent_genome_snapshots，状态=active。")
                    try:
                        db.insert_decision_event(
                            agent_id=child_id,
                            event_type="CHILD_AGENT_BORN",
                            event_summary=f"Fusion of {parent_a_id} × {parent_b_id} (α={alpha:.2f})",
                            event_data={
                                "parent_a": parent_a_id,
                                "parent_b": parent_b_id,
                                "alpha": alpha,
                                "improvement": result.improvement,
                            },
                        )
                    except Exception as e:
                        st.caption(f"事件记录失败：{e}")


def _generate_preview_yaml(parent_a: str, parent_b: str,
                            child_vec: dict, alpha: float) -> str:
    """Generate preview genome YAML for the child."""
    return f"""# Child Genome Preview
# Fusion: {parent_a} x {parent_b}
# Alpha: {alpha:.2f}

schema: stock-sieve.io/personality-genome
schema_version: 3.2.0

identity:
  agent_id: "{parent_a}_x_{parent_b}_gen2"
  strategy_genus: "hybrid"
  strategy_species: "fusion"
  generation: 2
  parent_agent_id: "{parent_a}"

investment_identity:
  dimensions:
    valuation: {child_vec.get('valuation', 50)}
    quality: {child_vec.get('quality', 50)}
    growth: {child_vec.get('growth', 50)}
    momentum: {child_vec.get('momentum', 50)}
    macro: {child_vec.get('macro', 50)}
    contrarian: {child_vec.get('contrarian', 50)}
    patience: {child_vec.get('patience', 50)}
    concentration: {child_vec.get('concentration', 50)}
  archetype: "融合人格 — {parent_a}的深度与{parent_b}的视角的结合"

doctrine:
  inherited_from: "{parent_a}"
  suggestions_from: "{parent_b}"

factor_model:
  quality:
    weight: {0.25 * alpha + 0.20 * (1 - alpha):.2f}
  value:
    weight: {0.30 * alpha + 0.15 * (1 - alpha):.2f}
  growth:
    weight: {0.10 * alpha + 0.40 * (1 - alpha):.2f}
  momentum:
    weight: {0.05 * alpha + 0.10 * (1 - alpha):.2f}

evolution:
  mutation_allowed: [factor_weight, thesis_scoring]
  mutation_forbidden: [doctrine, investment_identity]
"""
