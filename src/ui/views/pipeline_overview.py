"""📊 管线总览 — 端到端工作流可视化主页.

把 stock-sieve 的 8 阶段流水线 (数据 → 因子 → 研究 → 委员会 → 组合 → 执行 → 评估 → 进化)
画成一张可视化管线,每阶段显示实时状态/计数/最近产出,一眼看清流程走到哪一步.
"""

import os
import sqlite3

import streamlit as st

from src.utils.logger import get_logger

from ..nav import label_for
from ..utils.db_connector import get_db
from ..utils.theme import (
    ACCENT_CYAN,
    BG_SECONDARY,
    BORDER_COLOR,
    GAIN,
    LOSS,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    WARN,
)

logger = get_logger(__name__)

# ── Pipeline-specific CSS ───────────────────────────────────
PIPELINE_CSS = f"""
<style>
.pipeline-strip {{
    display: flex;
    align-items: stretch;
    gap: 0;
    overflow-x: auto;
    padding: 4px 2px 14px 2px;
    margin-bottom: 8px;
}}
.pipeline-stage {{
    flex: 1 1 0;
    min-width: 132px;
    background: {BG_SECONDARY};
    border: 1px solid {BORDER_COLOR};
    border-radius: 10px;
    padding: 14px 12px 12px 12px;
    position: relative;
    transition: border-color .15s, transform .15s;
    display: flex;
    flex-direction: column;
    gap: 6px;
}}
.pipeline-stage:hover {{
    border-color: {ACCENT_CYAN};
    transform: translateY(-2px);
}}
.pipeline-stage.is-active {{ border-top: 3px solid {GAIN}; }}
.pipeline-stage.is-idle {{ border-top: 3px solid {WARN}; opacity: .92; }}
.pipeline-stage.is-empty {{ border-top: 3px solid {LOSS}; opacity: .8; }}

.ps-head {{
    display: flex; align-items: center; justify-content: space-between;
}}
.ps-num {{
    font-family: 'JetBrains Mono','Consolas',monospace;
    font-size: .7rem; color: {TEXT_SECONDARY}; letter-spacing: 1px;
}}
.ps-dot {{
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
}}
.ps-dot.active {{ background: {GAIN}; box-shadow: 0 0 6px {GAIN}; animation: pulse 2s infinite; }}
.ps-dot.idle {{ background: {WARN}; }}
.ps-dot.empty {{ background: {LOSS}; }}

.ps-icon {{ font-size: 1.4rem; line-height: 1; margin-top: 2px; }}
.ps-name {{
    font-size: .82rem; font-weight: 600; color: {TEXT_PRIMARY};
    font-family: 'JetBrains Mono','Consolas',monospace;
}}
.ps-metric {{
    font-family: 'JetBrains Mono','Consolas',monospace;
    font-size: 1.35rem; font-weight: 700; color: {ACCENT_CYAN};
    margin-top: 2px;
}}
.ps-sub {{
    font-size: .68rem; color: {TEXT_SECONDARY};
    font-family: 'JetBrains Mono','Consolas',monospace;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}

.ps-arrow {{
    display: flex; align-items: center; justify-content: center;
    color: {BORDER_COLOR}; font-size: 1.1rem; padding: 0 4px;
    flex: 0 0 auto;
}}
.ps-arrow.done {{ color: {GAIN}; }}

.drill-card {{
    background: {BG_SECONDARY}; border: 1px solid {BORDER_COLOR};
    border-radius: 10px; padding: 16px; height: 100%;
    display: flex; flex-direction: column; gap: 8px;
}}
.drill-card:hover {{ border-color: {ACCENT_CYAN}; }}
.drill-title {{
    font-size: .95rem; font-weight: 600; color: {TEXT_PRIMARY};
    font-family: 'JetBrains Mono','Consolas',monospace;
}}
.drill-desc {{ font-size: .78rem; color: {TEXT_SECONDARY}; flex: 1; }}

/* ghost-style drill-down buttons that belong to the card above */
.stColumn:has(.drill-card) [data-testid="stButton"] > button {{
    background: transparent;
    border: 1px solid {BORDER_COLOR};
    color: {TEXT_SECONDARY};
    font-weight: 500;
}}
.stColumn:has(.drill-card) [data-testid="stButton"] > button:hover {{
    border-color: {ACCENT_CYAN};
    color: {ACCENT_CYAN};
}}
</style>
"""


def _read_eval(conn, sql, args=(), default=None):
    """Run a read-only query against the evaluation db connection, swallow errors."""
    try:
        row = conn.execute(sql, args).fetchone()
        return row
    except Exception:
        return default


def _read_cache(sql, args=(), default=None):
    """Read from cache.db (market data / universe)."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        c = sqlite3.connect(os.path.join(root, "data", "cache.db")).cursor()
        row = c.execute(sql, args).fetchone()
        c.connection.close()
        return row
    except Exception:
        return default


def _gather_stage_status() -> list[dict]:
    """Collect live status for all 8 pipeline stages from the databases."""
    db = get_db()
    conn = db.connect()

    # ── Stage 1: Data ──────────────────────────────────
    # Canonical universe size from security_master (5540 clean stocks).
    # tradable_universe is a derived snapshot that can be stale/garbage.
    uni_count = (
        _read_cache(
            "SELECT COUNT(*) FROM security_master WHERE status='active' AND is_st=0", default=(0,)
        )[0]
        or 0
    )
    mkt_last = _read_cache("SELECT MAX(trade_date) FROM market_index_daily", default=(None,))[0]

    # ── Stage 2: Factors (proxy: signal_snapshot rows) ─
    sig_row = _read_eval(
        conn, "SELECT COUNT(*), MAX(created_at) FROM signal_snapshot", default=(0, None)
    )
    sig_count = (sig_row[0] if sig_row else 0) or 0
    sig_last = sig_row[1] if sig_row else None

    # ── Stage 3: Research ──────────────────────────────
    rd_row = _read_eval(
        conn,
        "SELECT COUNT(*), date('now'), MAX(created_at) FROM research_decisions",
        default=(0, None, None),
    )
    rd_total = rd_row[0] if rd_row else 0
    rd_today = (
        _read_eval(
            conn,
            "SELECT COUNT(*) FROM research_decisions WHERE date(created_at)=date('now')",
            default=(0,),
        )[0]
        or 0
    )

    # ── Stage 4: Committee ─────────────────────────────
    cd_row = _read_eval(
        conn, "SELECT COUNT(*), MAX(created_at) FROM committee_decisions", default=(0, None)
    )
    cd_total = cd_row[0] if cd_row else 0
    verdict_dist = {}
    for v, n in (
        conn.execute(
            "SELECT verdict, COUNT(*) FROM committee_decisions GROUP BY verdict"
        ).fetchall()
        if conn
        else []
    ):
        verdict_dist[v] = n
    approve_n = verdict_dist.get("APPROVE", 0) + verdict_dist.get("APPROVE_WITH_CONDITIONS", 0)

    # ── Stage 5: Portfolio ─────────────────────────────
    pd_row = _read_eval(
        conn, "SELECT COUNT(*), MAX(created_at) FROM portfolio_decisions", default=(0, None)
    )
    pd_total = pd_row[0] if pd_row else 0
    pd_last = pd_row[1] if pd_row else None

    # ── Stage 6: Execution ─────────────────────────────
    pe_row = _read_eval(
        conn, "SELECT COUNT(*), MAX(execution_date) FROM portfolio_execution", default=(0, None)
    )
    pe_total = pe_row[0] if pe_row else 0
    pe_last = pe_row[1] if pe_row else None

    # ── Stage 7: Evaluation ────────────────────────────
    er_row = _read_eval(
        conn, "SELECT COUNT(*), MAX(eval_date) FROM evaluation_results", default=(0, None)
    )
    er_total = er_row[0] if er_row else 0
    win_row = (
        _read_eval(
            conn, "SELECT COUNT(*) FROM evaluation_results WHERE alpha_vs_market > 0", default=(0,)
        )[0]
        or 0
    )
    win_rate = (win_row / er_total) if er_total else 0.0

    # ── Stage 8: Evolution ────────────────────────────
    evo_cycle = _read_eval(conn, "SELECT MAX(cycle_id) FROM evolution_events", default=(0,))[0] or 0
    evo_status, evo_detail = "—", ""
    if evo_cycle:
        cyc_events = (
            conn.execute(
                "SELECT event_type, description FROM evolution_events WHERE cycle_id=? ORDER BY id",
                (evo_cycle,),
            ).fetchall()
            if conn
            else []
        )
        types = [e[0] for e in cyc_events]
        if "CYCLE_SKIPPED" in types:
            evo_status, evo_detail = "跳过", "代理不足,本轮跳过"
        elif "WARMUP" in types:
            cold = sum(1 for e in types if e == "COLD_START")
            evo_status, evo_detail = "暖机", f"{cold} 个代理冷启动中"
        elif "AGENT_BORN" in types:
            evo_status, evo_detail = "已繁衍", "新代理已激活"
        else:
            evo_detail = "沙箱校验中"
        if "AGENT_REJECTED" in types and "AGENT_BORN" not in types:
            evo_status = (evo_status + "/拒绝") if evo_status != "—" else "沙箱拒绝"
            evo_detail = evo_detail or "子代未过沙箱"

    try:
        conn.close()
    except Exception as exc:
        logger.debug("operation failed (was silently ignored): %s", exc)

    def _state(count, threshold=0):
        if count > threshold:
            return "is-active"
        if count is not None:
            return "is-empty"
        return "is-idle"

    return [
        dict(
            num="01",
            icon="📡",
            name="数据",
            state=_state(uni_count),
            metric=f"{uni_count}",
            sub=f"行情@{mkt_last or '—'}",
            drill="data_stage",
        ),
        dict(
            num="02",
            icon="🧮",
            name="因子",
            state=_state(sig_count),
            metric=f"{sig_count}",
            sub=f"快照@{(sig_last or '')[:10] or '—'}",
            drill="factors_stage",
        ),
        dict(
            num="03",
            icon="🔬",
            name="研究",
            # 绿=今日已跑; 黄=有历史但今日未跑; 红=从未产出
            state=("is-active" if rd_today > 0 else "is-idle" if rd_total > 0 else "is-empty"),
            metric=f"{rd_total}",
            sub=f"今日 {rd_today} 条",
            drill="thesis_tracker",
        ),
        dict(
            num="04",
            icon="🏛️",
            name="委员会",
            state=_state(cd_total),
            metric=f"{cd_total}",
            sub=f"通过 {approve_n}",
            drill="committee_room",
        ),
        dict(
            num="05",
            icon="💼",
            name="组合",
            state=_state(pd_total),
            metric=f"{pd_total}",
            sub=f"最近 {(pd_last or '—')[:10]}",
            drill="portfolio_stage",
        ),
        dict(
            num="06",
            icon="⚡",
            name="执行",
            state=_state(pe_total),
            metric=f"{pe_total}",
            sub=f"成交@{(pe_last or '—')}",
            drill="execution_stage",
        ),
        dict(
            num="07",
            icon="📊",
            name="评估",
            state=_state(er_total),
            metric=f"{er_total}",
            sub=f"胜率 {win_rate:.0%}",
            drill="leaderboard",
        ),
        dict(
            num="08",
            icon="🧬",
            name="进化",
            state=_state(evo_cycle),
            metric=f"#{evo_cycle}",
            sub=f"{evo_status} {evo_detail}".strip(),
            drill="genome_fusion",
        ),
    ]


def _dot_class(state: str) -> str:
    return {"is-active": "active", "is-idle": "idle", "is-empty": "empty"}.get(state, "idle")


def render():
    st.markdown(
        """
    <div class="page-header">
        <h1>📊 管线总览</h1>
        <p>stock-sieve 端到端工作流 — 数据 → 因子 → 研究 → 委员会 → 组合 → 执行 → 评估 → 进化</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown(PIPELINE_CSS, unsafe_allow_html=True)

    stages = _gather_stage_status()

    # ── Pipeline strip (8 connected stages) ──────────────
    cards_html = []
    for i, s in enumerate(stages):
        if i > 0:
            # arrow is "done" (green) if both this and previous stage have data
            done_cls = " done" if stages[i - 1]["state"] == "is-active" else ""
            cards_html.append(f'<div class="ps-arrow{done_cls}">►</div>')
        cards_html.append(
            f'<div class="pipeline-stage {s["state"]}">'
            f'<div class="ps-head">'
            f'<span class="ps-num">STAGE {s["num"]}</span>'
            f'<span class="ps-dot {_dot_class(s["state"])}"></span>'
            f"</div>"
            f'<div class="ps-icon">{s["icon"]}</div>'
            f'<div class="ps-name">{s["name"]}</div>'
            f'<div class="ps-metric">{s["metric"]}</div>'
            f'<div class="ps-sub" title="{s["sub"]}">{s["sub"]}</div>'
            f"</div>"
        )
    st.markdown(
        f'<div class="pipeline-strip">{"".join(cards_html)}</div>',
        unsafe_allow_html=True,
    )

    # ── Legend ───────────────────────────────────────────
    st.markdown(
        f"""
    <div style="display:flex; gap:20px; font-size:.72rem; color:{TEXT_SECONDARY};
                font-family:'JetBrains Mono','Consolas',monospace; margin: 2px 0 18px 4px;">
        <span><span class="ps-dot active" style="display:inline-block;margin-right:5px;"></span>有数据/今日已运行</span>
        <span><span class="ps-dot idle" style="display:inline-block;margin-right:5px;"></span>待运行</span>
        <span><span class="ps-dot empty" style="display:inline-block;margin-right:5px;"></span>尚无产出</span>
    </div>
    """,
        unsafe_allow_html=True,
    )

    # ── Drill-down views ─────────────────────────────────
    st.markdown(
        f'<div style="font-size:.78rem; color:{TEXT_SECONDARY}; margin: 6px 0 10px 2px;'
        f"font-family:'JetBrains Mono','Consolas',monospace;\">下钻视图 · 点击进入对应模块</div>",
        unsafe_allow_html=True,
    )

    drill_map = {
        "data_stage": ("📡 数据", "股票池构成、行情数据新鲜度与市场指数"),
        "factors_stage": ("🧮 因子", "代理产生的因子快照与 quality/value 分值"),
        "thesis_tracker": ("🔍 Thesis 追踪器", "研究阶段产出 — 投资论点与证据链追踪"),
        "committee_room": ("🏛️ 委员会会议室", "委员会投票、辩论与裁决记录"),
        "portfolio_stage": ("💼 组合", "头寸构建决策与权重推导链"),
        "execution_stage": ("⚡ 执行", "模拟成交与滑点/佣金/印花税摩擦拆解"),
        "leaderboard": ("🏆 Agent 排行榜", "基于评估结果的代理绩效排名"),
        "genome_fusion": ("🧬 人格融合工作室", "进化周期、基因组交叉与变异"),
    }
    drill_stages = [s for s in stages if s["drill"]]
    # Render in rows of 4 for readability (8 stages → 2 rows)
    for i in range(0, len(drill_stages), 4):
        chunk = drill_stages[i : i + 4]
        cols = st.columns(len(chunk))
        for col, s in zip(cols, chunk, strict=False):
            title, desc = drill_map[s["drill"]]
            with col:
                st.markdown(
                    f"""
                <div class="drill-card">
                    <div class="drill-title">{title}</div>
                    <div class="drill-desc">{desc}</div>
                    <div class="ps-sub" style="margin-top:4px;">对应阶段 {s["num"]} · {s["name"]}</div>
                </div>
                """,
                    unsafe_allow_html=True,
                )
                if st.button("进入 →", key=f"drill_{s['drill']}", use_container_width=True):
                    new_label = label_for(s["drill"])
                    # Sync BOTH the route variable and the sidebar radio widget
                    # key so the radio highlights the new page after rerun.
                    # current_page is the route source-of-truth; nav_radio is
                    # the radio widget key - they must move together or the
                    # radio session_state wins and the route appears unchanged.
                    st.session_state.current_page = new_label
                    st.session_state.nav_radio = new_label
                    st.rerun()

    # ── Pipeline health note ─────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    empty_stages = [s["name"] for s in stages if s["state"] == "is-empty"]
    if empty_stages:
        st.info(
            f"当前尚无产出的阶段: {', '.join(empty_stages)}。"
            "执行 `python -m src.daily_run` 可驱动整条管线产出新数据。"
        )
    else:
        st.success("所有阶段均有数据,管线运转正常。")
