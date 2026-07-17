"""
Report Exporter — Generate Markdown and Excel reports.

Supports:
  - Committee Report (Markdown/Excel)
  - Performance Report (Excel)
  - Thesis Trace (Markdown)
  - Evolution Log (Excel)
"""

import json
import os
from datetime import date, datetime


class ReportExporter:
    """Export Stock Sieve data to Markdown or Excel reports."""

    def __init__(self, db):
        self.db = db

    # ═══════════════════════════════════════════════════════
    # Committee Report
    # ═══════════════════════════════════════════════════════

    def export_committee_report(self, start_date: str = None,
                                 end_date: str = None,
                                 format: str = "md",
                                 output_path: str = None) -> str:
        """Export committee decisions as Markdown report.

        Args:
            start_date: ISO date string (default: 30 days ago)
            end_date: ISO date string (default: today)
            format: 'md' or 'xlsx'
            output_path: file path to write (default: auto-generated)

        Returns:
            Path to generated file.
        """
        if start_date is None:
            start_date = (date.today().replace(day=1)).isoformat()
        if end_date is None:
            end_date = date.today().isoformat()

        conn = self.db.connect()
        rows = conn.execute("""
            SELECT cd.*, rd.security_id, rd.thesis_claim, rd.alpha_score,
                   rd.confidence, rd.entry_price, rd.entry_date,
                   rd.agent_id as research_agent_id
            FROM committee_decisions cd
            LEFT JOIN research_decisions rd ON cd.research_decision_id = rd.id
            WHERE cd.created_at BETWEEN ? AND ?
            ORDER BY cd.created_at DESC
        """, (start_date, end_date + " 23:59:59")).fetchall()
        conn.close()

        decisions = [dict(r) for r in rows]

        if not decisions:
            return "No committee decisions found in the specified period."

        if format == "xlsx":
            return self._export_committee_xlsx(decisions, start_date, end_date, output_path)

        return self._export_committee_md(decisions, start_date, end_date, output_path)

    def _export_committee_md(self, decisions: list[dict],
                              start_date: str, end_date: str,
                              output_path: str = None) -> str:
        """Generate Markdown committee report."""
        lines = [
            "# Stock Sieve Investment Committee Report",
            f"**Period: {start_date} to {end_date}**",
            f"**Generated: {datetime.now().isoformat()[:19]}**",
            f"**Total Sessions: {len(decisions)}**",
            "",
            "---",
            "",
        ]

        for d in decisions:
            verdict_icon = {"APPROVE": "✅", "APPROVE_WITH_CONDITIONS": "⚠️",
                            "REJECT": "❌", "RETURN_FOR_REVISION": "↩️"}.get(d.get("verdict", ""), "❓")
            lines.extend([
                f"## {verdict_icon} Session: {d.get('security_id', '?')}",
                f"- **Date**: {str(d.get('created_at', '?'))[:10]}",
                f"- **Researcher**: {d.get('research_agent_id', '?')}",
                f"- **Thesis**: {d.get('thesis_claim', 'N/A')[:120]}",
                f"- **Alpha/Confidence**: {d.get('alpha_score', '-')}/{d.get('confidence', '-')}",
                f"- **Verdict**: {d.get('verdict', '?')} (ws={d.get('weighted_score', 0):.1f})",
                f"- **Reason**: {d.get('verdict_reason', 'N/A')}",
                "",
                "| Role | Score |",
                "|------|-------|",
                f"| Valuation Reviewer | {d.get('valuation_score', '-'):.0f} |",
                f"| Industry Reviewer | {d.get('industry_score', '-'):.0f} |",
                f"| Risk Controller | {d.get('risk_score', '-'):.0f} |",
                f"| Quant Auditor | {d.get('quant_score', '-'):.0f} |",
                f"| Devil's Advocate | {d.get('devil_advocate_score', '-'):.0f} |",
                "",
            ])

            # Devil's Advocate attack points
            attack_raw = d.get("devil_advocate_attack_points_json", "[]")
            try:
                attacks = json.loads(attack_raw) if isinstance(attack_raw, str) else attack_raw
                if attacks:
                    lines.append("### ⚠️ Devil's Advocate Attack Points")
                    for ap in attacks:
                        lines.append(f"- {ap}")
                    lines.append("")
            except (json.JSONDecodeError, TypeError):
                pass

            # Monitoring flags
            flags_raw = d.get("monitoring_flags_json", "[]")
            try:
                flags = json.loads(flags_raw) if isinstance(flags_raw, str) else flags_raw
                if flags:
                    lines.append(f"### 🔍 Monitoring Flags: {', '.join(flags)}")
                    lines.append("")
            except (json.JSONDecodeError, TypeError):
                pass

            # Member statements
            stmts_raw = d.get("member_statements_json", "{}")
            try:
                stmts = json.loads(stmts_raw) if isinstance(stmts_raw, str) else stmts_raw
                if stmts:
                    lines.append("### 📝 Member Statements")
                    role_names = {
                        "valuation_reviewer": "估值审查员", "industry_reviewer": "行业审查员",
                        "risk_controller": "风控官", "quant_auditor": "量化审计员",
                        "devil_advocate": "魔鬼代言人",
                    }
                    for role_key, stmt in stmts.items():
                        if stmt:
                            lines.append(f"- **{role_names.get(role_key, role_key)}**: {stmt}")
                    lines.append("")
            except (json.JSONDecodeError, TypeError):
                pass

            lines.append("---")
            lines.append("")

        content = "\n".join(lines)

        if output_path:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            return output_path

        # Auto-generate path
        reports_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "reports"
        )
        os.makedirs(reports_dir, exist_ok=True)
        path = os.path.join(
            reports_dir,
            f"committee_report_{start_date}_to_{end_date}.md"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _export_committee_xlsx(self, decisions: list[dict],
                                start_date: str, end_date: str,
                                output_path: str = None) -> str:
        """Export committee report as Excel."""
        try:
            import pandas as pd
        except ImportError:
            return "Error: pandas required for Excel export. Install with: pip install pandas openpyxl"

        rows = []
        for d in decisions:
            rows.append({
                "Date": str(d.get("created_at", ""))[:10],
                "Stock": d.get("security_id", ""),
                "Researcher": d.get("research_agent_id", ""),
                "Thesis": (d.get("thesis_claim", "") or "")[:80],
                "Alpha": d.get("alpha_score"),
                "Confidence": d.get("confidence"),
                "Valuation": d.get("valuation_score"),
                "Industry": d.get("industry_score"),
                "Risk": d.get("risk_score"),
                "Quant": d.get("quant_score"),
                "DevilAdvocate": d.get("devil_advocate_score"),
                "Weighted": d.get("weighted_score"),
                "Verdict": d.get("verdict"),
            })

        df = pd.DataFrame(rows)

        if not output_path:
            reports_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data", "reports"
            )
            os.makedirs(reports_dir, exist_ok=True)
            output_path = os.path.join(
                reports_dir,
                f"committee_report_{start_date}_to_{end_date}.xlsx"
            )

        df.to_excel(output_path, index=False, sheet_name="Committee Decisions")
        return output_path

    # ═══════════════════════════════════════════════════════
    # Performance Report
    # ═══════════════════════════════════════════════════════

    def export_performance_report(self, period: str = "quarterly",
                                   format: str = "xlsx",
                                   output_path: str = None) -> str:
        """Export agent performance report."""
        try:
            import pandas as pd
        except ImportError:
            return "Error: pandas required. Install with: pip install pandas openpyxl"

        conn = self.db.connect()
        rows = conn.execute("""
            SELECT * FROM agent_performance
            WHERE period_type = ?
            ORDER BY personality_score DESC
        """, (period,)).fetchall()
        conn.close()

        if not rows:
            return "No performance data found."

        df = pd.DataFrame([dict(r) for r in rows])
        cols = ["agent_id", "personality_score", "total_return", "sharpe_ratio",
                "max_drawdown", "win_rate", "alpha_vs_market", "period_end"]
        df = df[[c for c in cols if c in df.columns]]

        if not output_path:
            reports_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data", "reports"
            )
            os.makedirs(reports_dir, exist_ok=True)
            output_path = os.path.join(
                reports_dir,
                f"performance_report_{date.today().isoformat()}.xlsx"
            )

        df.to_excel(output_path, index=False, sheet_name="Agent Performance")
        return output_path

    # ═══════════════════════════════════════════════════════
    # Thesis Trace
    # ═══════════════════════════════════════════════════════

    def export_thesis_trace(self, stock_code: str, format: str = "md",
                             output_path: str = None) -> str:
        """Export full decision chain for a stock as Markdown."""
        conn = self.db.connect()
        rows = conn.execute("""
            SELECT rd.*, er.stock_return, er.alpha_vs_market, er.alpha_vs_sector,
                   er.verdict as eval_verdict, er.horizon_days
            FROM research_decisions rd
            LEFT JOIN evaluation_results er ON rd.id = er.research_decision_id
            WHERE rd.security_id = ?
            ORDER BY rd.entry_date DESC
        """, (stock_code,)).fetchall()
        conn.close()

        decisions = [dict(r) for r in rows]

        if not decisions:
            return f"No decisions found for {stock_code}."

        lines = [
            f"# Thesis Trace: {stock_code}",
            f"**Generated: {datetime.now().isoformat()[:19]}**",
            f"**Total Decisions: {len(decisions)}**",
            "",
            "---",
            "",
        ]

        for i, d in enumerate(decisions):
            lines.extend([
                f"## Decision {i+1}: {d.get('entry_date', '?')}",
                f"- **Agent**: {d.get('agent_id', '?')}",
                f"- **Thesis**: {d.get('thesis_claim', 'N/A')}",
                f"- **Pattern**: {d.get('thesis_pattern', '?')}",
                f"- **Alpha**: {d.get('alpha_score', '-')}",
                f"- **Confidence**: {d.get('confidence', '-')}",
                f"- **Entry Price**: {d.get('entry_price', '-')}",
                f"- **Status**: {d.get('status', '?')}",
                "",
            ])

            ret = d.get("stock_return")
            alpha = d.get("alpha_vs_market")
            if ret is not None:
                lines.append("### T+N Evaluation")
                lines.append(f"- **Return**: {ret:+.2%}")
                lines.append(f"- **Alpha vs Market**: {alpha:+.2%}" if alpha is not None else "")
                lines.append(f"- **Horizon**: {d.get('horizon_days', '?')} days")
                lines.append("")

            lines.append("---")
            lines.append("")

        content = "\n".join(lines)

        if output_path:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            return output_path

        reports_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "reports"
        )
        os.makedirs(reports_dir, exist_ok=True)
        path = os.path.join(
            reports_dir,
            f"thesis_trace_{stock_code}_{date.today().isoformat()}.md"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    # ═══════════════════════════════════════════════════════
    # Evolution Log
    # ═══════════════════════════════════════════════════════

    def export_evolution_log(self, format: str = "xlsx",
                              output_path: str = None) -> str:
        """Export evolution history as Excel."""
        try:
            import pandas as pd
        except ImportError:
            return "Error: pandas required. Install with: pip install pandas openpyxl"

        conn = self.db.connect()

        # Genome snapshots
        genome_rows = conn.execute("""
            SELECT agent_id, strategy_genus, strategy_species, generation,
                   parent_agent_id, mutation_reason, birth_date, status
            FROM agent_genome_snapshots
            ORDER BY birth_date DESC
        """).fetchall()

        # Decision events (evolution-related)
        event_rows = conn.execute("""
            SELECT agent_id, event_type, event_summary, event_timestamp
            FROM decision_events
            WHERE event_type IN ('CHILD_AGENT_BORN', 'AGENT_FROZEN',
                  'MUTATION_PROPOSED', 'MUTATION_APPLIED')
            ORDER BY event_timestamp DESC
        """).fetchall()

        conn.close()

        df_genome = pd.DataFrame([dict(r) for r in genome_rows])
        df_events = pd.DataFrame([dict(r) for r in event_rows])

        if not output_path:
            reports_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data", "reports"
            )
            os.makedirs(reports_dir, exist_ok=True)
            output_path = os.path.join(
                reports_dir,
                f"evolution_log_{date.today().isoformat()}.xlsx"
            )

        with pd.ExcelWriter(output_path) as writer:
            if not df_genome.empty:
                df_genome.to_excel(writer, index=False, sheet_name="Genome Snapshots")
            if not df_events.empty:
                df_events.to_excel(writer, index=False, sheet_name="Evolution Events")

        return output_path
