"""🏛️ Investment Committee Room — Terminal-styled governance dashboard."""

import streamlit as st

from ..components.committee_card import committee_card
from ..utils.data_loader import load_committee_history
from ..utils.db_connector import get_db
from ..utils.theme import (
    ACCENT_CYAN,
    verdict_badge,
)


def render():
    # ── Page header ──────────────────────────────────────
    st.markdown(
        """
    <div class="page-header">
        <h1>🏛️ 投资委员会会议室</h1>
        <p>旁听 AI 投资委员会的辩论 — 五角色评分 · 魔鬼代言人攻击 · 主席裁决</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    db = get_db()
    history = load_committee_history(db)

    if not history:
        st.info("暂无委员会会议记录。运行一次投资决策流程后，会议记录将在此展示。")
        st.caption(
            "提示：使用 CLI 运行 `python -m src.cli screen --personality value_purist` 生成决策数据"
        )
        return

    # ── Meeting selector ─────────────────────────────────
    meeting_labels = [
        f"{m['created_at'][:10]} — {m.get('security_id', '?')} — {m.get('verdict', '?')}"
        for m in history
    ]

    selected_idx = st.selectbox(
        "选择会议",
        range(len(history)),
        format_func=lambda i: meeting_labels[i],
        key="committee_selector",
    )

    decision = history[selected_idx]

    # ── Meeting summary card ─────────────────────────────
    with st.container(border=True):
        cols = st.columns(3)
        with cols[0]:
            st.markdown(f"**📅 {decision.get('created_at', '?')[:10]}**")
            st.caption(f"时间: {decision.get('created_at', '?')[11:16]}")
        with cols[1]:
            st.markdown(f"**📈 {decision.get('security_id', '?')}**")
            st.caption(f"研究员: `{decision.get('research_agent_id', '?')}`")
        with cols[2]:
            conf = decision.get("confidence", 0)
            st.metric("Confidence", f"{conf:.1f}")

    # ── Committee card ───────────────────────────────────
    committee_card(decision)

    # ── Historical list ──────────────────────────────────
    with st.expander("📋 历史会议列表"):
        for m in history[:10]:
            badge = verdict_badge(m.get("verdict", ""))
            ws = m.get("weighted_score", 0)
            st.markdown(
                f"{badge} &nbsp; "
                f"`{m.get('created_at', '?')[:10]}` &nbsp; "
                f"**{m.get('security_id', '?')}** &nbsp; "
                f'<span class="mono" style="color:{ACCENT_CYAN};">ws={ws:.1f}</span>',
                unsafe_allow_html=True,
            )
