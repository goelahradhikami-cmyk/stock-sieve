"""Navigation registry — single source of truth for sidebar page labels.

Both app.py (sidebar radio + routing) and pipeline_overview.py (drill-down
buttons) import from here so labels never drift out of sync.
"""

from collections import OrderedDict

# Ordered (key → label). First entry is the default landing page.
# Order mirrors the pipeline so the sidebar reads top-to-bottom as the workflow.
PAGES = OrderedDict(
    [
        ("overview", "📊  管线总览"),
        ("data_stage", "📡  数据"),
        ("factors_stage", "🧮  因子"),
        ("thesis_tracker", "🔍  Thesis 追踪器"),
        ("committee_room", "🏛️  委员会会议室"),
        ("portfolio_stage", "💼  组合"),
        ("execution_stage", "⚡  执行"),
        ("leaderboard", "🏆  Agent 排行榜"),
        ("genome_fusion", "🧬  人格融合工作室"),
    ]
)

LABELS = list(PAGES.values())


def label_for(key: str) -> str:
    """Return the sidebar label for a page key, falling back to overview."""
    return PAGES.get(key, PAGES["overview"])


def key_for(label: str) -> str:
    """Return the page key for a sidebar label."""
    for k, lbl in PAGES.items():
        if lbl == label:
            return k
    return "overview"
