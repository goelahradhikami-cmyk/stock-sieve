"""🏆 Agent Leaderboard — Terminal-styled personality ranking."""
import plotly.graph_objects as go
import streamlit as st

from ..components.agent_radar import agent_radar
from ..utils.data_loader import load_identity_vectors, load_leaderboard
from ..utils.db_connector import get_db
from ..utils.theme import (
    ACCENT_CYAN,
    GAIN,
    GRID_COLOR,
    LOSS,
    TEXT_SECONDARY,
    WARN,
    terminal_layout,
)


def render():
    # ── Page header ──────────────────────────────────────
    st.markdown("""
    <div class="page-header">
        <h1>🏆 Agent 排行榜</h1>
        <p>基于 personality_score 的实时排名 — 每个 AI 基金经理的信誉档案</p>
    </div>
    """, unsafe_allow_html=True)

    db = get_db()
    df = load_leaderboard(db)
    identity = load_identity_vectors()

    if df.empty:
        st.info(
            "暂无绩效排名：首批真实决策刚产生，需 T+N 回测后才有可信的 "
            "收益 / Sharpe / 胜率。下方可先查看各 Agent 的人格画像。"
        )

    # ── Top 3 summary cards ──────────────────────────────
    if not df.empty:
        top3 = df.head(3)
        top_cols = st.columns(3)
        medals = ["🥇", "🥈", "🥉"]
        for i, (_, row) in enumerate(top3.iterrows()):
            with top_cols[i]:
                agent_short = row["agent_id"].replace("_v1", "")
                st.markdown(f"""
                <div class="terminal-card">
                    <div class="terminal-card-header">
                        <span class="terminal-card-title">{medals[i]} {agent_short}</span>
                        <span class="terminal-badge badge-info">#{i+1}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between;">
                        <div>
                            <div class="mono" style="font-size:1.5rem; font-weight:700; color:{ACCENT_CYAN};">
                                {row['personality_score']:.2f}
                            </div>
                            <div style="font-size:0.7rem; color:{TEXT_SECONDARY};">综合分</div>
                        </div>
                        <div style="text-align:right;">
                            <div class="mono" style="color:{GAIN if row['total_return'] > 0 else LOSS};">
                                {row['total_return']:+.1%}
                            </div>
                            <div style="font-size:0.7rem; color:{TEXT_SECONDARY};">收益</div>
                        </div>
                        <div style="text-align:right;">
                            <div class="mono">{row['sharpe_ratio']:.2f}</div>
                            <div style="font-size:0.7rem; color:{TEXT_SECONDARY};">Sharpe</div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

    # ── Ranking table ────────────────────────────────────
    if not df.empty:
        st.subheader("📊 综合排名")

        styled = df.copy()
        styled["#"] = range(1, len(df) + 1)
        styled = styled.rename(columns={
            "agent_id": "Agent",
            "personality_score": "综合分",
            "total_return": "收益",
            "sharpe_ratio": "Sharpe",
            "max_drawdown": "最大回撤",
            "win_rate": "胜率",
            "alpha_vs_market": "Alpha",
        })
        cols = ["#", "Agent", "综合分", "收益", "Sharpe", "最大回撤", "胜率", "Alpha"]

        st.dataframe(
            styled[cols].style
            .map(
                lambda v: "color: #00e676; font-weight: bold" if isinstance(v, (int, float)) and v >= 0.80
                else "color: #ffab40" if isinstance(v, (int, float)) and v >= 0.70
                else "color: #ff5252" if isinstance(v, (int, float)) and v < 0.60
                else "",
                subset=["综合分"],
            )
            .map(
                lambda v: "color: #00e676" if isinstance(v, (int, float)) and v > 0
                else "color: #ff5252" if isinstance(v, (int, float)) and v < 0
                else "",
                subset=["收益", "Alpha"],
            )
            .format({
                "综合分": "{:.2f}", "收益": "{:.1%}", "Sharpe": "{:.2f}",
                "最大回撤": "{:.1%}", "胜率": "{:.0%}", "Alpha": "{:.1%}",
            }),
            use_container_width=True,
            hide_index=True,
        )

    # ── Agent detail (radar + trend) ─────────────────────
    st.subheader("🔍 Agent 详情")

    agent_ids = sorted(identity.keys())
    col1, col2 = st.columns(2)
    with col1:
        selected = st.selectbox("选择 Agent", agent_ids, key="radar_agent")
    with col2:
        compare = st.selectbox("对比 Agent（可选）", ["无"] + agent_ids, key="radar_compare")

    compare_list = [selected]
    if compare != "无" and compare != selected:
        compare_list.append(compare)

    agent_radar(identity, compare_list)

    # ── Performance trend ────────────────────────────────
    st.subheader("📈 绩效趋势（季度）")

    from ..utils.data_loader import load_performance_trend
    trend_df = load_performance_trend(db, selected)
    if not trend_df.empty and len(trend_df) >= 2:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=trend_df["period_end"].astype(str),
            y=trend_df["personality_score"],
            mode="lines+markers",
            line=dict(color=ACCENT_CYAN, width=3),
            marker=dict(size=8, color=ACCENT_CYAN, line=dict(color="#fff", width=1)),
            name="personality_score",
            fill="tozeroy",
            fillcolor="rgba(0, 188, 212, 0.08)",
        ))
        fig.add_hline(y=0.70, line_dash="dash", line_color=WARN,
                       annotation_text="精英线", annotation_font_color=WARN)
        fig.add_hline(y=0.20, line_dash="dash", line_color=LOSS,
                       annotation_text="生存线", annotation_font_color=LOSS)
        layout = terminal_layout()
        layout.update(
            yaxis=dict(range=[0, 1.0], title="personality_score",
                       gridcolor=GRID_COLOR, tickfont=dict(color=TEXT_SECONDARY)),
            height=280,
        )
        fig.update_layout(**layout)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("暂无足够的历史季度数据绘制趋势 — 需要至少 2 个季度的 personality_score 记录。")

    # ── Performance metrics ──────────────────────────────
    agent_data = df[df["agent_id"] == selected]
    if not agent_data.empty:
        row = agent_data.iloc[0]
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("综合分", f"{row['personality_score']:.2f}")
        m2.metric("收益", f"{row['total_return']:.1%}")
        m3.metric("Sharpe", f"{row['sharpe_ratio']:.2f}")
        m4.metric("回撤", f"{row['max_drawdown']:.1%}")
        m5.metric("胜率", f"{row['win_rate']:.0%}")
