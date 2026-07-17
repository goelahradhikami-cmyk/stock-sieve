"""
Stock Sieve — Streamlit Dashboard  (Terminal Theme)

Protocol: agent_contract_v1.1 | Genome v3.2 | Committee v1.0.1

Usage:
    streamlit run src/ui/app.py --server.port 8503
"""

import os
import sys
from datetime import datetime

import streamlit as st

# Ensure project root in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.ui.utils.theme import ACCENT_CYAN, BG_SIDEBAR, BORDER_COLOR, TERMINAL_CSS, TEXT_SECONDARY
from src.ui.views import committee_room, genome_fusion, leaderboard, thesis_tracker

# ── Page config ──────────────────────────────────────────
st.set_page_config(
    page_title="Stock Sieve · 筛股魔方",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Inject terminal theme ────────────────────────────────
st.markdown(TERMINAL_CSS, unsafe_allow_html=True)

# ── Sidebar ──────────────────────────────────────────────
with st.sidebar:
    # Brand header
    st.markdown("""
    <div style="display:flex; align-items:center; gap:12px; margin-bottom:4px;">
        <span style="font-size:2rem;">🧬</span>
        <div>
            <div style="font-size:1.3rem; font-weight:700; color:#e6edf3;
                        font-family:'JetBrains Mono','Consolas',monospace;">
                Stock Sieve
            </div>
            <div style="font-size:0.75rem; color:#8b949e; margin-top:2px;">
                AI 投资机构治理面板
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.divider()

    # ── System status ────────────────────────────────────
    try:
        from src.ui.utils.db_connector import get_db
        db = get_db()
        conn = db.connect()
        agent_count = conn.execute(
            "SELECT COUNT(*) FROM agent_genome_snapshots WHERE status='active'"
        ).fetchone()[0]
        committee_count = conn.execute(
            "SELECT COUNT(*) FROM committee_decisions"
        ).fetchone()[0]
        conn.close()
    except Exception:
        agent_count = 8
        committee_count = 8

    st.markdown(f"""
    <div style="
        background: {BG_SIDEBAR}; border: 1px solid {BORDER_COLOR};
        border-radius: 8px; padding: 12px 16px; margin-bottom: 8px;">
        <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
            <span style="color:{TEXT_SECONDARY}; font-size:0.75rem;">SYSTEM STATUS</span>
            <span style="color:#00e676; font-size:0.75rem;">
                <span class="status-dot status-dot-active"></span>ONLINE
            </span>
        </div>
        <div style="display:flex; justify-content:space-between;">
            <div style="text-align:center;">
                <div style="font-size:1.4rem; font-weight:700; color:#e6edf3;
                            font-family:'JetBrains Mono',monospace;">{agent_count}</div>
                <div style="font-size:0.7rem; color:{TEXT_SECONDARY};">Active Agents</div>
            </div>
            <div style="text-align:center;">
                <div style="font-size:1.4rem; font-weight:700; color:#e6edf3;
                            font-family:'JetBrains Mono',monospace;">{committee_count}</div>
                <div style="font-size:0.7rem; color:{TEXT_SECONDARY};">Meetings</div>
            </div>
            <div style="text-align:center;">
                <div style="font-size:1.4rem; font-weight:700; color:{ACCENT_CYAN};
                            font-family:'JetBrains Mono',monospace;">震荡</div>
                <div style="font-size:0.7rem; color:{TEXT_SECONDARY};">Market</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Navigation ───────────────────────────────────────
    st.markdown('<div class="nav-radio">', unsafe_allow_html=True)
    page = st.radio(
        "导航",
        [
            "🏛️  委员会会议室",
            "🏆  Agent 排行榜",
            "🔍  Thesis 追踪器",
            "🧬  人格融合工作室",
        ],
        label_visibility="collapsed",
    )
    st.markdown('</div>', unsafe_allow_html=True)

    st.divider()

    # ── Footer ───────────────────────────────────────────
    st.markdown(f"""
    <div style="font-size:0.7rem; color:{TEXT_SECONDARY};
                font-family:'JetBrains Mono',monospace; padding: 4px 0;">
        <div>Protocol v1.1 &middot; Genome v3.2 &middot; Committee v1.0.1</div>
        <div>Stock Sieve v0.1.0 &middot; {datetime.now().strftime('%Y-%m-%d')}</div>
    </div>
    """, unsafe_allow_html=True)

# ── Main content routing ─────────────────────────────────
if "委员会" in page:
    committee_room.render()
elif "排行榜" in page:
    leaderboard.render()
elif "追踪" in page:
    thesis_tracker.render()
elif "融合" in page:
    genome_fusion.render()
