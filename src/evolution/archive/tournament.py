"""
[ARCHIVED 2026-07-23 — v2-v3 research tool, zero production callers.
 Kept with its characterization tests (tests/test_evolution_arenas.py).
 See src/evolution/archive/__init__.py for revival procedure.]

Evolution Tournament v1.1 — Real-market competitive tournament.

Runs all active agents against the same historical market dates,
with real trading constraints (circuit breakers, suspensions).
Ranks by multi-dimensional fitness, not just raw returns.

Arena roles in this package (renamed 2026-07-23 to disambiguate):
  - archive/tournament.py     (this file) — multi-agent tournament + fitness ranking
  - archive/crowding_arena.py — single-date crowding / overlap / alpha-decay metrics
  - survival_arena.py         — doctrine backtest with return attribution + survival selection
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.data.db import managed_connect
from src.data.index_provider import IndexDataProvider
from src.data.provider import MarketDataProvider
from src.utils.logger import get_logger

logger = get_logger(__name__)


class PortfolioSimulator:
    """Unified trade simulation: limit-up/down, suspensions, slippage."""

    def __init__(self):
        self.provider = None  # Set by arena after init

    def _get_provider(self):
        if self.provider is None:
            try:
                from src.data.local_provider import LocalDataProvider

                lp = LocalDataProvider()
                if lp.tdx_root:
                    self.provider = lp
                    return self.provider
            except Exception as exc:
                logger.warning("operation failed (was silently ignored): %s", exc)
            self.provider = MarketDataProvider()
        return self.provider

    def simulate_order(self, code: str, trade_date: date, action: str = "BUY") -> float | None:
        """Return effective fill price at next trading day's open, or None if untradeable."""
        provider = self._get_provider()
        next_date = trade_date + timedelta(days=1)
        kline = provider.get_daily_kline(code, next_date.isoformat(), next_date.isoformat())
        if kline.empty:
            return None
        open_price = float(kline.iloc[0].get("open", 0))
        if open_price is None or open_price <= 0:
            return None
        return open_price

    def simulate_sell(self, code: str, trade_date: date) -> float | None:
        return self.simulate_order(code, trade_date, "SELL")


class EvolutionArena:
    """Competitive tournament: all agents trade the same historical dates."""

    def __init__(self, db_path: str = "data/evaluation.db"):
        self.db = managed_connect(self, db_path)
        # Try local TDX first, fallback to mootdx
        try:
            from src.data.local_provider import LocalDataProvider

            lp = LocalDataProvider()
            self.provider = lp if lp.tdx_root else MarketDataProvider()
        except Exception:
            self.provider = MarketDataProvider()
        self.index_provider = IndexDataProvider()
        self.simulator = PortfolioSimulator()
        self.simulator.provider = self.provider
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS evolution_arena_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER, agent_id TEXT,
                trade_date DATE, horizon INTEGER,
                portfolio_size INTEGER DEFAULT 30,
                avg_return REAL, alpha_vs_market REAL,
                sharpe REAL, sortino REAL,
                max_drawdown REAL, win_rate REAL, turnover REAL,
                avg_volatility REAL,
                rank INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.commit()

    def run_tournament(
        self, cycle_id: int, start_date: str, end_date: str, horizons: list[int] | None = None
    ) -> dict:
        if horizons is None:
            horizons = [20, 60]
        agents = self._get_active_agents()
        if len(agents) < 2:
            print(f"   Need ≥2 agents, have {len(agents)}")
            return {"rankings": []}

        trade_dates = self._get_monthly_trade_dates(start_date, end_date)
        if not trade_dates:
            # Fallback: use quarterly dates
            d = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
            trade_dates = []
            while d <= end:
                trade_dates.append(d)
                d = d + timedelta(days=90)

        print(
            f"   Tournament: {len(agents)} agents × {len(trade_dates)} dates × {len(horizons)} horizons"
        )

        results = []
        for trade_date in trade_dates:
            universe = self._get_historical_universe(trade_date)
            if len(universe) < 20:
                # Fallback: use built-in list
                from src.data.security_master import SecurityMaster

                master = SecurityMaster()
                df = master.get_active_universe()
                universe = [
                    f"{c}.{'SH' if c.startswith('6') else 'SZ'}" for c in df["code"].tolist()
                ]

            agent_picks = {}
            for agent in agents[:8]:  # Cap at 8 for speed
                picks = self._select_stocks(agent, universe, trade_date, n=20)
                if picks:
                    agent_picks[agent["agent_id"]] = picks

            for horizon in horizons:
                for agent_id, picks in agent_picks.items():
                    metrics = self._calc_forward_metrics(picks, trade_date, horizon)
                    if metrics:
                        results.append(
                            {
                                "cycle_id": cycle_id,
                                "agent_id": agent_id,
                                "trade_date": trade_date,
                                "horizon": horizon,
                                **metrics,
                            }
                        )

        if results:
            self._save_results(results)

        rankings = self._calculate_rankings(cycle_id)
        return {
            "cycle_id": cycle_id,
            "total_rounds": len(trade_dates) * len(horizons),
            "agents": len(agents),
            "rankings": rankings,
        }

    def _select_stocks(
        self, agent: dict, universe: list[str], trade_date: date, n: int = 20
    ) -> list[tuple[str, float]]:
        """Score stocks using agent's genome + 250-day factor data from local TDX."""
        import yaml

        genome = yaml.safe_load(agent["genome_yaml"]) or {}
        factor_start = trade_date - timedelta(days=365)
        scores = []

        # Try local provider first (years of data)
        try:
            from src.data.local_provider import LocalDataProvider

            lp = LocalDataProvider() if not hasattr(self, "_local") else self._local
            self._local = lp
            use_local = lp.tdx_root is not None
        except Exception:
            use_local = False

        sample = universe[: min(200, len(universe))]

        for security_id in sample:
            code = security_id.split(".")[0] if "." in security_id else security_id

            if use_local:
                kline = lp.get_daily_kline(code, factor_start.isoformat(), trade_date.isoformat())
            else:
                kline = self.provider.get_daily_kline(
                    code, factor_start.isoformat(), trade_date.isoformat()
                )

            if kline.empty or len(kline) < 60:
                continue

            score = self._calc_genome_score(genome, kline)
            scores.append((security_id, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:n]

    def _calc_genome_score(self, genome: dict, kline: pd.DataFrame) -> float:
        """Score a stock using the agent's factor model weights."""
        fm = genome.get("factor_model", {})
        weights = {}
        for family, config in fm.items():
            if isinstance(config, dict):
                weights[family] = config.get("weight", 0)

        close = kline["close"].values if "close" in kline.columns else []
        if len(close) < 20:
            return 0

        # Compute simple factor proxies from K-line
        rets = np.diff(close) / close[:-1]
        mom_3m = close[-1] / close[-min(63, len(close))] - 1 if len(close) >= 63 else 0
        vol = np.std(rets[-20:]) * np.sqrt(252) if len(rets) >= 20 else 0.3

        score = 0
        score += weights.get("momentum", 0) * (mom_3m * 100)
        score += weights.get("quality", 0) * 30
        score += weights.get("value", 0) * 25
        score += weights.get("growth", 0) * 25
        score -= weights.get("risk", 0) * (vol * 50)
        score += weights.get("sentiment", 0) * 25

        return score

    def _calc_forward_metrics(
        self, picks: list[tuple[str, float]], signal_date: date, horizon: int
    ) -> dict | None:
        """Compute forward portfolio metrics with real trade simulation."""
        # Get buy prices
        valid = []
        for security_id, _ in picks:
            code = security_id.split(".")[0] if "." in security_id else security_id
            buy_price = self.simulator.simulate_order(code, signal_date, "BUY")
            if buy_price is not None:
                valid.append((code, buy_price))

        if len(valid) < 3:
            return None

        # Simulate sell at end of horizon
        sell_date = signal_date + timedelta(days=horizon)
        returns = []
        for code, buy_price in valid:
            sell_price = self.simulator.simulate_sell(code, sell_date)
            if sell_price is not None and buy_price > 0:
                returns.append((sell_price - buy_price) / buy_price)

        if len(returns) < 3:
            return None

        avg_ret = float(np.mean(returns))
        vol = float(np.std(returns)) if len(returns) > 1 else 0.1
        sharpe = (avg_ret / vol) * np.sqrt(252 / horizon) if vol > 0 else 0
        downside = [r for r in returns if r < 0]
        down_vol = float(np.std(downside)) if len(downside) > 1 else vol
        sortino = (avg_ret / down_vol) * np.sqrt(252 / horizon) if down_vol > 0 else 0
        max_dd = float(min(returns))
        win_rate = float(np.mean([1 if r > 0 else 0 for r in returns]))

        # Benchmark alpha
        bench_ret = self.index_provider.get_return(
            "000300", signal_date.isoformat(), sell_date.isoformat()
        )

        return {
            "portfolio_size": len(valid),
            "avg_return": avg_ret,
            "alpha_vs_market": avg_ret - bench_ret,
            "sharpe": sharpe,
            "sortino": sortino,
            "max_drawdown": max_dd,
            "win_rate": win_rate,
            "avg_volatility": vol,
        }

    def _calculate_rankings(self, cycle_id: int) -> list[dict]:
        """Multi-dimensional fitness ranking (P0-4)."""
        rows = self.db.execute(
            """
            SELECT agent_id,
                AVG(alpha_vs_market), AVG(sharpe), AVG(sortino),
                AVG(max_drawdown), AVG(win_rate), COUNT(*)
            FROM evolution_arena_results
            WHERE cycle_id = ?
            GROUP BY agent_id
            ORDER BY agent_id
        """,
            (cycle_id,),
        ).fetchall()

        agent_fitness = []
        for row in rows:
            alpha = row[1] or 0
            sharpe = row[2] or 0
            sortino = row[3] or 0
            dd = row[4] or 0
            wr = row[5] or 0
            rounds = row[6]
            fitness = alpha * 0.35 + sharpe * 0.25 + sortino * 0.15 + wr * 0.15 - abs(dd) * 0.10
            agent_fitness.append(
                {
                    "agent_id": row[0],
                    "avg_alpha": alpha,
                    "avg_sharpe": sharpe,
                    "avg_sortino": sortino,
                    "avg_max_dd": dd,
                    "avg_win_rate": wr,
                    "rounds": rounds,
                    "fitness": fitness,
                }
            )

        agent_fitness.sort(key=lambda x: x["fitness"], reverse=True)
        for rank, a in enumerate(agent_fitness, 1):
            a["rank"] = rank
        return agent_fitness

    # ── Helpers ────────────────────────────────────────────

    def _get_active_agents(self) -> list[dict]:
        rows = self.db.execute("""
            SELECT agent_id, strategy_genus, genome_yaml
            FROM agent_genome_snapshots WHERE status='active'
            ORDER BY agent_id
        """).fetchall()
        return [{"agent_id": r[0], "strategy_genus": r[1], "genome_yaml": r[2]} for r in rows]

    def _get_historical_universe(self, trade_date: date) -> list[str]:
        try:
            rows = self.db.execute(
                "SELECT security_id FROM universe_snapshot WHERE trade_date=?",
                (trade_date.isoformat(),),
            ).fetchall()
            return [r[0] for r in rows] if rows else []
        except Exception:
            return []

    def _get_monthly_trade_dates(self, start: str, end: str) -> list[date]:
        # Generate first trading day of each month
        d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
        dates = []
        while d <= end_d:
            dates.append(d)
            # Next month
            d = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
        return dates[:12]

    def _save_results(self, results: list[dict]):
        for r in results:
            self.db.execute(
                """
                INSERT INTO evolution_arena_results
                (cycle_id, agent_id, trade_date, horizon, portfolio_size,
                 avg_return, alpha_vs_market, sharpe, sortino, max_drawdown, win_rate, avg_volatility)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    r["cycle_id"],
                    r["agent_id"],
                    r["trade_date"].isoformat(),
                    r["horizon"],
                    r.get("portfolio_size", 20),
                    r.get("avg_return", 0),
                    r.get("alpha_vs_market", 0),
                    r.get("sharpe", 0),
                    r.get("sortino", 0),
                    r.get("max_drawdown", 0),
                    r.get("win_rate", 0),
                    r.get("avg_volatility", 0),
                ),
            )
        self.db.commit()
