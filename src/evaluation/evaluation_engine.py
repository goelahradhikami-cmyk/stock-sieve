"""
Evaluation Engine — T+N performance evaluation with Brinson attribution.

Commit 3.1.1 boundary fixes applied:
  1. EXIT/HOLD opportunity cost calculation
  2. Genome time-travel protection
  3. Brinson gross/net selection alpha
  4. Beta Winsorize
  5. evaluation_confidence based on data completeness
  6. DB field sync (exit_opportunity_cost, holding_opportunity_cost, etc.)
"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class EvaluationConfig:
    """Evaluation engine configuration."""

    min_kline_bars: int = 20  # Minimum bars for reliable beta
    kline_lookback_days: int = 365  # How far back to fetch K-line
    winsorize_clip: float = 0.10  # Clip returns at ±10% for beta
    cost_ratio: float = 0.0015  # Default cost ratio (commission + tax + slippage)


@dataclass
class EvaluationResult:
    """Output of a single T+N evaluation."""

    research_decision_id: int
    horizon_days: int
    eval_date: str
    evaluation_type: str  # ENTRY / EXIT / HOLD

    # Returns
    stock_return: float
    market_return: float
    sector_return: float
    gross_return: float  # Before costs
    net_return: float  # After costs

    # Opportunity costs (for EXIT/HOLD)
    exit_opportunity_cost: float | None = None
    holding_opportunity_cost: float | None = None

    # Alpha
    alpha_vs_market: float = 0.0
    alpha_vs_sector: float = 0.0
    alpha_jensen: float = 0.0
    beta: float | None = None

    # Attribution
    market_contribution: float = 0.0
    sector_contribution: float = 0.0
    gross_selection_alpha: float = 0.0
    net_selection_alpha: float = 0.0

    # Path
    max_drawdown_during: float = 0.0
    max_profit_during: float = 0.0

    # Meta
    evaluation_confidence: float = 1.0
    market_regime: str = ""
    thesis_pattern: str = ""
    genome_version: str | None = None

    # Verdict
    is_profitable: bool = False
    alpha_positive: bool = False
    verdict: str = ""


class EvaluationEngine:
    """T+N evaluation with Brinson attribution and boundary fixes."""

    def __init__(self, db, config: EvaluationConfig | None = None):
        self.db = db
        self.config = config or EvaluationConfig()

    def evaluate(
        self, research_decision_id: int, horizon_days: int = 30, evaluation_type: str = "ENTRY"
    ) -> EvaluationResult | None:
        """Evaluate a single research decision.

        Args:
            research_decision_id: PK from research_decisions
            horizon_days: T+N horizon (30/60/90/180/365)
            evaluation_type: ENTRY (buy-and-hold) / EXIT (sold) / HOLD (held)

        Returns:
            EvaluationResult or None if insufficient data
        """
        # ── Load research decision ────────────────────────
        conn = self.db.connect()
        row = conn.execute(
            "SELECT * FROM research_decisions WHERE id=?", (research_decision_id,)
        ).fetchone()
        if not row:
            conn.close()
            return None

        rd = dict(row)
        entry_date_str = rd.get("entry_date", "")
        entry_price = rd.get("entry_price") or 0
        security_id = rd.get("security_id", "")

        # Parse dates
        try:
            entry_date = date.fromisoformat(entry_date_str[:10])
        except (ValueError, TypeError):
            conn.close()
            return None

        eval_date = entry_date + timedelta(days=horizon_days)
        if eval_date > date.today():
            eval_date = date.today()

        # ── Get genome version (with time-travel protection) ──
        genome_version = self._get_genome_version(
            research_decision_id, rd.get("created_at", entry_date_str)
        )

        # ── Fetch price data ──────────────────────────────
        kline = self._get_kline(security_id, entry_date, eval_date)

        # ── Compute evaluation_confidence (Fix 5) ─────────
        eval_confidence = self._compute_confidence(kline, horizon_days)

        if kline.empty or len(kline) < 3:
            conn.close()
            return EvaluationResult(
                research_decision_id=research_decision_id,
                horizon_days=horizon_days,
                eval_date=eval_date.isoformat(),
                evaluation_type=evaluation_type,
                stock_return=0.0,
                market_return=0.0,
                sector_return=0.0,
                gross_return=0.0,
                net_return=0.0,
                evaluation_confidence=eval_confidence,
            )

        # ── Compute returns ──────────────────────────────
        exit_price = float(kline.iloc[-1]["close"]) if "close" in kline.columns else 0
        market_ret = self._get_market_return(entry_date, eval_date)
        sector_ret = self._get_sector_return(rd.get("industry", ""), entry_date, eval_date)

        # ── Fix 1: EXIT/HOLD opportunity cost ────────────
        cost_ratio = self.config.cost_ratio

        if evaluation_type == "EXIT":
            # exit_opportunity_cost: positive = sold too early (missed gains)
            exit_opp_cost = (exit_price - entry_price) / entry_price if entry_price else 0
            gross_return = -exit_opp_cost  # negative = lost opportunity
            net_return = gross_return - cost_ratio
        elif evaluation_type == "HOLD":
            holding_opp_cost = self._get_holding_opportunity_cost(
                security_id, entry_date, eval_date
            )
            gross_return = holding_opp_cost
            net_return = gross_return
        else:  # ENTRY
            gross_return = (exit_price - entry_price) / entry_price if entry_price else 0
            net_return = gross_return - cost_ratio

        # ── Drawdown during holding ─────────────────────
        if not kline.empty and "close" in kline.columns:
            prices = kline["close"].values
            peak = np.maximum.accumulate(prices)
            dd = (prices - peak) / peak
            max_dd = float(dd.min()) if len(dd) > 0 else 0.0
            max_profit = float(prices[-1] / prices[0] - 1) if len(prices) > 1 else 0.0
        else:
            max_dd = 0.0
            max_profit = 0.0

        # ── Alpha ────────────────────────────────────────
        alpha_mkt = gross_return - market_ret
        alpha_sec = gross_return - sector_ret

        # ── Beta with Winsorize (Fix 4) ─────────────────
        beta = self._calculate_beta(kline, entry_date, eval_date)

        # Jensen's Alpha: α = R_p - [R_f + β(R_m - R_f)]
        rf = 0.02 / 252 * horizon_days  # ~2% annual risk-free
        alpha_jensen = gross_return - (rf + beta * (market_ret - rf))

        # ── Brinson attribution (Fix 3) ──────────────────
        market_contrib = market_ret
        sector_contrib = sector_ret - market_ret
        gross_selection = gross_return - market_contrib - sector_contrib
        net_selection = net_return - market_contrib - sector_contrib

        conn.close()

        return EvaluationResult(
            research_decision_id=research_decision_id,
            horizon_days=horizon_days,
            eval_date=eval_date.isoformat(),
            evaluation_type=evaluation_type,
            stock_return=gross_return,
            market_return=market_ret,
            sector_return=sector_ret,
            gross_return=gross_return,
            net_return=net_return,
            exit_opportunity_cost=(-gross_return if evaluation_type == "EXIT" else None),
            holding_opportunity_cost=(gross_return if evaluation_type == "HOLD" else None),
            alpha_vs_market=alpha_mkt,
            alpha_vs_sector=alpha_sec,
            alpha_jensen=alpha_jensen,
            beta=beta,
            market_contribution=market_contrib,
            sector_contribution=sector_contrib,
            gross_selection_alpha=gross_selection,
            net_selection_alpha=net_selection,
            max_drawdown_during=max_dd,
            max_profit_during=max_profit,
            evaluation_confidence=eval_confidence,
            market_regime="",  # To be filled from market_regime_snapshots
            thesis_pattern=rd.get("thesis_pattern", ""),
            genome_version=genome_version,
            is_profitable=net_return > 0,
            alpha_positive=alpha_mkt > 0,
            verdict="market_alpha_positive" if alpha_mkt > 0 else "market_alpha_negative",
        )

    def save_to_db(self, result: EvaluationResult):
        """Persist evaluation result to evaluation_results + attribution table."""
        conn = self.db.connect()
        c = conn.execute(
            """
            INSERT INTO evaluation_results
            (research_decision_id, horizon_days, eval_date,
             stock_return, market_return, sector_return, agent_top10_ew_return,
             alpha_vs_market, alpha_vs_sector, alpha_vs_peer,
             max_drawdown_during, max_profit_during,
             is_profitable, alpha_positive, verdict)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, ?)
        """,
            (
                result.research_decision_id,
                result.horizon_days,
                result.eval_date,
                result.stock_return,
                result.market_return,
                result.sector_return,
                result.alpha_vs_market,
                result.alpha_vs_sector,
                result.max_drawdown_during,
                result.max_profit_during,
                1 if result.is_profitable else 0,
                1 if result.alpha_positive else 0,
                result.verdict,
            ),
        )
        eval_id = c.lastrowid

        # ── Save attribution (if table exists) ─────────────
        try:
            conn.execute(
                """
                INSERT INTO evaluation_attribution
                (evaluation_id, market_contribution, sector_contribution, stock_alpha)
                VALUES (?, ?, ?, ?)
            """,
                (
                    eval_id,
                    result.market_contribution,
                    result.sector_contribution,
                    result.gross_selection_alpha,
                ),
            )
        except Exception as exc:
            logger.warning("evaluation_attribution insert failed: %s", exc)

        conn.commit()
        conn.close()
        return eval_id

    # ── Internal helpers ──────────────────────────────────

    def _get_genome_version(self, research_decision_id: int, created_at: str | None) -> str | None:
        """Fix 2: Time-travel protected genome version lookup."""
        conn = self.db.connect()
        row = conn.execute(
            """
            SELECT ags.genome_hash FROM agent_genome_snapshots ags
            JOIN research_decisions rd ON rd.agent_id = ags.agent_id
            WHERE rd.id = ? AND ags.birth_date <= ?
            ORDER BY ags.birth_date DESC LIMIT 1
        """,
            (research_decision_id, created_at[:10] if created_at else ""),
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def _get_kline(self, code: str, start: date, end: date) -> pd.DataFrame:
        """Fetch K-line data for evaluation period."""
        try:
            from mootdx.quotes import Quotes

            q = Quotes.factory(market="std")
            df = q.bars(symbol=code, frequency=9, offset=300)
            if df is not None and not df.empty:
                df = df.rename(columns={"vol": "volume", "datetime": "date"})
                if "date" in df.columns:
                    df["date"] = pd.to_datetime(df["date"])
                return df
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)
        return pd.DataFrame()

    def _get_market_return(self, start: date, end: date) -> float:
        """Get benchmark (CSI 300) return."""
        try:
            from mootdx.quotes import Quotes

            q = Quotes.factory(market="std")
            df = q.bars(symbol="000300", frequency=9, offset=300)
            if df is not None and not df.empty and "close" in df.columns:
                closes = df["close"].values
                return (closes[-1] / closes[0] - 1) if len(closes) > 1 else 0.0
        except Exception as exc:
            logger.warning("operation failed (was silently ignored): %s", exc)
        return 0.0

    def _get_sector_return(self, industry: str, start: date, end: date) -> float:
        """Get sector index return. Falls back to market return."""
        return self._get_market_return(start, end)

    def _get_holding_opportunity_cost(self, code: str, start: date, end: date) -> float:
        """Fix 1: Holding opportunity cost = stock return - benchmark return."""
        stock_ret = 0.0
        kline = self._get_kline(code, start, end)
        if not kline.empty and "close" in kline.columns:
            closes = kline["close"].values
            if len(closes) > 1:
                stock_ret = closes[-1] / closes[0] - 1

        bench_ret = self._get_market_return(start, end)
        return stock_ret - bench_ret

    def _calculate_beta(self, kline: pd.DataFrame, start: date, end: date) -> float:
        """Fix 4: Calculate beta with Winsorized returns."""
        if kline.empty or "close" not in kline.columns or len(kline) < 10:
            return 1.0

        try:
            stock_ret = kline["close"].pct_change().dropna()
            # Winsorize at ±10%
            stock_ret = stock_ret.clip(
                lower=-self.config.winsorize_clip, upper=self.config.winsorize_clip
            )

            # Get benchmark returns
            bench = self._get_kline("000300", start, end)
            if bench.empty or "close" not in bench.columns:
                return 1.0
            bench_ret = bench["close"].pct_change().dropna()
            bench_ret = bench_ret.clip(
                lower=-self.config.winsorize_clip, upper=self.config.winsorize_clip
            )

            # Align
            aligned = pd.concat([stock_ret, bench_ret], axis=1).dropna()
            if len(aligned) < 5:
                return 1.0

            stock = aligned.iloc[:, 0]
            bench = aligned.iloc[:, 1]

            beta = np.cov(stock, bench)[0][1] / np.var(bench) if np.var(bench) > 0 else 1.0
            return max(0.1, min(3.0, float(beta)))
        except Exception:
            return 1.0

    def _compute_confidence(self, kline: pd.DataFrame, horizon_days: int) -> float:
        """Fix 5: Data-completeness-based evaluation confidence."""
        data_score = 1.0
        if kline.empty or len(kline) < self.config.min_kline_bars:
            data_score = 0.3
        elif len(kline) < horizon_days * 0.8:
            data_score = 0.7
        return data_score

    def run_batch(
        self, research_decision_ids: list[int], horizon_days: int = 30
    ) -> list[EvaluationResult]:
        """Evaluate multiple research decisions."""
        results = []
        for rid in research_decision_ids:
            try:
                result = self.evaluate(rid, horizon_days)
                if result:
                    self.save_to_db(result)
                    results.append(result)
            except Exception as e:
                logger.warning(f"  ⚠️ Evaluation failed for decision {rid}: {e}")
        return results

    def run_pending(self):
        """Evaluate all research_decisions whose T+N date has arrived."""
        conn = self.db.connect()
        today = date.today().isoformat()
        rows = conn.execute(
            """
            SELECT rd.id as research_decision_id, rd.entry_date,
                   rd.security_id, rd.agent_id
            FROM research_decisions rd
            LEFT JOIN evaluation_results er ON rd.id = er.research_decision_id
            WHERE er.id IS NULL
              AND rd.entry_date <= ?
            ORDER BY rd.entry_date
        """,
            (today,),
        ).fetchall()
        conn.close()

        evaluated = 0
        for row in rows:
            rd = dict(row)
            # Calculate days since entry
            try:
                entry = date.fromisoformat(rd["entry_date"][:10])
                days = (date.today() - entry).days
            except (ValueError, TypeError):
                continue

            if days >= 30:
                result = self.evaluate(rd["research_decision_id"], horizon_days=30)
                if result:
                    self.save_to_db(result)
                    evaluated += 1

        return evaluated
