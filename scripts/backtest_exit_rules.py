"""Backtest exit rules for shadow-trading selected candidates (Issue #2).

Simulates per-candidate exit rules over the T+20 window after each episode's
trade_date, using local TDX daily bars (raw OHLC), and compares each rule
against the fixed T+20 baseline already stored in shadow_candidates.

Rules implemented:
  baseline_t20      exit at T+20 close (stored behavior)
  stop_8pct         hard stop at entry_close * 0.92 (intraday low triggers,
                    exit at stop price)
  stop_atr2         hard stop at entry_close - 2 * ATR20 (pre-entry bars)
  time_stop_t10     at T+10, exit if stock cum. return < sector cum. return
  trail_15_5        after close gain >= +15%, exit on close 5% below the
                    post-trigger peak close
  combo             stop_atr2 + time_stop_t10 + trail_15_5 combined

Residual alpha is measured vs the candidate's sector cumulative return
(compounded industry_daily_returns) over the actual holding window.

Limitations:
  - Raw (unadjusted) prices are used to match the stored stock_return_t20
    convention; dividend gaps may rarely false-trigger stops.
  - Stop exits assume fill at the stop price (no gap-down slippage model).
  - "Logic-falsification" and "timing-linked" exits from Issue #2 are NOT
    backtested here: the former needs daily pessimism series we do not store,
    the latter needs daily timing decisions which episodes (sparse) lack.

Usage:
    python scripts/backtest_exit_rules.py
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.local_provider import LocalDataProvider  # noqa: E402

SHADOW_DB = REPO_ROOT / "data" / "shadow_trading.db"
CACHE_DB = REPO_ROOT / "data" / "cache.db"
HORIZON = 20
PRE_BARS = 30  # bars before entry for ATR


@dataclass
class Trade:
    episode_id: str
    code: str
    entry_date: str
    rule: str
    exit_date: str = ""
    exit_reason: str = ""
    ret: float = 0.0
    sector_ret: float = 0.0
    alpha: float = 0.0
    holding_days: int = 0


class ExitRuleBacktester:
    def __init__(self):
        self.shadow = sqlite3.connect(str(SHADOW_DB))
        self.shadow.row_factory = sqlite3.Row
        self.cache = sqlite3.connect(str(CACHE_DB))
        self.cache.row_factory = sqlite3.Row
        self.local = LocalDataProvider()
        self._calendar: list[str] = [
            r[0]
            for r in self.cache.execute(
                "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
                "ORDER BY trade_date"
            )
        ]
        self._cal_idx = {d: i for i, d in enumerate(self._calendar)}
        self._industry_cache: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    def load_candidates(self) -> list[sqlite3.Row]:
        return self.shadow.execute(
            "SELECT c.episode_id, c.stock_code, c.stock_return_t20, "
            "c.sector_return_t20, e.trade_date "
            "FROM shadow_candidates c "
            "JOIN shadow_episode e ON e.episode_id = c.episode_id "
            "WHERE c.selected = 1 AND c.stock_return_t20 IS NOT NULL "
            "ORDER BY e.trade_date"
        ).fetchall()

    def _industry(self, code: str) -> str | None:
        if code not in self._industry_cache:
            row = self.cache.execute(
                "SELECT industry FROM security_master WHERE code = ?", (code,)
            ).fetchone()
            self._industry_cache[code] = row["industry"] if row else None
        return self._industry_cache[code]

    def _sector_cum_return(
        self, industry: str | None, start: str, end: str
    ) -> float:
        if not industry:
            return 0.0
        rows = self.cache.execute(
            "SELECT return FROM industry_daily_returns "
            "WHERE industry = ? AND trade_date > ? AND trade_date <= ? "
            "ORDER BY trade_date",
            (industry, start, end),
        ).fetchall()
        cum = 1.0
        for r in rows:
            if r["return"] is not None:
                cum *= 1.0 + r["return"]
        return cum - 1.0

    # ------------------------------------------------------------------
    @staticmethod
    def _atr20(bars) -> float | None:
        """ATR over the last 20 pre-entry bars (list of (high, low, close))."""
        if len(bars) < 21:
            return None
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i][0], bars[i][1], bars[i - 1][2]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs[-20:]) / 20.0

    def _window(self, code: str, entry_date: str):
        """Return (pre_bars, fwd_bars) as lists of dicts with raw OHLC."""
        i = self._cal_idx.get(entry_date)
        if i is None:
            return None, None
        start = self._calendar[max(0, i - PRE_BARS)]
        end_i = min(len(self._calendar) - 1, i + HORIZON)
        end = self._calendar[end_i]
        df = self.local.get_daily_kline(code, start, end)
        if df is None or df.empty:
            return None, None
        df = df.sort_values("date")
        pre, fwd = [], []
        for row in df.itertuples():
            bar = {
                "date": str(row.date)[:10],
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
            }
            if bar["date"] < entry_date:
                pre.append(bar)
            elif bar["date"] > entry_date:
                fwd.append(bar)
        # entry bar itself (close = entry price)
        entry_rows = df[df["date"].astype(str).str[:10] == entry_date]
        if entry_rows.empty:
            return None, None
        entry_close = float(entry_rows.iloc[0]["close"])
        return (pre, fwd, entry_close, end)

    # ------------------------------------------------------------------
    def simulate(self, code: str, episode_id: str, entry_date: str) -> dict:
        """Run all rules for one candidate. Returns rule -> Trade."""
        got = self._window(code, entry_date)
        if not got or got[0] is None:
            return {}
        pre, fwd, entry_close, t20_date = got
        # Align with stored convention: eval at calendar T+20; suspended names
        # simply have fewer bars — exit at the last available close <= T+20.
        fwd = [b for b in fwd if b["date"] <= t20_date]
        if len(fwd) < 10:
            return {}  # too few bars to simulate meaningfully
        industry = self._industry(code)
        atr = self._atr20([(b["high"], b["low"], b["close"]) for b in pre])

        def mktrade(rule, idx, reason, exit_price):
            bar = fwd[idx]
            ret = (exit_price - entry_close) / entry_close
            sec = self._sector_cum_return(industry, entry_date, bar["date"])
            return Trade(
                episode_id=episode_id,
                code=code,
                entry_date=entry_date,
                rule=rule,
                exit_date=bar["date"],
                exit_reason=reason,
                ret=ret,
                sector_ret=sec,
                alpha=ret - sec,
                holding_days=idx + 1,
            )

        last = len(fwd) - 1
        trades: dict[str, Trade] = {}

        # 1. baseline: hold to T+20 close
        trades["baseline_t20"] = mktrade(
            "baseline_t20", last, "t20_close", fwd[last]["close"]
        )

        # 2. stop -8%
        stop8 = entry_close * 0.92
        t = None
        for i, b in enumerate(fwd):
            if b["low"] <= stop8:
                t = mktrade("stop_8pct", i, "stop_loss", stop8)
                break
        trades["stop_8pct"] = t or mktrade(
            "stop_8pct", last, "t20_close", fwd[last]["close"]
        )

        # 3. stop 2*ATR
        if atr:
            stop_atr = entry_close - 2 * atr
            t = None
            for i, b in enumerate(fwd):
                if b["low"] <= stop_atr:
                    t = mktrade("stop_atr2", i, "stop_loss", stop_atr)
                    break
            trades["stop_atr2"] = t or mktrade(
                "stop_atr2", last, "t20_close", fwd[last]["close"]
            )

        # 4. time stop at calendar T+10 vs sector
        i_entry = self._cal_idx[entry_date]
        t10_date = self._calendar[
            min(len(self._calendar) - 1, i_entry + HORIZON // 2)
        ]
        i10 = max(
            (i for i, b in enumerate(fwd) if b["date"] <= t10_date),
            default=None,
        )
        if i10 is None:
            return {}
        stock_cum_10 = (fwd[i10]["close"] - entry_close) / entry_close
        sec_cum_10 = self._sector_cum_return(
            industry, entry_date, fwd[i10]["date"]
        )
        if stock_cum_10 < sec_cum_10:
            trades["time_stop_t10"] = mktrade(
                "time_stop_t10", i10, "underperform_sector", fwd[i10]["close"]
            )
        else:
            trades["time_stop_t10"] = mktrade(
                "time_stop_t10", last, "t20_close", fwd[last]["close"]
            )

        # 5. trailing take profit: arm at +15%, exit on -5% from peak close
        armed, peak = False, entry_close
        t = None
        for i, b in enumerate(fwd):
            c = b["close"]
            if not armed and (c - entry_close) / entry_close >= 0.15:
                armed, peak = True, c
            if armed:
                peak = max(peak, c)
                if c <= peak * 0.95:
                    t = mktrade("trail_15_5", i, "trailing_exit", c)
                    break
        trades["trail_15_5"] = t or mktrade(
            "trail_15_5", last, "t20_close", fwd[last]["close"]
        )

        # 6. combo: stop_atr2 + time_stop_t10 + trail_15_5
        if atr:
            armed, peak = False, entry_close
            t = None
            for i, b in enumerate(fwd):
                if b["low"] <= stop_atr:
                    t = mktrade("combo", i, "stop_loss", stop_atr)
                    break
                c = b["close"]
                if i == i10:
                    sc10 = (c - entry_close) / entry_close
                    sec10 = self._sector_cum_return(
                        industry, entry_date, b["date"]
                    )
                    if sc10 < sec10:
                        t = mktrade("combo", i, "underperform_sector", c)
                        break
                if not armed and (c - entry_close) / entry_close >= 0.15:
                    armed, peak = True, c
                if armed:
                    peak = max(peak, c)
                    if c <= peak * 0.95:
                        t = mktrade("combo", i, "trailing_exit", c)
                        break
            trades["combo"] = t or mktrade(
                "combo", last, "t20_close", fwd[last]["close"]
            )

        return trades

    # ------------------------------------------------------------------
    def run(self):
        cands = self.load_candidates()
        print(f"selected candidates with T+20 outcome: {len(cands)}")
        all_trades: dict[str, list[Trade]] = {}
        skipped = 0
        sanity_diffs = []
        for c in cands:
            trades = self.simulate(
                c["stock_code"], c["episode_id"], c["trade_date"]
            )
            if not trades:
                skipped += 1
                continue
            # sanity: baseline should match stored stock_return_t20
            stored = c["stock_return_t20"]
            sim = trades["baseline_t20"].ret
            sanity_diffs.append(abs(sim - stored))
            for rule, t in trades.items():
                all_trades.setdefault(rule, []).append(t)

        print(f"skipped (missing/incomplete bars): {skipped}")
        if sanity_diffs:
            big = sum(1 for d in sanity_diffs if d > 0.01)
            print(
                f"baseline-vs-stored |diff|>1pct: {big}/{len(sanity_diffs)}, "
                f"median diff {statistics.median(sanity_diffs):.4f}"
            )
        return all_trades


def summarize(rule: str, trades: list[Trade]) -> dict:
    rets = [t.ret for t in trades]
    alphas = [t.alpha for t in trades]
    holds = [t.holding_days for t in trades]
    exits: dict[str, int] = {}
    for t in trades:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    return {
        "rule": rule,
        "n": len(trades),
        "mean_ret": statistics.mean(rets),
        "median_ret": statistics.median(rets),
        "win_rate": sum(1 for r in rets if r > 0) / len(rets),
        "worst": min(rets),
        "mean_alpha": statistics.mean(alphas),
        "median_alpha": statistics.median(alphas),
        "alpha_pos_rate": sum(1 for a in alphas if a > 0) / len(alphas),
        "avg_hold": statistics.mean(holds),
        "exits": exits,
    }


def main():
    bt = ExitRuleBacktester()
    all_trades = bt.run()
    rows = [summarize(r, ts) for r, ts in sorted(all_trades.items())]

    header = (
        f"{'rule':<14} {'n':>4} {'mean_ret':>9} {'med_ret':>8} "
        f"{'win%':>6} {'worst':>8} {'mean_a':>8} {'med_a':>8} "
        f"{'a_pos%':>7} {'hold':>5}"
    )
    print("\n" + header)
    print("-" * len(header))
    for s in rows:
        print(
            f"{s['rule']:<14} {s['n']:>4} {s['mean_ret']:>9.4f} "
            f"{s['median_ret']:>8.4f} {s['win_rate'] * 100:>5.1f}% "
            f"{s['worst']:>8.4f} {s['mean_alpha']:>8.4f} "
            f"{s['median_alpha']:>8.4f} {s['alpha_pos_rate'] * 100:>6.1f}% "
            f"{s['avg_hold']:>5.1f}"
        )
    print()
    for s in rows:
        print(f"{s['rule']}: exits {s['exits']}")

    # markdown report
    out = REPO_ROOT / "data" / "reports" / "exit_rules_backtest_2026-08-27.md"
    lines = [
        "# 退出规则回测（Issue #2）",
        "",
        "- 样本：shadow_candidates 中 `selected=1` 且已有 T+20 结果的候选",
        "- 窗口：入场日（episode trade_date 收盘）到 T+20 收盘",
        "- 价格：本地 TDX 日线原始 OHLC（与存量 stock_return_t20 口径一致）",
        "- 基准：行业累计收益（industry_daily_returns 复合），alpha = 个股收益 - 行业收益",
        "- 生成：2026-08-27，scripts/backtest_exit_rules.py",
        "",
        "| 规则 | n | 平均收益 | 中位收益 | 胜率 | 最差 | 平均alpha | 中位alpha | alpha>0占比 | 平均持有天数 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in rows:
        lines.append(
            f"| {s['rule']} | {s['n']} | {s['mean_ret']:.2%} | "
            f"{s['median_ret']:.2%} | {s['win_rate']:.1%} | {s['worst']:.2%} | "
            f"{s['mean_alpha']:.2%} | {s['median_alpha']:.2%} | "
            f"{s['alpha_pos_rate']:.1%} | {s['avg_hold']:.1f} |"
        )
    lines.append("")
    lines.append("## 退出原因分布")
    lines.append("")
    for s in rows:
        lines.append(f"- **{s['rule']}**: {s['exits']}")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nreport written: {out}")


if __name__ == "__main__":
    main()
