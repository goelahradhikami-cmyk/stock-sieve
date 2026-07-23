"""⚡ 执行阶段详情 — 阶段06: 模拟成交与市场摩擦拆解."""

import pandas as pd
import streamlit as st

from ..utils.db_connector import get_db
from ..utils.theme import ACCENT_CYAN, LOSS, TEXT_SECONDARY, WARN


def render():
    st.markdown(
        """
    <div class="page-header">
        <h1>⚡ 执行 · 阶段 06</h1>
        <p>ExecutionSimulator 产出的模拟成交 — 滑点/佣金/印花税/过户费的摩擦拆解</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    db = get_db()
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM portfolio_execution").fetchone()[0] or 0
        last_date = conn.execute("SELECT MAX(execution_date) FROM portfolio_execution").fetchone()[
            0
        ]
        # how many portfolio_decisions had final_weight=0 (=> no order)
        zero_weight = (
            conn.execute(
                "SELECT COUNT(*) FROM portfolio_decisions WHERE final_weight=0"
            ).fetchone()[0]
            or 0
        )
        rows = conn.execute("""
            SELECT pe.id, pe.execution_date, pe.agent_id, pe.security_id, pe.action,
                   pe.order_price, pe.fill_price, pe.quantity, pe.slippage,
                   pe.commission, pe.stamp_tax, pe.transfer_fee, pe.total_cost,
                   pe.execution_status, pe.execution_mode
            FROM portfolio_execution pe
            ORDER BY pe.created_at DESC
            LIMIT 100
        """).fetchall()
    except Exception as e:
        st.error(f"读取 portfolio_execution 失败: {e}")
        conn.close()
        return
    conn.close()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("模拟成交总数", f"{total}")
    m2.metric("最近成交日", str(last_date or "—"))
    m3.metric("零权重决策(未下单)", f"{zero_weight}")
    m4.metric("执行模式", "PAPER")

    if not rows:
        st.markdown("<br>", unsafe_allow_html=True)
        st.warning(
            "尚无模拟成交记录。常见原因: 组合代理在熊市/高风险行情下将 final_weight 算为 0, "
            "从而不下单。可在「组合」阶段查看 portfolio_decisions 的权重推导链, "
            "确认是否因 regime=risk 触发空仓。"
        )
        st.info(
            "驱动产生成交: 跑 `python -m src.daily_run`,当某只股票 final_weight>0 时, "
            "ExecutionSimulator 会模拟下单并写入 portfolio_execution。"
        )
        return

    # aggregate friction
    total_cost = sum((r[12] or 0) for r in rows)
    total_slip = sum((r[8] or 0) for r in rows)
    total_comm = sum((r[9] or 0) for r in rows)
    total_tax = sum((r[10] or 0) for r in rows)
    total_fee = sum((r[11] or 0) for r in rows)
    st.markdown(
        f"""
    <div class="terminal-card" style="display:flex;gap:24px;justify-content:space-around;">
        <div style="text-align:center;">
            <div class="mono" style="font-size:1.4rem;color:#e6edf3;">¥{total_cost:,.2f}</div>
            <div style="font-size:0.72rem;color:{TEXT_SECONDARY};">总摩擦成本</div>
        </div>
        <div style="text-align:center;">
            <div class="mono" style="font-size:1.4rem;color:{ACCENT_CYAN};">¥{total_comm:,.2f}</div>
            <div style="font-size:0.72rem;color:{TEXT_SECONDARY};">佣金</div>
        </div>
        <div style="text-align:center;">
            <div class="mono" style="font-size:1.4rem;color:{WARN};">¥{total_slip:,.2f}</div>
            <div style="font-size:0.72rem;color:{TEXT_SECONDARY};">滑点</div>
        </div>
        <div style="text-align:center;">
            <div class="mono" style="font-size:1.4rem;color:{LOSS};">¥{total_tax:,.2f}</div>
            <div style="font-size:0.72rem;color:{TEXT_SECONDARY};">印花税</div>
        </div>
        <div style="text-align:center;">
            <div class="mono" style="font-size:1.4rem;color:{TEXT_SECONDARY};">¥{total_fee:,.2f}</div>
            <div style="font-size:0.72rem;color:{TEXT_SECONDARY};">过户费</div>
        </div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("📋 成交明细")
    df = pd.DataFrame(
        rows,
        columns=[
            "ID",
            "成交日",
            "代理",
            "代码",
            "动作",
            "委托价",
            "成交价",
            "数量",
            "滑点",
            "佣金",
            "印花税",
            "过户费",
            "总成本",
            "状态",
            "模式",
        ],
    )
    show = df[
        ["成交日", "代理", "代码", "动作", "委托价", "成交价", "数量", "滑点", "总成本", "状态"]
    ]
    st.dataframe(
        show.style.format(
            {
                "委托价": "{:.2f}",
                "成交价": "{:.2f}",
                "滑点": "{:.4f}",
                "总成本": "¥{:,.2f}",
                "数量": "{:.0f}",
            }
        )
        .map(
            lambda v: (
                "color:#00e676"
                if isinstance(v, str) and v == "filled"
                else "color:#ff5252"
                if isinstance(v, str) and v == "rejected"
                else ""
            ),
            subset=["状态"],
        )
        .map(
            lambda v: (
                "color:#00e676"
                if isinstance(v, str) and v == "BUY"
                else "color:#ff5252"
                if isinstance(v, str) and v == "SELL"
                else ""
            ),
            subset=["动作"],
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.caption("数据源: data/evaluation.db · portfolio_execution (ExecutionSimulator)")
