"""💼 组合阶段详情 — 阶段05: 头寸决策与权重推导链."""

import json

import pandas as pd
import streamlit as st

from ..utils.db_connector import get_db
from ..utils.theme import GAIN, LOSS, TEXT_SECONDARY


def render():
    st.markdown(
        """
    <div class="page-header">
        <h1>💼 组合 · 阶段 05</h1>
        <p>头寸构建决策 — 从 base_weight → kelly → regime 调整 → final_weight 的推导链</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    db = get_db()
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM portfolio_decisions").fetchone()[0] or 0
        latest = conn.execute(
            "SELECT MAX(decision_date), MAX(created_at) FROM portfolio_decisions"
        ).fetchone()
        rows = conn.execute("""
            SELECT pd.id, pd.decision_date, pd.agent_id, rd.security_id,
                   pd.base_weight, pd.kelly_weight, pd.regime_multiplier,
                   pd.risk_penalty, pd.final_weight, pd.market_regime,
                   pd.market_risk_score, pd.cash_level, pd.decision_trace,
                   pd.execution_instruction, pd.status
            FROM portfolio_decisions pd
            LEFT JOIN research_decisions rd ON rd.id = pd.research_decision_id
            ORDER BY pd.created_at DESC
            LIMIT 100
        """).fetchall()
    except Exception as e:
        st.error(f"读取 portfolio_decisions 失败: {e}")
        conn.close()
        return
    conn.close()

    avg_final = (sum((r[8] or 0) for r in rows) / len(rows)) if rows else 0
    zero_n = sum(1 for r in rows if (r[8] or 0) == 0)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("组合决策总数", f"{total}")
    m2.metric("最近决策日", str(latest[0] or "—"))
    m3.metric("平均最终权重", f"{avg_final:.1%}")
    m4.metric(
        "零权重(空仓)",
        f"{zero_n}",
        delta="熊市/风控触发" if zero_n else None,
        delta_color="inverse",
    )

    if not rows:
        st.info("暂无组合决策 — 需委员会 APPROVE 后才会产生 portfolio_decision。")
        return

    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("📋 组合决策明细")
    df = pd.DataFrame(
        rows,
        columns=[
            "ID",
            "决策日",
            "代理",
            "代码",
            "base",
            "kelly",
            "regime×",
            "风险扣",
            "final",
            "市场",
            "风险分",
            "现金",
            "trace",
            "执行指令",
            "状态",
        ],
    )
    show = df[
        [
            "决策日",
            "代理",
            "代码",
            "base",
            "kelly",
            "regime×",
            "风险扣",
            "final",
            "市场",
            "风险分",
            "状态",
        ]
    ]
    st.dataframe(
        show.style.format(
            {
                "base": "{:.1%}",
                "kelly": "{:.1%}",
                "regime×": "{:.2f}",
                "风险扣": "{:.2f}",
                "final": "{:.1%}",
                "风险分": "{:.0f}",
            }
        )
        .map(
            lambda v: (
                "color:#00e676;font-weight:bold"
                if isinstance(v, (int, float)) and v and v > 0
                else "color:#ff5252"
                if isinstance(v, (int, float)) and v is not None and v == 0
                else ""
            ),
            subset=["final"],
        )
        .map(
            lambda v: (
                "color:#ff5252"
                if isinstance(v, str) and v == "bear"
                else "color:#00e676"
                if isinstance(v, str) and v == "bull"
                else ""
            ),
            subset=["市场"],
        ),
        use_container_width=True,
        hide_index=True,
    )

    # decision trace expansion
    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("🔍 权重推导链（decision trace）")
    sel_id = st.selectbox(
        "选择决策查看推导链",
        [f"#{r[0]} {r[2]} · {r[3] or '?'} · {r[1]}" for r in rows],
        key="pd_trace_sel",
    )
    idx = [f"#{r[0]} {r[2]} · {r[3] or '?'} · {r[1]}" for r in rows].index(sel_id)
    trace_raw = rows[idx][12]
    try:
        trace = json.loads(trace_raw) if trace_raw else {}
    except Exception:
        trace = {"_raw": trace_raw}

    if trace:
        _ = st.columns(min(len(trace), 4)) or [st.container()]
        keys = list(trace.keys())
        for i in range(0, len(keys), 4):
            chunk = keys[i : i + 4]
            ccols = st.columns(len(chunk))
            for col, k in zip(ccols, chunk, strict=False):
                v = trace[k]
                with col:
                    st.markdown(
                        f"""
                    <div class="terminal-card">
                        <div style="font-size:0.72rem;color:{TEXT_SECONDARY};
                                    font-family:'JetBrains Mono',monospace;">{k}</div>
                        <div class="mono" style="font-size:1.2rem;font-weight:700;color:{GAIN if isinstance(v, (int, float)) and v > 0 else LOSS if isinstance(v, (int, float)) and v < 1 else "#e6edf3"};">
                            {v if not isinstance(v, float) else f"{v:.3f}"}
                        </div>
                    </div>
                    """,
                        unsafe_allow_html=True,
                    )
        st.caption(f"执行指令: {rows[idx][13]} · 状态: {rows[idx][14]}")

    st.caption("数据源: data/evaluation.db · portfolio_decisions · decision_trace (JSON)")
