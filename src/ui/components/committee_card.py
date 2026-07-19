"""Committee Card — Terminal-styled role scoring dashboard."""
import json

import plotly.graph_objects as go
import streamlit as st

from ..utils.theme import (
    GAIN,
    LOSS,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    WARN,
    terminal_layout,
    verdict_badge,
)


def committee_card(decision: dict):
    """Render a committee decision card with role scores."""

    # ── Header with verdict badge ────────────────────────
    verdict = decision.get("verdict", "UNKNOWN")
    badge_html = verdict_badge(verdict)

    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    with col1:
        st.markdown(f"### {badge_html}", unsafe_allow_html=True)
        st.caption(decision.get("verdict_reason", ""))
    with col2:
        st.metric("加权分", f"{decision.get('weighted_score', 0):.1f}")
    with col3:
        st.metric("仓位上限", f"{decision.get('position_cap_modifier', 1.0):.0%}")
    with col4:
        st.metric("Alpha", f"{decision.get('alpha_score', 0):.1f}")

    # ── Role scores bar chart (terminal themed) ──────────
    roles = ["valuation", "industry", "risk", "quant", "devil_advocate"]
    role_labels = ["估值审查", "行业审查", "风控官", "量化审计", "魔鬼代言人"]
    role_weights = [0.20, 0.20, 0.25, 0.15, 0.20]
    scores = [decision.get(f"{r}_score", 50) for r in roles]

    fig = go.Figure()
    colors_bar = [GAIN if s >= 70 else WARN if s >= 50 else LOSS for s in scores]

    fig.add_trace(go.Bar(
        x=scores,
        y=role_labels,
        orientation="h",
        marker_color=colors_bar,
        text=[f"{s:.0f}" for s in scores],
        textposition="outside",
        textfont=dict(color=TEXT_PRIMARY, size=12),
    ))

    for i, (s, w) in enumerate(zip(scores, role_weights)):
        fig.add_annotation(
            x=s + 2, y=i,
            text=f"×{w:.0%} = {s*w:.1f}",
            showarrow=False,
            font=dict(size=10, color=TEXT_SECONDARY),
        )

    layout = terminal_layout()
    layout.update(
        xaxis=dict(range=[0, 110], title="评分"),
        showlegend=False,
        height=240,
        margin=dict(l=10, r=50, t=10, b=10),
    )
    fig.update_layout(**layout)
    st.plotly_chart(fig, use_container_width=True)

    # ── Devil's Advocate attack points ───────────────────
    attack_raw = decision.get("devil_advocate_attack_points_json", "[]")
    try:
        attack_points = json.loads(attack_raw) if isinstance(attack_raw, str) else attack_raw
    except (json.JSONDecodeError, TypeError):
        attack_points = []

    if attack_points:
        st.markdown("#### ⚠️ 魔鬼代言人攻击点")
        for ap in attack_points:
            st.warning(ap)

    # ── Member statements ────────────────────────────────
    statements_raw = decision.get("member_statements_json", "{}")
    try:
        statements = json.loads(statements_raw) if isinstance(statements_raw, str) else statements_raw
    except (json.JSONDecodeError, TypeError):
        statements = {}

    if statements:
        with st.expander("📝 委员陈述", expanded=False):
            role_names = {
                "valuation_reviewer": "估值审查员",
                "industry_reviewer": "行业审查员",
                "risk_controller": "风控官",
                "quant_auditor": "量化审计员",
                "devil_advocate": "魔鬼代言人",
            }
            for role_key, stmt in statements.items():
                if stmt:
                    st.markdown(
                        f"**{role_names.get(role_key, role_key)}**: {stmt}"
                    )

    # ── Monitoring flags ─────────────────────────────────
    flags_raw = decision.get("monitoring_flags_json", "[]")
    try:
        flags = json.loads(flags_raw) if isinstance(flags_raw, str) else flags_raw
    except (json.JSONDecodeError, TypeError):
        flags = []

    if flags:
        st.caption("🔍 监控条件: " + ", ".join(flags))
