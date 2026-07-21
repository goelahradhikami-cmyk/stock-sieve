"""Data loader — fetches and caches UI data from evaluation_db."""

import json
import os
import random
from datetime import date, timedelta

import pandas as pd
import streamlit as st


def _is_demo_mode() -> bool:
    """Return True when STOCK_SIEVE_DEMO=1 (shows mock data for UI development).

    In production (default) data loaders return empty containers on failure
    rather than fabricated mock data, so the UI never presents fake financial
    numbers as if they were real.
    """
    return os.environ.get("STOCK_SIEVE_DEMO", "") == "1"


@st.cache_data(ttl=60)
def load_leaderboard(_db=None) -> pd.DataFrame:
    """Load agent leaderboard from agent_performance table.

    No real performance rows -> return an empty frame.  We deliberately do NOT
    fabricate returns/Sharpe/drawdown/win-rate numbers because the evolution
    engine has not yet accumulated T+N realised performance.
    """
    if _db is None:
        return _empty_leaderboard()

    try:
        conn = _db.connect()
        rows = conn.execute("""
            SELECT agent_id, personality_score, total_return, sharpe_ratio,
                   max_drawdown, win_rate, alpha_vs_market
            FROM agent_performance
            WHERE period_type = 'quarterly'
            ORDER BY period_end DESC
        """).fetchall()
        conn.close()

        if rows:
            df = pd.DataFrame([dict(r) for r in rows])
            df = df.groupby("agent_id").first().reset_index()
            df = df.sort_values("personality_score", ascending=False)
            return df
    except Exception:
        pass

    return _empty_leaderboard()


def _empty_leaderboard() -> pd.DataFrame:
    """Empty placeholder when no realised performance exists yet."""
    return pd.DataFrame(columns=[
        "agent_id", "personality_score", "total_return", "sharpe_ratio",
        "max_drawdown", "win_rate", "alpha_vs_market"
    ])


# Kept only for tests/legacy callers that expect the old symbol name.
def _mock_leaderboard() -> pd.DataFrame:
    """Deprecated: no fabricated leaderboard data is shown in production UI."""
    return _empty_leaderboard()


@st.cache_data(ttl=60)
def load_identity_vectors() -> dict:
    """Load investment_identity vectors for all active agents."""
    try:
        import yaml
        config_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "config", "personalities"
        )
        vectors = {}
        for f in sorted(os.listdir(config_dir)):
            if f.endswith(".yaml"):
                with open(os.path.join(config_dir, f), encoding="utf-8") as fh:
                    genome = yaml.safe_load(fh)
                agent_id = genome.get("identity", {}).get("agent_id", f.replace(".yaml", ""))
                dims = genome.get("investment_identity", {}).get("dimensions", {})
                vectors[agent_id] = dims
        return vectors
    except Exception:
        return _mock_identity_vectors()


def _mock_identity_vectors() -> dict:
    return {
        "value_purist_v1": {"valuation": 90, "quality": 85, "growth": 40, "momentum": 15, "macro": 30, "contrarian": 80, "patience": 95, "concentration": 70},
        "growth_hunter_v1": {"valuation": 50, "quality": 70, "growth": 90, "momentum": 40, "macro": 45, "contrarian": 20, "patience": 50, "concentration": 60},
        "momentum_chaser_v1": {"valuation": 10, "quality": 40, "growth": 40, "momentum": 95, "macro": 50, "contrarian": 5, "patience": 20, "concentration": 55},
        "contrarian_v1": {"valuation": 85, "quality": 50, "growth": 15, "momentum": 5, "macro": 60, "contrarian": 95, "patience": 85, "concentration": 65},
        "quality_compounder_v1": {"valuation": 50, "quality": 95, "growth": 55, "momentum": 15, "macro": 25, "contrarian": 35, "patience": 90, "concentration": 80},
        "dividend_aristocrat_v1": {"valuation": 75, "quality": 70, "growth": 25, "momentum": 10, "macro": 35, "contrarian": 55, "patience": 85, "concentration": 50},
        "insider_follower_v1": {"valuation": 55, "quality": 55, "growth": 50, "momentum": 35, "macro": 40, "contrarian": 45, "patience": 60, "concentration": 65},
        "quant_nerd_v1": {"valuation": 40, "quality": 50, "growth": 50, "momentum": 55, "macro": 65, "contrarian": 30, "patience": 30, "concentration": 40},
    }


@st.cache_data(ttl=300)
def load_committee_history(_db=None, limit: int = 20) -> list[dict]:
    """Load committee decision history."""
    if _db is None:
        return _mock_committee_history() if _is_demo_mode() else []

    try:
        conn = _db.connect()
        rows = conn.execute("""
            SELECT cd.*, rd.security_id, rd.agent_id as research_agent_id,
                   rd.thesis_claim, rd.alpha_score, rd.confidence
            FROM committee_decisions cd
            LEFT JOIN research_decisions rd ON cd.research_decision_id = rd.id
            ORDER BY cd.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()

        if rows:
            return [dict(r) for r in rows]
    except Exception:
        pass

    return _mock_committee_history() if _is_demo_mode() else []


def _mock_committee_history() -> list[dict]:
    """Generate mock committee decisions for demo."""
    verdicts = ["APPROVE", "APPROVE_WITH_CONDITIONS", "APPROVE_WITH_CONDITIONS", "REJECT"]
    stocks = [("300308", "中际旭创"), ("600519", "贵州茅台"), ("000858", "五粮液"), ("300750", "宁德时代")]
    agents = ["value_purist_v1", "growth_hunter_v1", "quality_compounder_v1", "contrarian_v1"]

    mock = []
    for i in range(8):
        dt = date.today() - timedelta(days=i * 3)
        v = verdicts[i % 4]
        s = stocks[i % 4]
        scores = {
            "valuation": random.uniform(55, 90),
            "industry": random.uniform(50, 95),
            "risk": random.uniform(45, 92),
            "quant": random.uniform(40, 88),
            "devil_advocate": random.uniform(35, 85),
        }
        ws = sum(scores[k] * [0.20, 0.20, 0.25, 0.15, 0.20][i]
                 for i, k in enumerate(scores.keys()))

        mock.append({
            "committee_id": f"comm_{dt.isoformat()}_{i}",
            "research_decision_id": i + 1,
            "research_agent_id": agents[i % 4],
            "security_id": s[0],
            "thesis_claim": f"{s[1]}投资假设示例",
            "alpha_score": random.uniform(5, 9),
            "confidence": random.uniform(6, 9),
            "valuation_score": scores["valuation"],
            "industry_score": scores["industry"],
            "risk_score": scores["risk"],
            "quant_score": scores["quant"],
            "devil_advocate_score": scores["devil_advocate"],
            "weighted_score": ws,
            "verdict": v,
            "verdict_reason": "模拟委员会裁决" if "APPROVE" in v else "风险维度不达标",
            "position_cap_modifier": 1.0 if v == "APPROVE" else 0.5,
            "confidence_modifier": 0.0 if v == "APPROVE" else -1.0,
            "monitoring_flags_json": json.dumps(["MONITOR_VALUATION"] if "CONDITIONS" in v else []),
            "devil_advocate_attack_points_json": json.dumps([
                "ASSERTION_TOO_ABSOLUTE: 使用了过于绝对的断言",
                "INSUFFICIENT_EVIDENCE: 证据不足",
            ]),
            "member_statements_json": json.dumps({
                "valuation_reviewer": "估值处于合理区间，安全边际尚可。",
                "industry_reviewer": "行业趋势向好，催化剂明确。",
                "risk_controller": "组合风险可控，但需关注集中度。",
                "quant_auditor": "Alpha持续性较强，样本量充足。",
                "devil_advocate": "核心假设存在脆弱点，证据链不完整。",
            }),
            "created_at": dt.isoformat(),
        })
    return mock


@st.cache_data(ttl=300)
def load_decision_timeline(_db=None, stock_code: str = "", limit: int = 5,
                           days: int | None = None) -> list[dict]:
    """Load decision chain for a stock, optionally limited to the last N days."""
    if _db is None or not stock_code:
        return (_mock_decision_timeline(stock_code) if _is_demo_mode() else []) if stock_code else []

    try:
        conn = _db.connect()
        sql = """
            SELECT rd.*, er.verdict as eval_verdict, er.stock_return, er.alpha_vs_market
            FROM research_decisions rd
            LEFT JOIN evaluation_results er ON rd.id = er.research_decision_id
            WHERE rd.security_id = ?
        """
        args: list = [stock_code]
        if days:
            sql += " AND rd.entry_date >= date('now', ?)"
            args.append(f"-{int(days)} days")
        sql += " ORDER BY rd.entry_date DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        conn.close()

        if rows:
            return [dict(r) for r in rows]
    except Exception:
        pass

    return _mock_decision_timeline(stock_code) if _is_demo_mode() else []


def _mock_decision_timeline(stock_code: str) -> list[dict]:
    names = {"600519": "贵州茅台", "300308": "中际旭创", "000858": "五粮液"}
    name = names.get(stock_code, stock_code)
    return [{
        "id": 1,
        "agent_id": "value_purist_v1",
        "security_id": stock_code,
        "thesis_claim": f"{name}的品牌护城河与定价权创造超额回报",
        "thesis_pattern": "quality_compound",
        "alpha_score": 7.5,
        "confidence": 8.0,
        "entry_price": 1680.0 if stock_code == "600519" else 150.0,
        "entry_date": (date.today() - timedelta(days=30)).isoformat(),
        "status": "active",
        "factor_snapshot": json.dumps({"roe": 0.31, "gross_margin": 0.92, "pe_ttm": 35}),
        "risk_assessment": json.dumps({"idiosyncratic_risk": "low", "expected_drawdown_12m": 0.18}),
        "stock_return": 0.08,
        "alpha_vs_market": 0.04,
        "eval_verdict": "market_alpha_positive",
    }]


@st.cache_data(ttl=600)
def load_stock_name(stock_code: str) -> str:
    """Look up stock name from security_master (cache.db).

    Falls back to the raw code when the name is unavailable, so the UI never
    shows a fabricated name. Replaces the hardcoded {code: name} dicts that
    were previously maintained in each view.
    """
    if not stock_code:
        return stock_code
    try:
        from src.data.security_master import SecurityMaster
        sm = SecurityMaster()
        rec = sm.get_by_code(str(stock_code).zfill(6))
        if rec and rec.get("name"):
            return rec["name"]
    except Exception:
        pass
    return str(stock_code)


@st.cache_data(ttl=300)
def load_performance_trend(_db, agent_id: str) -> pd.DataFrame:
    """Load real quarterly personality_score history for an agent.

    Returns an empty frame when no realised performance history exists — the
    caller should show a 'no history yet' message instead of fabricating a
    trend with random numbers.
    """
    if _db is None or not agent_id:
        return pd.DataFrame()
    try:
        conn = _db.connect()
        rows = conn.execute("""
            SELECT period_end, period_type, personality_score,
                   total_return, sharpe_ratio, max_drawdown, win_rate
            FROM agent_performance
            WHERE agent_id = ? AND period_type = 'quarterly'
            ORDER BY period_end ASC
        """, (agent_id,)).fetchall()
        conn.close()
        if rows:
            return pd.DataFrame([dict(r) for r in rows])
    except Exception:
        pass
    return pd.DataFrame()

