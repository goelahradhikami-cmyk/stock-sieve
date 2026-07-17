"""Agent Radar Chart — Terminal-styled 8-dimension identity visualization."""
import plotly.graph_objects as go
import streamlit as st

from ..utils.theme import SERIES_COLORS, terminal_polar


def _hex_to_rgba(hex_color: str, alpha: float = 0.1) -> str:
    """Convert #RRGGBB / #RGB to rgba(r,g,b,alpha) for Plotly 6.x."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        r, g, b = [int(c * 2, 16) for c in hex_color]
    else:
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def agent_radar(identity_vectors: dict, selected_agents: list[str] = None):
    """Render radar chart for selected agents' investment identity.

    Args:
        identity_vectors: {agent_id: {dim: value, ...}}
        selected_agents: list of agent_ids to display (max 2 for overlay)
    """
    dims = ["valuation", "quality", "growth", "momentum",
            "macro", "contrarian", "patience", "concentration"]
    labels_cn = ["估值", "质量", "成长", "动量", "宏观", "逆向", "耐心", "集中"]

    if not selected_agents:
        selected_agents = list(identity_vectors.keys())[:1]

    fig = go.Figure()

    for idx, agent_id in enumerate(selected_agents[:2]):
        vector = identity_vectors.get(agent_id, {})
        values = [vector.get(d, 50) for d in dims]
        values.append(values[0])  # close the loop

        color = SERIES_COLORS[idx]
        fig.add_trace(go.Scatterpolar(
            r=values,
            theta=labels_cn + [labels_cn[0]],
            name=agent_id.replace("_v1", ""),
            fill="toself",
            fillcolor=_hex_to_rgba(color, 0.1),
            opacity=0.6,
            line=dict(color=color, width=2),
            marker=dict(size=6, color=color, line=dict(color="#fff", width=1)),
        ))

    fig.update_layout(
        **terminal_polar(),
        height=380,
    )

    st.plotly_chart(fig, use_container_width=True)
