"""Decision Timeline — Terminal-styled investment decision chain."""

import json

import streamlit as st

from ..utils.theme import (
    ACCENT_CYAN,
    BG_SECONDARY,
    BORDER_COLOR,
    GAIN,
    LOSS,
    TEXT_SECONDARY,
)


def decision_timeline(decisions: list[dict], stock_name: str = ""):
    """Render a vertical timeline of investment decisions for a stock."""
    if not decisions:
        st.info("暂无该股票的决策记录")
        return

    for _idx, rd in enumerate(decisions):
        with st.container(border=True):
            # ── Header row ───────────────────────────────
            cols = st.columns([3, 1, 1])
            with cols[0]:
                thesis_full = rd.get("thesis_claim", "未命名 Thesis")
                thesis = thesis_full if len(thesis_full) <= 80 else thesis_full[:80] + "…"
                st.markdown(f"**🧠 {thesis}**")
            with cols[1]:
                alpha = rd.get("alpha_score", 0)
                st.metric("Alpha", f"{alpha:.1f}/10")
            with cols[2]:
                conf = rd.get("confidence", 0)
                st.metric("Confidence", f"{conf:.1f}/10")

            # ── Decision chain pipeline ──────────────────
            st.markdown("##### 📋 决策链")

            chain_cols = st.columns(5)
            step_style = (
                f"background:{BG_SECONDARY}; border:1px solid {BORDER_COLOR};"
                f"border-radius:6px; padding:10px; font-size:0.8rem;"
            )

            # 1. Research
            with chain_cols[0]:
                st.markdown(
                    f"""
                <div style="{step_style} border-top:2px solid {ACCENT_CYAN};">
                    <div style="font-weight:700; color:{ACCENT_CYAN}; margin-bottom:4px;">
                        🧠 Research
                    </div>
                    <div class="mono" style="color:{TEXT_SECONDARY};">
                        {rd.get("agent_id", "?")}<br>
                        {rd.get("entry_date", "?")}<br>
                        ¥{rd.get("entry_price", "?")}
                    </div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

            # 2. Validator
            with chain_cols[1]:
                thesis_evidence = rd.get("thesis_evidence", "[]")
                try:
                    ev = (
                        json.loads(thesis_evidence)
                        if isinstance(thesis_evidence, str)
                        else thesis_evidence
                    )
                    ev_count: int | str = len(ev)
                except (json.JSONDecodeError, TypeError):
                    ev_count = "?"
                st.markdown(
                    f"""
                <div style="{step_style} border-top:2px solid {SERIES_COLORS[1]};">
                    <div style="font-weight:700; color:{SERIES_COLORS[1]}; margin-bottom:4px;">
                        🔍 Validator
                    </div>
                    <div class="mono" style="color:{TEXT_SECONDARY};">
                        Evidence: {ev_count}<br>
                        Verdict: —
                    </div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

            # 3. Committee
            with chain_cols[2]:
                st.markdown(
                    f"""
                <div style="{step_style} border-top:2px solid {SERIES_COLORS[3]};">
                    <div style="font-weight:700; color:{SERIES_COLORS[3]}; margin-bottom:4px;">
                        🏛️ Committee
                    </div>
                    <div class="mono" style="color:{TEXT_SECONDARY};">
                        Verdict: —<br>
                        Weighted: —
                    </div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

            # 4. Portfolio
            with chain_cols[3]:
                st.markdown(
                    f"""
                <div style="{step_style} border-top:2px solid {SERIES_COLORS[4]};">
                    <div style="font-weight:700; color:{SERIES_COLORS[4]}; margin-bottom:4px;">
                        💼 Portfolio
                    </div>
                    <div class="mono" style="color:{TEXT_SECONDARY};">
                        Weight: —<br>
                        Cap: —
                    </div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

            # 5. Evaluation
            with chain_cols[4]:
                ret = rd.get("stock_return")
                alpha_mkt = rd.get("alpha_vs_market")
                ret_text = f"{ret:+.1%}" if ret is not None else "Pending"
                alpha_text = f"{alpha_mkt:+.1%}" if alpha_mkt is not None else "—"
                ret_color = (
                    GAIN if (ret and ret > 0) else (LOSS if (ret and ret < 0) else TEXT_SECONDARY)
                )
                alpha_color = (
                    GAIN
                    if (alpha_mkt and alpha_mkt > 0)
                    else (LOSS if (alpha_mkt and alpha_mkt < 0) else TEXT_SECONDARY)
                )

                st.markdown(
                    f"""
                <div style="{step_style} border-top:2px solid {GAIN};">
                    <div style="font-weight:700; color:{GAIN}; margin-bottom:4px;">
                        📊 Eval
                    </div>
                    <div class="mono">
                        <span style="color:{ret_color};">Return: {ret_text}</span><br>
                        <span style="color:{alpha_color};">Alpha: {alpha_text}</span>
                    </div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

            # ── Factor profile ───────────────────────────
            factor_raw = rd.get("factor_snapshot", "{}")
            try:
                factors = json.loads(factor_raw) if isinstance(factor_raw, str) else factor_raw
                if factors:
                    with st.expander("📐 因子快照"):
                        factor_cols = st.columns(4)
                        items = list(factors.items())[:8]
                        for i, (k, v) in enumerate(items):
                            with factor_cols[i % 4]:
                                st.metric(k, f"{v:.2f}" if isinstance(v, float) else str(v))
            except (json.JSONDecodeError, TypeError):
                pass


# Import SERIES_COLORS for inline HTML usage
from ..utils.theme import SERIES_COLORS
