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

from src.ui.nav import LABELS, PAGES, key_for
from src.ui.utils.theme import ACCENT_CYAN, BG_SIDEBAR, BORDER_COLOR, TERMINAL_CSS, TEXT_SECONDARY
from src.ui.views import (
    committee_room,
    data_stage,
    execution_stage,
    factors_stage,
    genome_fusion,
    leaderboard,
    pipeline_overview,
    portfolio_stage,
    thesis_tracker,
)

# ── Page config ──────────────────────────────────────────
st.set_page_config(
    page_title="Stock Sieve · 筛股魔方",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Inject terminal theme ────────────────────────────────
st.markdown(TERMINAL_CSS, unsafe_allow_html=True)

# ── Navigation state ─────────────────────────────────────
# `current_page` is the single source of truth for routing. It is NOT bound
# as a widget key (that triggers StreamlitAPIException when drill-down
# buttons try to reassign it). Instead the radio below reads/writes a
# separate `nav_radio` key and syncs back here via an on_change callback.
if "current_page" not in st.session_state:
    st.session_state.current_page = PAGES["overview"]


def _sync_nav_radio():
    """Callback: copy the radio's selected label into current_page."""
    st.session_state.current_page = st.session_state.nav_radio


# Index of the current page within LABELS, so the radio highlights it.
try:
    _nav_index = LABELS.index(st.session_state.current_page)
except ValueError:
    _nav_index = 0

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

    # ── System status (real values, no hardcoded fallbacks) ──
    agent_count, committee_count, market_regime = "—", "—", "—"
    status_ok = False
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
        row = conn.execute(
            "SELECT market_regime FROM signal_snapshot ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        regime_map = {"bull": "牛市", "bear": "熊市", "rotation": "轮动",
                      "volatile": "震荡", "crisis": "危机"}
        raw_regime = (row[0] or "") if row else ""
        market_regime = regime_map.get(raw_regime.lower(), raw_regime or "—")
        status_ok = True
    except Exception:
        pass

    _status_label = "ONLINE" if status_ok else "DEGRADED"
    _status_color = "#00e676" if status_ok else "#ffab40"
    _dot_class = "status-dot-active" if status_ok else "status-dot-idle"
    _regime_color = {"牛市": "#00e676", "熊市": "#ff5252"}.get(market_regime, "#ffab40")

    st.markdown(f"""
    <div style="
        background: {BG_SIDEBAR}; border: 1px solid {BORDER_COLOR};
        border-radius: 8px; padding: 12px 16px; margin-bottom: 8px;">
        <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
            <span style="color:{TEXT_SECONDARY}; font-size:0.75rem;">SYSTEM STATUS</span>
            <span style="color:{_status_color}; font-size:0.75rem;">
                <span class="status-dot {_dot_class}"></span>{_status_label}
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
                <div style="font-size:1.4rem; font-weight:700; color:{_regime_color};
                            font-family:'JetBrains Mono',monospace;">{market_regime}</div>
                <div style="font-size:0.7rem; color:{TEXT_SECONDARY};">Market</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Navigation ───────────────────────────────────────
    st.markdown('<div class="nav-radio">', unsafe_allow_html=True)
    st.radio(
        "导航",
        LABELS,
        index=_nav_index,
        key="nav_radio",
        on_change=_sync_nav_radio,
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
_route = {
    "overview":        pipeline_overview.render,
    "data_stage":      data_stage.render,
    "factors_stage":   factors_stage.render,
    "committee_room":  committee_room.render,
    "leaderboard":     leaderboard.render,
    "thesis_tracker":  thesis_tracker.render,
    "portfolio_stage": portfolio_stage.render,
    "execution_stage": execution_stage.render,
    "genome_fusion":   genome_fusion.render,
}
_route.get(key_for(st.session_state.current_page), pipeline_overview.render)()
