"""🧮 因子阶段详情 — 阶段02: 信号快照与因子分值."""

import json

import pandas as pd
import streamlit as st

from ..utils.db_connector import get_db
from ..utils.theme import ACCENT_CYAN, GAIN, LOSS, TEXT_SECONDARY, WARN


def render():
    st.markdown(
        """
    <div class="page-header">
        <h1>🧮 因子 · 阶段 02</h1>
        <p>各代理产生的因子快照 — quality / value 等家族分值与信号强度</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    db = get_db()
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM signal_snapshot").fetchone()[0] or 0
        latest = conn.execute(
            "SELECT MAX(signal_date), MAX(created_at) FROM signal_snapshot"
        ).fetchone()
        n_stocks = (
            conn.execute("SELECT COUNT(DISTINCT security_id) FROM signal_snapshot").fetchone()[0]
            or 0
        )
        n_agents = (
            conn.execute("SELECT COUNT(DISTINCT agent_id) FROM signal_snapshot").fetchone()[0] or 0
        )
        regimes = conn.execute(
            "SELECT market_regime, COUNT(*) FROM signal_snapshot GROUP BY market_regime"
        ).fetchall()
    except Exception as e:
        st.error(f"读取 signal_snapshot 失败: {e}")
        conn.close()
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("因子快照总数", f"{total}")
    m2.metric("最近信号日", str(latest[0] or "—"))
    m3.metric("覆盖股票", f"{n_stocks} 只")
    m4.metric("参与代理", f"{n_agents} 个")

    # regime distribution
    if regimes:
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("🌡️ 市场状态分布")
        cols = st.columns(4)
        regime_color = {"bull": GAIN, "bear": LOSS, "rotation": WARN, "volatile": WARN}
        for col, r in zip(cols, regimes, strict=False):
            with col:
                color = regime_color.get(r[0], ACCENT_CYAN)
                st.markdown(
                    f"""
                <div class="terminal-card" style="text-align:center;">
                    <div class="mono" style="font-size:1.4rem;font-weight:700;color:{color};">{r[1]}</div>
                    <div style="font-size:0.75rem;color:{TEXT_SECONDARY};margin-top:4px;">{r[0] or "未知"}</div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

    # factor snapshot table
    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("🔬 最近因子快照")
    try:
        rows = conn.execute("""
            SELECT signal_date, security_id, agent_id, factor_values,
                   alpha_score, confidence, action, signal_strength, market_regime
            FROM signal_snapshot
            ORDER BY created_at DESC
            LIMIT 200
        """).fetchall()
    except Exception as e:
        st.warning(f"无法读取快照明细: {e}")
        rows = []
    conn.close()

    if rows:
        records = []
        for r in rows:
            try:
                fv = json.loads(r[3]) if r[3] else {}
            except Exception:
                fv = {}
            records.append(
                {
                    "信号日": r[0],
                    "代码": r[1],
                    "代理": r[2],
                    "quality": fv.get("quality"),
                    "value": fv.get("value"),
                    "alpha": r[4],
                    "信心": r[5],
                    "动作": r[6],
                    "强度": r[7],
                    "市场": r[8],
                }
            )
        df = pd.DataFrame(records)
        st.dataframe(
            df.style.map(
                lambda v: (
                    "color:#00e676"
                    if isinstance(v, (int, float)) and v and v > 30
                    else "color:#ff5252"
                    if isinstance(v, (int, float)) and v is not None and v < 15
                    else ""
                ),
                subset=["quality", "value"],
            )
            .map(
                lambda v: (
                    "color:#00e676"
                    if isinstance(v, (int, float)) and v and v > 4
                    else "color:#ff5252"
                    if isinstance(v, (int, float)) and v is not None and v < 3
                    else ""
                ),
                subset=["alpha"],
            )
            .format(
                {
                    "quality": "{:.1f}",
                    "value": "{:.1f}",
                    "alpha": "{:.1f}",
                    "信心": "{:.1f}",
                    "强度": "{:.2f}",
                },
                na_rep="—",
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("暂无因子快照 — 需先运行 daily_run 产生 signal_snapshot。")

    st.caption("数据源: data/evaluation.db · signal_snapshot · factor_values (JSON)")
