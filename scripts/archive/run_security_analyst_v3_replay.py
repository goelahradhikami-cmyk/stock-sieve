"""
Security Analyst v3 Replay - Commit 6-S.13.5.

Re-evaluates historical BUY episodes using the v3 CandidateGenerator
(three-stage funnel) and compares against v1 (doctrine) and v2 (three-layer
ranking). The v3 generator produces a NEW candidate pool - it does not
change historical BUY/BLOCK decisions (those remain Market Guardian's
frozen responsibility).

Validation targets (6-S.13.1 Design Freeze):
  v3.1: FRM improving rate > 85% (baseline v2: 67%)
  v3.2: residual_alpha > -2%   (baseline v2: -5.01%)
  v3.3: residual_alpha > 0     (true selection alpha)

Comparison:
  v1: doctrine consensus selected stocks (historical, from shadow_candidates)
  v2: three-layer ranking top-5 (from v2 replay)
  v3: CandidateGenerator top-5 (NEW funnel: Recovery -> RS -> Mispricing)

Usage:
    python scripts/run_security_analyst_v3_replay.py
    python scripts/run_security_analyst_v3_replay.py --episode E20240829
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.thesis.candidate_generator import CandidateGenerator
from src.utils.logger import get_logger

logger = get_logger(__name__)

SHADOW_DB = "data/shadow_trading.db"
REPORT_DIR = "data/reports"
V3_TOP_N = 5  # diversification minimum


class SecurityAnalystV3Replay:
    """6-S.13.5: Replay BUY episodes with v3 CandidateGenerator."""

    def __init__(self):
        self.conn = sqlite3.connect(SHADOW_DB)
        self.conn.row_factory = sqlite3.Row
        self.generator = CandidateGenerator()

    def run(self, episode_filter: str | None = None) -> dict:
        print("=" * 60, flush=True)
        print("Security Analyst v3 Replay (6-S.13.5)", flush=True)
        print("=" * 60, flush=True)

        # BUY episodes
        if episode_filter:
            episodes = self.conn.execute(
                "SELECT episode_id, trade_date, market_state "
                "FROM shadow_episode WHERE episode_id = ? "
                "AND decision='BUY' AND status='evaluated'",
                (episode_filter,),
            ).fetchall()
        else:
            episodes = self.conn.execute(
                "SELECT episode_id, trade_date, market_state "
                "FROM shadow_episode "
                "WHERE decision='BUY' AND status='evaluated' "
                "ORDER BY trade_date"
            ).fetchall()
        print(f"BUY episodes to replay: {len(episodes)}", flush=True)

        results = []
        for i, ep in enumerate(episodes):
            r = self._replay_episode(ep)
            if r:
                results.append(r)
            if (i + 1) % 20 == 0:
                print(f"  ... {i + 1}/{len(episodes)} processed", flush=True)

        print(f"Episodes with v3 candidates: {len(results)}", flush=True)
        return self._analyze(results)

    def _replay_episode(self, ep: sqlite3.Row) -> dict | None:
        """Run v3 CandidateGenerator for one episode."""
        try:
            candidates = self.generator.generate(
                ep["trade_date"],
                ep["market_state"],
                top_n=50,
                episode_id=ep["episode_id"],
            )
        except Exception as e:
            logger.warning("v3 replay %s failed: %s", ep["episode_id"], e)
            return None

        if not candidates:
            return None

        # Look up residual_alpha for v3 candidates from shadow_candidates
        # (v3 candidates that were ALSO in the original shadow_candidates)
        v3_codes = [c.code for c in candidates[:V3_TOP_N]]
        v3_data = self._lookup_candidate_data(ep["episode_id"], v3_codes)

        # v1 selected (historical)
        v1_rows = self.conn.execute(
            "SELECT stock_code, residual_alpha, market_beta, stock_return_t20, "
            "earnings_revision_direction "
            "FROM shadow_candidates "
            "WHERE episode_id=? AND selected=1",
            (ep["episode_id"],),
        ).fetchall()

        return {
            "episode_id": ep["episode_id"],
            "trade_date": ep["trade_date"],
            "market_state": ep["market_state"],
            "v3_candidate_count": len(candidates),
            "v3_top": v3_data,
            "v1_selected": [dict(r) for r in v1_rows],
            "v3_frm_directions": [
                c.v3_features.frm_direction for c in candidates[:V3_TOP_N] if c.v3_features
            ],
        }

    def _lookup_candidate_data(self, episode_id: str, codes: list[str]) -> list[dict]:
        """Look up residual_alpha etc. for v3 candidates.

        v3 candidates may or may not be in shadow_candidates (they're
        newly generated). For those that ARE, we have attribution data.
        For those that AREN'T, residual_alpha is NULL (not in original pool).
        """
        if not codes:
            return []
        placeholders = ",".join("?" * len(codes))
        rows = self.conn.execute(
            f"SELECT stock_code, residual_alpha, market_beta, "
            f"stock_return_t20, earnings_revision_direction, frm_score "
            f"FROM shadow_candidates "
            f"WHERE episode_id=? AND stock_code IN ({placeholders})",
            [episode_id] + codes,
        ).fetchall()
        found = {r["stock_code"]: dict(r) for r in rows}
        # Return in v3 candidate order, with None for missing
        result = []
        for code in codes:
            if code in found:
                result.append(found[code])
            else:
                result.append(
                    {
                        "stock_code": code,
                        "residual_alpha": None,
                        "market_beta": None,
                        "stock_return_t20": None,
                        "earnings_revision_direction": None,
                        "frm_score": None,
                        "_in_original_pool": False,
                    }
                )
        return result

    def _analyze(self, results: list[dict]) -> dict:
        print("\n" + "=" * 60, flush=True)
        print("V1 vs V3 Comparison", flush=True)
        print("=" * 60, flush=True)

        # FRM direction comparison (v3.1 target)
        v1_dirs = defaultdict(int)
        v3_dirs = defaultdict(int)
        for r in results:
            for s in r["v1_selected"]:
                d = s.get("earnings_revision_direction") or "unknown"
                v1_dirs[d] += 1
            for d in r["v3_frm_directions"]:
                v3_dirs[d or "unknown"] += 1

        v1_total = sum(v1_dirs.values())
        v3_total = sum(v3_dirs.values())
        v1_improving_rate = v1_dirs.get("improving", 0) / max(1, v1_total)
        v3_improving_rate = v3_dirs.get("improving", 0) / max(1, v3_total)

        print("\n--- FRM Direction (v3.1 target: improving > 85%) ---", flush=True)
        print(f"  V1 selected: {dict(v1_dirs)}", flush=True)
        print(f"  V3 top-5:    {dict(v3_dirs)}", flush=True)
        print(f"  V1 improving rate: {v1_improving_rate:.1%}", flush=True)
        print(f"  V3 improving rate: {v3_improving_rate:.1%}", flush=True)
        v3_1_pass = v3_improving_rate > 0.85
        print(f"  v3.1 GATE: {'PASS ✅' if v3_1_pass else 'FAIL ❌'} (target >85%)", flush=True)

        # Residual alpha comparison (v3.2 target)
        v1_res = []
        v3_res = []
        v3_in_pool = 0
        v3_not_in_pool = 0
        for r in results:
            for s in r["v1_selected"]:
                if s.get("residual_alpha") is not None:
                    v1_res.append(s["residual_alpha"])
            for s in r["v3_top"]:
                if s.get("residual_alpha") is not None:
                    v3_res.append(s["residual_alpha"])
                    if s.get("_in_original_pool", True) is False:
                        v3_not_in_pool += 1
                    else:
                        v3_in_pool += 1
                else:
                    v3_not_in_pool += 1

        print("\n--- Residual Alpha (v3.2 target: > -2%) ---", flush=True)

        def _stats(arr, label):
            if not arr:
                print(f"  {label}: N=0 (no attribution data)", flush=True)
                return None
            a = np.array(arr)
            print(
                f"  {label}: N={len(a)} mean={np.mean(a):+.4f} "
                f"median={np.median(a):+.4f} >0: {np.mean(a > 0):.1%}",
                flush=True,
            )
            return {
                "n": len(a),
                "mean": float(np.mean(a)),
                "median": float(np.median(a)),
                "positive_rate": float(np.mean(a > 0)),
            }

        v1_stats = _stats(v1_res, "V1 selected")
        v3_stats = _stats(v3_res, "V3 top-5 (in original pool)")
        print(f"  V3 candidates in original pool: {v3_in_pool}", flush=True)
        print(
            f"  V3 candidates NOT in original pool: {v3_not_in_pool} (no attribution data)",
            flush=True,
        )

        v3_2_pass = v3_stats and v3_stats["mean"] > -0.02
        if v3_stats:
            print(f"  v3.2 GATE: {'PASS ✅' if v3_2_pass else 'FAIL ❌'} (target >-2%)", flush=True)

        # Funnel log summary
        print("\n--- Funnel Log Summary ---", flush=True)
        funnel_stats = self._funnel_log_summary()
        for line in funnel_stats["lines"]:
            print(f"  {line}", flush=True)

        # Verdict
        verdict = self._compute_verdict(
            v3_1_pass, v3_2_pass, v1_improving_rate, v3_improving_rate, v1_stats, v3_stats
        )

        # Export report
        report_path = self._export_report(
            v1_dirs,
            v3_dirs,
            v1_improving_rate,
            v3_improving_rate,
            v1_stats,
            v3_stats,
            v3_in_pool,
            v3_not_in_pool,
            funnel_stats,
            verdict,
        )
        print(f"\n=== Report: {report_path} ===", flush=True)

        return {"verdict": verdict, "report_path": report_path}

    def _funnel_log_summary(self) -> dict:
        """Summarize shadow_funnel_log across all replayed episodes."""
        lines = []
        total = self.conn.execute("SELECT COUNT(*) FROM shadow_funnel_log").fetchone()[0]
        lines.append(f"Total funnel entries: {total}")

        if total == 0:
            lines.append("(no funnel log data)")
            return {"lines": lines}

        for r in self.conn.execute(
            "SELECT rejection_stage, rejection_reason, COUNT(*) c "
            "FROM shadow_funnel_log "
            "WHERE rejection_stage IS NOT NULL "
            "GROUP BY rejection_stage, rejection_reason "
            "ORDER BY c DESC"
        ).fetchall():
            lines.append(f"  {r['rejection_stage']}/{r['rejection_reason']}: {r['c']}")

        passed = self.conn.execute(
            "SELECT COUNT(*) FROM shadow_funnel_log WHERE final_pass=1"
        ).fetchone()[0]
        lines.append(f"  final_pass (all 3 stages): {passed}")
        return {"lines": lines}

    def _compute_verdict(
        self, v3_1_pass, v3_2_pass, v1_improving, v3_improving, v1_stats, v3_stats
    ):
        gates = {
            "v3_1_frm_improving": {
                "target": ">85%",
                "v1": f"{v1_improving:.1%}",
                "v3": f"{v3_improving:.1%}",
                "passed": v3_1_pass,
            },
        }
        if v3_stats:
            gates["v3_2_residual_alpha"] = {
                "target": ">-2%",
                "v1": f"{v1_stats['mean']:+.4f}" if v1_stats else "n/a",
                "v3": f"{v3_stats['mean']:+.4f}",
                "passed": v3_2_pass,
            }

        all_pass = all(g["passed"] for g in gates.values())
        if v3_1_pass and v3_2_pass:
            verdict = "V3.1 + V3.2 PASS - proceed to v3.3 (residual>0)"
        elif v3_1_pass:
            verdict = "V3.1 PASS (FRM direction corrected) - v3.2 not yet"
        else:
            verdict = "V3.1 NOT MET - Stage 1 gate needs adjustment"
        return {"gates": gates, "all_pass": all_pass, "verdict": verdict}

    def _export_report(
        self,
        v1_dirs,
        v3_dirs,
        v1_improving,
        v3_improving,
        v1_stats,
        v3_stats,
        v3_in_pool,
        v3_not_in_pool,
        funnel_stats,
        verdict,
    ) -> str:
        today = date.today().isoformat()
        path = os.path.join(REPORT_DIR, f"security_analyst_v3_replay_{today}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        lines = []
        lines.append("# Security Analyst v3 Replay Report")
        lines.append("# Commit 6-S.13.5")
        lines.append(f"# Date: {today}")
        lines.append(f"# Verdict: {verdict['verdict']}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## v3.1: FRM Direction (target: improving > 85%)")
        lines.append("")
        lines.append(f"V1 selected: {dict(v1_dirs)}")
        lines.append(f"V3 top-5:    {dict(v3_dirs)}")
        lines.append("")
        lines.append("| Metric | V1 | V3 |")
        lines.append("|--------|----|----|")
        lines.append(f"| improving rate | {v1_improving:.1%} | {v3_improving:.1%} |")
        lines.append("")
        g = verdict["gates"].get("v3_1_frm_improving", {})
        mark = "✅" if g.get("passed") else "❌"
        lines.append(f"**{mark} v3.1 GATE: {g.get('passed')}** (target {g.get('target')})")
        lines.append("")
        lines.append("## v3.2: Residual Alpha (target: > -2%)")
        lines.append("")
        lines.append("| Metric | V1 selected | V3 top-5 |")
        lines.append("|--------|-------------|----------|")
        if v1_stats and v3_stats:
            lines.append(f"| N | {v1_stats['n']} | {v3_stats['n']} |")
            lines.append(f"| mean | {v1_stats['mean']:+.4f} | {v3_stats['mean']:+.4f} |")
            lines.append(f"| median | {v1_stats['median']:+.4f} | {v3_stats['median']:+.4f} |")
            lines.append(
                f"| positive rate | {v1_stats['positive_rate']:.1%} | "
                f"{v3_stats['positive_rate']:.1%} |"
            )
        lines.append("")
        lines.append(f"V3 candidates in original pool (have attribution): {v3_in_pool}")
        lines.append(f"V3 candidates NOT in original pool (no attribution): {v3_not_in_pool}")
        lines.append("")
        g2 = verdict["gates"].get("v3_2_residual_alpha", {})
        if g2:
            mark2 = "✅" if g2.get("passed") else "❌"
            lines.append(f"**{mark2} v3.2 GATE: {g2.get('passed')}** (target {g2.get('target')})")
        lines.append("")
        lines.append("## Funnel Log Summary")
        lines.append("")
        lines.append("```")
        for line in funnel_stats["lines"]:
            lines.append(line)
        lines.append("```")
        lines.append("")
        lines.append("## Interpretation")
        lines.append("")
        if v3_improving > v1_improving:
            lines.append(
                f"V3 corrected FRM direction: {v1_improving:.1%} -> "
                f"{v3_improving:.1%} improving. The Stage 1 hard gate "
                f"(reject deteriorating) successfully filters out "
                f"stocks with declining earnings."
            )
        if v3_stats and v1_stats:
            delta = v3_stats["mean"] - v1_stats["mean"]
            lines.append(
                f"Residual alpha: V1 {v1_stats['mean']:+.4f} -> "
                f"V3 {v3_stats['mean']:+.4f} (delta {delta:+.4f})."
            )
            if v3_stats["mean"] > 0:
                lines.append("V3 produces POSITIVE true selection alpha!")
            elif v3_stats["mean"] > v1_stats["mean"]:
                lines.append("V3 improves but residual_alpha still negative.")
            else:
                lines.append("V3 does NOT improve residual_alpha.")
        lines.append("")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Security Analyst v3 Replay (6-S.13.5)")
    parser.add_argument("--episode", type=str, default=None)
    args = parser.parse_args()

    replay = SecurityAnalystV3Replay()
    try:
        replay.run(episode_filter=args.episode)
    finally:
        replay.conn.close()


if __name__ == "__main__":
    main()
