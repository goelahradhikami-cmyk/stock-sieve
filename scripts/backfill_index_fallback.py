"""Backfill index-fallback portfolio returns for BUY episodes (EV diagnostic P0).

Rule (fallback ladder):
  - selected >= MIN_POSITIONS (5): no change, portfolio = selected equal-weight
  - 1 <= selected < 5: 5 equal slots; selected fill their slots, CSI300 fills
    the remaining slots
  - selected == 0: 100% CSI300 (no more idle cash on BUY days)

The frozen baseline columns (portfolio_return_t20, alpha_vs_hs300) are NOT
touched. New columns on shadow_outcome:
  - n_selected              number of selected candidates
  - fallback_weight         index share of the fallback portfolio (0..1)
  - portfolio_return_t20_fb fallback-adjusted portfolio return
  - alpha_vs_hs300_fb       fallback alpha vs CSI300

Usage:
    python scripts/backfill_index_fallback.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.index_provider import IndexDataProvider  # noqa: E402
from src.data.local_provider import LocalDataProvider  # noqa: E402

SHADOW_DB = "data/shadow_trading.db"
CACHE_DB = "data/cache.db"
HORIZON = 20
MIN_POSITIONS = 5

NEW_COLUMNS = [
    ("n_selected", "INTEGER"),
    ("fallback_weight", "REAL"),
    ("portfolio_return_t20_fb", "REAL"),
    ("alpha_vs_hs300_fb", "REAL"),
]


def ensure_columns(conn: sqlite3.Connection):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(shadow_outcome)")}
    for name, typ in NEW_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE shadow_outcome ADD COLUMN {name} {typ}")
    conn.commit()


def compute_fallback(conn, idx, local, episode_id, trade_date, eval_date):
    """Return (n_sel, fb_weight, fb_return, fb_alpha) or None."""
    selected = conn.execute(
        "SELECT stock_code FROM shadow_candidates "
        "WHERE episode_id=? AND selected=1",
        (episode_id,),
    ).fetchall()
    n_sel = len(selected)

    mkt = idx.get_return("000300", trade_date, eval_date)
    if mkt is None:
        return None

    if n_sel >= MIN_POSITIONS:
        # no fallback needed; mirror baseline
        rets = []
        for c in selected:
            ret = _stock_return(local, c["stock_code"], trade_date, eval_date)
            if ret is not None:
                rets.append(ret)
        if not rets:
            return None
        port = float(np.mean(rets))
        return n_sel, 0.0, port, port - mkt

    # partial or empty book: 5 equal slots, index fills the rest
    slot_rets = []
    for c in selected:
        ret = _stock_return(local, c["stock_code"], trade_date, eval_date)
        slot_rets.append(ret if ret is not None else mkt)
    n_fill = MIN_POSITIONS - n_sel
    slot_rets.extend([mkt] * n_fill)
    fb_return = float(np.mean(slot_rets))
    fb_weight = n_fill / MIN_POSITIONS
    return n_sel, fb_weight, fb_return, fb_return - mkt


def _stock_return(local, code, start, end):
    try:
        kline = local.get_daily_kline(code, start, end)
        if kline is not None and not kline.empty and len(kline) >= 2:
            close = kline["close"].values
            return float((close[-1] - close[0]) / close[0])
    except Exception:
        pass
    return None


def main():
    conn = sqlite3.connect(SHADOW_DB)
    conn.row_factory = sqlite3.Row
    ensure_columns(conn)

    idx = IndexDataProvider()
    local = LocalDataProvider()
    cache = sqlite3.connect(CACHE_DB)

    episodes = conn.execute(
        "SELECT e.episode_id, e.trade_date, o.portfolio_return_t20, "
        "o.alpha_vs_hs300, o.evaluated_at "
        "FROM shadow_episode e JOIN shadow_outcome o "
        "ON e.episode_id = o.episode_id "
        "WHERE e.decision='BUY' AND o.evaluated_at IS NOT NULL "
        "ORDER BY e.trade_date"
    ).fetchall()
    print(f"BUY episodes with outcome: {len(episodes)}")

    done = 0
    base_rets, fb_rets, base_alphas, fb_alphas = [], [], [], []
    for ep in episodes:
        eval_row = cache.execute(
            "SELECT trade_date FROM trading_calendar WHERE is_trading=1 "
            "AND trade_date > ? ORDER BY trade_date LIMIT 1 OFFSET ?",
            (ep["trade_date"], HORIZON - 1),
        ).fetchone()
        if not eval_row:
            continue
        got = compute_fallback(
            conn, idx, local, ep["episode_id"], ep["trade_date"], eval_row[0]
        )
        if got is None:
            continue
        n_sel, fb_w, fb_ret, fb_alpha = got
        conn.execute(
            "UPDATE shadow_outcome SET n_selected=?, fallback_weight=?, "
            "portfolio_return_t20_fb=?, alpha_vs_hs300_fb=? "
            "WHERE episode_id=?",
            (n_sel, fb_w, fb_ret, fb_alpha, ep["episode_id"]),
        )
        done += 1
        base_rets.append(ep["portfolio_return_t20"] or 0.0)
        fb_rets.append(fb_ret)
        base_alphas.append(ep["alpha_vs_hs300"] or 0.0)
        fb_alphas.append(fb_alpha)

    conn.commit()

    print(f"backfilled: {done}")
    b, f = np.array(base_rets), np.array(fb_rets)
    ba, fa = np.array(base_alphas), np.array(fb_alphas)
    print("\n=== baseline (current, empty book = cash) vs fallback (index) ===")
    print(f"portfolio T+20 mean:   baseline {b.mean():+.4f}  fallback {f.mean():+.4f}")
    print(f"portfolio T+20 median: baseline {np.median(b):+.4f}  fallback {np.median(f):+.4f}")
    print(f"alpha mean:            baseline {ba.mean():+.4f}  fallback {fa.mean():+.4f}")
    print(f"sum of T+20 returns:   baseline {b.sum():+.4f}  fallback {f.sum():+.4f}")
    print(f"positive-rate:         baseline {(b > 0).mean():.2f}  fallback {(f > 0).mean():.2f}")
    print(f"worst episode:         baseline {b.min():+.4f}  fallback {f.min():+.4f}")

    conn.close()
    cache.close()


if __name__ == "__main__":
    main()
