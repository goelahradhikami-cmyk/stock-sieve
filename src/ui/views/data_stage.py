"""📡 数据阶段详情 — 阶段01: 股票池与行情数据新鲜度."""

import os
import sqlite3

import streamlit as st

from ..utils.theme import ACCENT_CYAN, TEXT_SECONDARY


def _cache_conn():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    return sqlite3.connect(os.path.join(root, "data", "cache.db"))


def render():
    st.markdown(
        """
    <div class="page-header">
        <h1>📡 数据 · 阶段 01</h1>
        <p>股票池构成与行情数据新鲜度 — 整条管线的输入层</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    conn = _cache_conn()
    try:
        # Canonical universe size from security_master (always authoritative,
        # 5540 clean stocks). tradable_universe is a derived snapshot that the
        # runner overwrote with stale/garbage rows, so it is NOT reliable here.
        uni_count = (
            conn.execute(
                "SELECT COUNT(*) FROM security_master WHERE status='active' AND is_st=0"
            ).fetchone()[0]
            or 0
        )
        last_refresh = (
            conn.execute("SELECT MAX(updated_at) FROM tradable_universe").fetchone()[0] or "—"
        )

        # market index last close
        idx_rows = conn.execute("""
            SELECT index_code, trade_date, close, volume
            FROM market_index_daily
            WHERE trade_date = (SELECT MAX(trade_date) FROM market_index_daily)
        """).fetchall()
    except Exception as e:
        st.error(f"读取 cache.db 失败: {e}")
        conn.close()
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("股票池规模", f"{uni_count} 只")
    m2.metric("最近刷新", str(last_refresh)[:10] if last_refresh else "—")
    # index cards
    idx_map = {"000300": "沪深300", "000905": "中证500", "000001": "上证综指"}
    m3.metric("指数条数", f"{len(idx_rows)}")
    m4.metric("最新交易日", idx_rows[0][1] if idx_rows else "—")

    # index detail cards
    if idx_rows:
        st.markdown("<br>", unsafe_allow_html=True)
        st.subheader("📈 市场指数（最新交易日）")
        cols = st.columns(4)
        for col, row in zip(cols, idx_rows, strict=False):
            with col:
                label = idx_map.get(row[0], row[0])
                st.markdown(
                    f"""
                <div class="terminal-card">
                    <div class="terminal-card-header">
                        <span class="terminal-card-title">{label}</span>
                        <span class="terminal-badge badge-info">{row[0]}</span>
                    </div>
                    <div class="mono" style="font-size:1.5rem;font-weight:700;color:{ACCENT_CYAN};">
                        {row[2]:.2f}
                    </div>
                    <div style="font-size:0.7rem;color:{TEXT_SECONDARY};margin-top:2px;">{row[1]} · 成交 {row[3] or 0:,.0f}</div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

    # universe table
    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("📋 股票池明细")
    try:
        rows = conn.execute("""
            SELECT s.security_id, s.name, s.industry, s.float_mv, s.is_st
            FROM security_master s
            WHERE s.status='active' AND s.is_st=0
            ORDER BY s.security_id
        """).fetchall()
    except Exception as e:
        st.warning(f"无法读取股票池明细: {e}")
        rows = []

    conn.close()

    if rows:
        import pandas as pd

        df = pd.DataFrame(rows, columns=["代码", "名称", "行业", "流通市值(亿)", "ST"])
        df["流通市值(亿)"] = df["流通市值(亿)"].apply(lambda v: f"{v:.1f}" if v else "—")
        df["ST"] = df["ST"].apply(lambda v: "⚠️" if v else "")
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("股票池为空 — security_master 中无 active 非 ST 标的。")

    st.caption("数据源: data/cache.db · security_master（权威股票池）+ market_index_daily")
