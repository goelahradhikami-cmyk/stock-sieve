"""
Stock Sieve — Financial Terminal Theme

Professional dark theme inspired by Bloomberg Terminal / quantitative trading platforms.
Centralized CSS injection + Plotly layout presets + color constants.
"""

# ── Color Palette ──────────────────────────────────────────
BG_PRIMARY   = "#0b0e14"    # Deepest background
BG_SECONDARY = "#131720"    # Card / container background
BG_TERTIARY  = "#1a1f2e"    # Elevated elements
BG_SIDEBAR   = "#0d1117"    # Sidebar background

TEXT_PRIMARY   = "#e6edf3"   # Main text
TEXT_SECONDARY = "#8b949e"   # Muted text
TEXT_ACCENT    = "#58a6ff"   # Accent / link color

GAIN           = "#00e676"   # Green for positive values
GAIN_DIM       = "#1b5e20"   # Dim green
LOSS           = "#ff5252"   # Red for negative values
LOSS_DIM       = "#b71c1c"   # Dim red
WARN           = "#ffab40"   # Orange / warning
NEUTRAL        = "#78909c"   # Gray for neutral

ACCENT_CYAN    = "#00bcd4"   # Cyan accent
ACCENT_BLUE    = "#2196f3"   # Blue accent
ACCENT_PURPLE  = "#7c4dff"   # Purple accent
ACCENT_GOLD    = "#ffd740"   # Gold / highlight

BORDER_COLOR   = "#21262d"   # Subtle borders
GRID_COLOR     = "#1c2333"   # Chart gridlines

# Agent series colors
SERIES_COLORS = [
    "#00bcd4", "#ff7043", "#66bb6a", "#ab47bc",
    "#ffa726", "#42a5f5", "#ef5350", "#26c6da",
]


# ── Streamlit CSS Injection ────────────────────────────────
TERMINAL_CSS = f"""
<style>
/* === Global overrides === */
.stApp {{
    background-color: {BG_PRIMARY};
    color: {TEXT_PRIMARY};
}}

/* === Sidebar === */
section[data-testid="stSidebar"] {{
    background-color: {BG_SIDEBAR};
    border-right: 1px solid {BORDER_COLOR};
}}
section[data-testid="stSidebar"] .stMarkdown p,
section[data-testid="stSidebar"] .stMarkdown li {{
    color: {TEXT_SECONDARY};
}}
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3 {{
    color: {TEXT_PRIMARY};
}}

/* === Navigation radio buttons → terminal-style tabs === */
/* Note: st.markdown('<div class="nav-radio">') does NOT wrap the radio —
   Streamlit puts each element in its own container, so target the sidebar
   radiogroup directly (there is only one radio in the sidebar). */
section[data-testid="stSidebar"] div[role="radiogroup"] {{
    display: flex;
    flex-direction: column;
    gap: 2px;
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"] {{
    padding: 10px 16px;
    border-radius: 6px;
    border-left: 3px solid transparent;
    transition: background 0.15s;
    cursor: pointer;
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"]:hover {{
    background-color: {BG_TERTIARY};
}}
/* hide the circle dot (first child div), keep the text */
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"] > div:first-child {{
    display: none !important;
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"] p {{
    color: {TEXT_SECONDARY};
    font-size: 0.95rem;
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"]:hover p {{
    color: {TEXT_PRIMARY};
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"]:has(input[type="radio"]:checked) {{
    background-color: {BG_TERTIARY};
    border-left: 3px solid {ACCENT_CYAN};
}}
section[data-testid="stSidebar"] div[role="radiogroup"] label[data-baseweb="radio"]:has(input[type="radio"]:checked) p {{
    color: {ACCENT_CYAN};
    font-weight: 600;
}}

/* === Metrics === */
[data-testid="stMetric"] {{
    background-color: {BG_SECONDARY};
    border: 1px solid {BORDER_COLOR};
    border-radius: 8px;
    padding: 12px 16px;
}}
[data-testid="stMetric"] label {{
    color: {TEXT_SECONDARY} !important;
    font-size: 0.8rem !important;
}}
[data-testid="stMetric"] [data-testid="stMetricValue"] {{
    color: {TEXT_PRIMARY} !important;
    font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace !important;
}}
[data-testid="stMetricDelta"] {{
    font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace !important;
}}

/* === Containers with border === */
[data-testid="stElementContainer"][style*="border"] {{
    background-color: {BG_SECONDARY} !important;
    border-color: {BORDER_COLOR} !important;
    border-radius: 8px !important;
}}

/* === DataFrames === */
[data-testid="stDataFrame"] {{
    border: 1px solid {BORDER_COLOR};
    border-radius: 8px;
    overflow: hidden;
}}

/* === Buttons === */
.stButton > button[kind="primary"] {{
    background-color: {ACCENT_CYAN} !important;
    color: {BG_PRIMARY} !important;
    border: none !important;
    font-weight: 700 !important;
    border-radius: 6px !important;
    letter-spacing: 0.5px;
}}
.stButton > button[kind="primary"]:hover {{
    background-color: #26c6da !important;
}}

/* === Expander === */
.streamlit-expanderHeader {{
    color: {TEXT_SECONDARY};
    font-size: 0.9rem;
}}
.streamlit-expanderHeader:hover {{
    color: {TEXT_PRIMARY};
}}

/* === Selectbox / Input === */
[data-baseweb="select"] > div {{
    background-color: {BG_TERTIARY} !important;
    border-color: {BORDER_COLOR} !important;
    color: {TEXT_PRIMARY} !important;
}}
.stTextInput > div > div {{
    background-color: {BG_TERTIARY} !important;
    border-color: {BORDER_COLOR} !important;
}}
.stTextInput input {{
    color: {TEXT_PRIMARY} !important;
    font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace !important;
}}

/* === Divider === */
[data-testid="stDivider"] {{
    border-color: {BORDER_COLOR} !important;
}}

/* === Captions === */
[data-testid="stCaptionContainer"] {{
    color: {TEXT_SECONDARY};
}}

/* === Status badges === */
.terminal-badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace;
    letter-spacing: 0.5px;
    text-transform: uppercase;
}}
.badge-approve {{
    background-color: rgba(0, 230, 118, 0.15);
    color: {GAIN};
    border: 1px solid rgba(0, 230, 118, 0.3);
}}
.badge-reject {{
    background-color: rgba(255, 82, 82, 0.15);
    color: {LOSS};
    border: 1px solid rgba(255, 82, 82, 0.3);
}}
.badge-conditional {{
    background-color: rgba(255, 171, 64, 0.15);
    color: {WARN};
    border: 1px solid rgba(255, 171, 64, 0.3);
}}
.badge-info {{
    background-color: rgba(0, 188, 212, 0.15);
    color: {ACCENT_CYAN};
    border: 1px solid rgba(0, 188, 212, 0.3);
}}

/* === Terminal card === */
.terminal-card {{
    background-color: {BG_SECONDARY};
    border: 1px solid {BORDER_COLOR};
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 8px;
}}
.terminal-card-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid {BORDER_COLOR};
}}
.terminal-card-title {{
    color: {TEXT_PRIMARY};
    font-size: 0.95rem;
    font-weight: 600;
    font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace;
}}

/* === Status indicator dots === */
.status-dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s infinite;
}}
.status-dot-active {{ background-color: {GAIN}; }}
.status-dot-idle {{ background-color: {WARN}; }}
@keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.5; }}
}}

/* === Mono font for data === */
.mono {{
    font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace;
}}

/* === Slider === */
[data-testid="stSlider"] .stSlider {{
    color: {ACCENT_CYAN};
}}

/* === Sidebar bottom section === */
.sidebar-footer {{
    position: fixed;
    bottom: 0;
    left: 0;
    width: 21rem;
    background-color: {BG_SIDEBAR};
    padding: 12px 24px;
    border-top: 1px solid {BORDER_COLOR};
    color: {TEXT_SECONDARY};
    font-size: 0.75rem;
}}

/* === Hide default Streamlit chrome === */
#MainMenu {{visibility: hidden;}}
footer {{visibility: hidden;}}
[data-testid="stDeployButton"],
.stDeployButton {{
    display: none !important;
}}
[data-testid="stToolbar"] {{
    display: none !important;
}}
header[data-testid="stHeader"] {{
    background-color: {BG_PRIMARY};
    border-bottom: 1px solid {BORDER_COLOR};
}}

/* === Warning / Info / Success boxes === */
[data-testid="stAlert"] {{
    border-radius: 6px;
}}

/* === Tabs === */
.stTabs [data-baseweb="tab-list"] {{
    gap: 2px;
}}
.stTabs [data-baseweb="tab"] {{
    background-color: {BG_TERTIARY};
    border-radius: 6px 6px 0 0;
    color: {TEXT_SECONDARY};
    padding: 8px 20px;
    font-size: 0.9rem;
}}
.stTabs [aria-selected="true"] {{
    background-color: {BG_SECONDARY} !important;
    color: {ACCENT_CYAN} !important;
    border-bottom: 2px solid {ACCENT_CYAN};
}}

/* === Markdown code blocks === */
code {{
    background-color: {BG_TERTIARY} !important;
    color: {ACCENT_CYAN} !important;
    padding: 2px 6px !important;
    border-radius: 4px !important;
    font-size: 0.85em !important;
}}

/* === Page header style === */
.page-header {{
    padding-bottom: 16px;
    margin-bottom: 8px;
    border-bottom: 1px solid {BORDER_COLOR};
}}
.page-header h1 {{
    margin: 0;
    font-size: 1.6rem;
    color: {TEXT_PRIMARY};
    font-weight: 700;
}}
.page-header p {{
    color: {TEXT_SECONDARY};
    font-size: 0.9rem;
    margin-top: 4px;
}}
</style>
"""


# ── Plotly Layout Presets ──────────────────────────────────
def terminal_layout(**overrides) -> dict:
    """Return a Plotly layout dict with terminal dark theme."""
    base = dict(
        paper_bgcolor=BG_PRIMARY,
        plot_bgcolor=BG_SECONDARY,
        font=dict(
            color=TEXT_PRIMARY,
            family="JetBrains Mono, SF Mono, Consolas, monospace",
            size=12,
        ),
        xaxis=dict(
            gridcolor=GRID_COLOR,
            zerolinecolor=GRID_COLOR,
            tickfont=dict(color=TEXT_SECONDARY, size=11),
            title_font=dict(color=TEXT_SECONDARY, size=12),
        ),
        yaxis=dict(
            gridcolor=GRID_COLOR,
            zerolinecolor=GRID_COLOR,
            tickfont=dict(color=TEXT_SECONDARY, size=11),
            title_font=dict(color=TEXT_SECONDARY, size=12),
        ),
        legend=dict(
            font=dict(color=TEXT_PRIMARY, size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
        margin=dict(l=10, r=10, t=10, b=10),
        colorway=SERIES_COLORS,
    )
    base.update(overrides)
    return base


def terminal_polar(**overrides) -> dict:
    """Return a Plotly layout dict for polar/radar charts."""
    base = dict(
        paper_bgcolor=BG_PRIMARY,
        font=dict(
            color=TEXT_PRIMARY,
            family="JetBrains Mono, SF Mono, Consolas, monospace",
            size=12,
        ),
        polar=dict(
            bgcolor=BG_SECONDARY,
            radialaxis=dict(
                visible=True,
                range=[0, 100],
                gridcolor=GRID_COLOR,
                linecolor=BORDER_COLOR,
                tickfont=dict(color=TEXT_SECONDARY, size=10),
            ),
            angularaxis=dict(
                gridcolor=GRID_COLOR,
                linecolor=BORDER_COLOR,
                tickfont=dict(color=TEXT_PRIMARY, size=11),
            ),
        ),
        showlegend=True,
        legend=dict(
            font=dict(color=TEXT_PRIMARY, size=11),
            bgcolor="rgba(0,0,0,0)",
            orientation="h",
            x=0.5, xanchor="center",
            y=-0.06, yanchor="top",
        ),
        margin=dict(l=40, r=40, t=20, b=20),
        colorway=SERIES_COLORS,
    )
    base.update(overrides)
    return base


# ── Helper Functions ───────────────────────────────────────
def verdict_badge(verdict: str) -> str:
    """Return HTML badge for a committee verdict."""
    mapping = {
        "APPROVE": ("badge-approve", "APPROVED"),
        "APPROVE_WITH_CONDITIONS": ("badge-conditional", "CONDITIONAL"),
        "REJECT": ("badge-reject", "REJECTED"),
        "RETURN_FOR_REVISION": ("badge-info", "REVISION"),
    }
    cls, label = mapping.get(verdict, ("badge-info", verdict))
    return f'<span class="terminal-badge {cls}">{label}</span>'


def color_value(val, positive_is_good=True, fmt="{:+.1%}"):
    """Return markdown-colored string based on sign."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return str(val)
    if positive_is_good:
        color = "green" if v > 0 else ("red" if v < 0 else "gray")
    else:
        color = "red" if v > 0 else ("green" if v < 0 else "gray")
    return f":{color}[{fmt.format(v)}]"


def mono(text: str) -> str:
    """Wrap text in mono-font span."""
    return f'<span class="mono">{text}</span>'
