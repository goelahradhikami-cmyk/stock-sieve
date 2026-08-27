"""EV diagnostic: missed gains on non-deployed BUY episodes + funnel discrimination.

Part 1: For BUY episodes whose portfolio was empty (selection layer passed
nothing), recompute CSI300 T+20 forward return from market_index_daily —
this is the opportunity cost of an empty book.

Part 2: For the 10 episodes covered by shadow_funnel_log, bucket every
candidate by funnel outcome and compute forward T+20 stock returns per
bucket. This tests whether each funnel stage actually discriminates
future returns (i.e., whether the selection layer adds or destroys EV).

Buckets:
  stage1_liquidity  rejected at stage1, LOW_LIQUIDITY
  stage1_frm        rejected at stage1, DETERIORATING
  stage2_rs         rejected at stage2, WEAK_RS
  stage3_mispricing rejected at stage3, NO_MISPRICING
  passed_unselected final_pass=1 but not in shadow_candidates.selected
  selected          final_pass=1 and selected=1

Forward return convention matches the attribution backfiller:
close(entry trade_date) -> close(calendar T+20), raw prices.
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.local_provider import LocalDataProvider  # noqa: E402

SHADOW = REPO_ROOT / "data" / "shadow_trading.db"
CACHE = REPO_ROOT / "data" / "cache.db"
HORIZON = 20


def t20_date(calendar, idx, trade_date):
    i = idx.get(trade_date)
    if i is None:
        return None
    j = min(len(calendar) - 1, i + HORIZON)
    return calendar[j]


def main():
    shadow = sqlite3.connect(str(SHADOW))
    shadow.row_factory = sqlite3.Row
    cache = sqlite3.connect(str(CACHE))
    cache.row_factory = sqlite3.Row
    local = LocalDataProvider()

    calendar = [
        r[0]
        for r in cache.execute(
            "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
            "ORDER BY trade_date"
        )
    ]
    cal_idx = {d: i for i, d in enumerate(calendar)}
    idx_close = {
        r["trade_date"]: r["adj_close"]
        for r in cache.execute(
            "SELECT trade_date, adj_close FROM market_index_daily "
            "WHERE index_code='000300'"
        )
    }

    def index_t20(trade_date):
        end = t20_date(calendar, cal_idx, trade_date)
        p0, p1 = idx_close.get(trade_date), idx_close.get(end)
        if p0 is None or p1 is None:
            # nearest available close at or before the date
            def nearest(d):
                cands = [t for t in idx_close if t <= d]
                return idx_close[max(cands)] if cands else None
            p0, p1 = p0 or nearest(trade_date), p1 or (end and nearest(end))
        if not p0 or not p1:
            return None
        return (p1 - p0) / p0

    def stock_t20(code, trade_date):
        end = t20_date(calendar, cal_idx, trade_date)
        if end is None:
            return None
        try:
            df = local.get_daily_kline(code, trade_date, end)
            if df is None or df.empty or len(df) < 2:
                return None
            close = df.sort_values("date")["close"].values
            return float((close[-1] - close[0]) / close[0])
        except Exception:
            return None

    # ---------------- Part 1: missed gains ----------------
    rows = shadow.execute(
        "SELECT e.episode_id, e.trade_date, o.portfolio_return_t20 "
        "FROM shadow_episode e JOIN shadow_outcome o "
        "ON e.episode_id = o.episode_id "
        "WHERE e.decision='BUY'"
    ).fetchall()
    nodep = [r for r in rows if not r["portfolio_return_t20"]]
    gains = []
    for r in nodep:
        m = index_t20(r["trade_date"])
        if m is not None:
            gains.append((r["trade_date"], m))
    vals = [g for _, g in gains]
    print("=== Part 1: non-deployed BUY episodes (empty book) ===")
    print(f"episodes: {len(nodep)}, with index data: {len(gains)}")
    print(
        "CSI300 T+20: mean %.4f median %.4f positive-rate %.2f"
        % (statistics.mean(vals), statistics.median(vals),
           sum(1 for v in vals if v > 0) / len(vals))
    )
    pos = [v for v in vals if v > 0]
    print(
        "missed upside: %d positive windows, sum %.3f, mean of positives %.4f"
        % (len(pos), sum(pos), statistics.mean(pos) if pos else 0)
    )

    # ---------------- Part 2: funnel discrimination ----------------
    print("\n=== Part 2: funnel discrimination (forward T+20 returns) ===")
    selected = {
        (r["episode_id"], r["stock_code"])
        for r in shadow.execute(
            "SELECT episode_id, stock_code FROM shadow_candidates "
            "WHERE selected = 1"
        )
    }
    fl = shadow.execute(
        "SELECT episode_id, trade_date, stock_code, rejection_stage, "
        "rejection_reason, final_pass FROM shadow_funnel_log"
    ).fetchall()

    def bucket(r):
        if r["rejection_stage"] == "stage1":
            return (
                "stage1_liquidity"
                if r["rejection_reason"] == "LOW_LIQUIDITY"
                else "stage1_frm"
            )
        if r["rejection_stage"] == "stage2":
            return "stage2_rs"
        if r["rejection_stage"] == "stage3":
            return "stage3_mispricing"
        if (r["episode_id"], r["stock_code"]) in selected:
            return "selected"
        return "passed_unselected"

    buckets: dict[str, list[float]] = {}
    mkt_cache: dict[str, float | None] = {}
    done = 0
    for r in fl:
        ret = stock_t20(r["stock_code"], r["trade_date"])
        done += 1
        if done % 2000 == 0:
            print(f"  ... {done}/{len(fl)} priced", flush=True)
        if ret is None:
            continue
        if r["trade_date"] not in mkt_cache:
            mkt_cache[r["trade_date"]] = index_t20(r["trade_date"])
        mkt = mkt_cache[r["trade_date"]]
        b = bucket(r)
        buckets.setdefault(b, []).append(
            (ret, ret - mkt if mkt is not None else None)
        )

    print(f"\n{'bucket':<20} {'n':>6} {'mean_ret':>9} {'med_ret':>8} "
          f"{'mean_alpha':>10} {'med_alpha':>9} {'win%':>6}")
    for b in [
        "stage1_frm", "stage1_liquidity", "stage2_rs", "stage3_mispricing",
        "passed_unselected", "selected",
    ]:
        v = buckets.get(b)
        if not v:
            continue
        rets = [x[0] for x in v]
        alphas = [x[1] for x in v if x[1] is not None]
        print(
            f"{b:<20} {len(rets):>6} {statistics.mean(rets):>9.4f} "
            f"{statistics.median(rets):>8.4f} "
            f"{statistics.mean(alphas):>10.4f} "
            f"{statistics.median(alphas):>9.4f} "
            f"{sum(1 for x in rets if x > 0) / len(rets) * 100:>5.1f}%"
        )

    # per-episode view for selected vs others
    print("\n=== per-episode: selected vs all-candidates mean return ===")
    eps = sorted({r["episode_id"] for r in fl})
    for ep in eps:
        sub = [r for r in fl if r["episode_id"] == ep]
        td = sub[0]["trade_date"]
        sel_rets, all_rets = [], []
        for r in sub:
            ret = stock_t20(r["stock_code"], td)
            if ret is None:
                continue
            all_rets.append(ret)
            if (ep, r["stock_code"]) in selected:
                sel_rets.append(ret)
        if not all_rets:
            continue
        msg = (
            f"{ep} ({td}): candidates={len(all_rets)} "
            f"mean={statistics.mean(all_rets):.4f}"
        )
        if sel_rets:
            msg += (
                f" | selected={len(sel_rets)} "
                f"mean={statistics.mean(sel_rets):.4f}"
            )
        else:
            msg += " | selected=0"
        print(msg)


if __name__ == "__main__":
    main()
