"""🔍 Thesis Tracker — Terminal-styled decision chain viewer."""
import streamlit as st

from ..components.decision_timeline import decision_timeline
from ..utils.data_loader import load_decision_timeline, load_stock_name
from ..utils.db_connector import get_db
from ..utils.theme import (
    ACCENT_CYAN,
)


def render():
    # ── Page header ──────────────────────────────────────
    st.markdown("""
    <div class="page-header">
        <h1>🔍 Thesis 追踪器</h1>
        <p>从 Research → Validator → Committee → Portfolio → 收益的完整决策链</p>
    </div>
    """, unsafe_allow_html=True)

    db = get_db()

    # ── Search bar ───────────────────────────────────────
    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        stock_code = st.text_input(
            "股票代码", "600519",
            placeholder="例如: 600519, 300308",
            key="thesis_stock_code",
        )
    with col2:
        time_range = st.selectbox(
            "时间范围",
            ["全部", "近30天", "近90天", "近1年"],
            key="thesis_time",
        )
    with col3:
        st.markdown("")
        st.markdown("")
        search = st.button("🔍 查询", use_container_width=True, type="primary")

    # ── Stock name lookup (from security_master) ─────────
    stock_name = load_stock_name(stock_code)

    if stock_code:
        st.markdown(f"""
        <div style="display:flex; align-items:baseline; gap:12px; margin:8px 0 4px;">
            <span style="font-size:1.2rem; font-weight:700; color:#e6edf3;">📈 {stock_name}</span>
            <span class="mono" style="color:{ACCENT_CYAN}; font-size:0.95rem;">{stock_code}</span>
            <span class="terminal-badge badge-info">{time_range}</span>
        </div>
        """, unsafe_allow_html=True)

    # ── Decision timeline ────────────────────────────────
    decisions = load_decision_timeline(db, stock_code)
    decision_timeline(decisions, stock_name)

    # ── Summary stats ────────────────────────────────────
    if decisions:
        alphas = [d.get("alpha_vs_market", 0) for d in decisions if d.get("alpha_vs_market") is not None]
        returns = [d.get("stock_return", 0) for d in decisions if d.get("stock_return") is not None]

        if alphas or returns:
            st.subheader("📊 统计摘要")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("决策总数", len(decisions))
            with c2:
                if returns:
                    avg_ret = sum(returns) / len(returns)
                    st.metric("平均收益", f"{avg_ret:+.1%}")
            with c3:
                if alphas:
                    avg_alpha = sum(alphas) / len(alphas)
                    st.metric("平均Alpha", f"{avg_alpha:+.1%}")
            with c4:
                if returns:
                    win_pct = sum(1 for r in returns if r > 0) / len(returns)
                    st.metric("胜率", f"{win_pct:.0%}")
