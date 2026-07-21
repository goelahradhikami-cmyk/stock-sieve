-- Crowding Snapshot Table - Commit 6-S.17.4 (v3.5.1 Phase 1)
--
-- Crowding diagnostic data for Mechanism Identification (H3-A vs H3-B).
-- This is NOT an alpha factor and NOT a gate. It is a CONTROL variable
-- for Exp9/10/11 to distinguish:
--   H3-A: Uncertainty Asymmetry Premium (alpha survives crowding control)
--   H3-B: Crowding Avoidance Premium (alpha disappears with crowding control)
--
-- Design principles (6-S.17.3 freeze):
--   1. STORE RAW FEATURES, not just composite. Different crowding sources
--      (momentum, liquidity, volatility, attention) may drive H3-B
--      differently. Raw values enable post-hoc decomposition.
--   2. Vintage-safe: all features computed from data BEFORE trade_date.
--      No lookahead. Uses same TDX kline source as event_reaction.py.
--   3. RS is NOT rehabilitated as a gate. RS-like measures (momentum,
--      volume) are crowding DIAGNOSTICS only. The distinction:
--        WRONG (v3.2.2): RS_high -> BUY
--        RIGHT (v3.5.1): RS_high -> possibly_crowded (control variable)
--   4. crowding_score_v1 is a DIAGNOSTIC composite (equal-weight zscore
--      sum), NOT a production score. Used only for Exp9 regression.
--      Weighted/PCA versions deferred to v3.6 if H3-B is validated.
--
-- Four feature groups (all vintage-safe, from TDX kline BEFORE trade_date):
--   momentum:   return_20d, return_60d
--   liquidity:  turnover_percentile, volume_ratio
--   volatility: realized_vol_20d
--   attention:  abnormal_volume, price_gap
--
-- Joins 1:1 with Group A candidates on (security_id, trade_date).

CREATE TABLE IF NOT EXISTS crowding_snapshot (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity + vintage
    security_id                 TEXT NOT NULL,
    trade_date                  DATE NOT NULL,         -- decision date (vintage gate)

    -- ─── Momentum crowding (trailing returns BEFORE trade_date) ───
    return_20d                  REAL,                  -- raw, 20-trading-day return
    return_60d                  REAL,                  -- raw, 60-trading-day return
                                                      -- high momentum = market noticed = crowded

    -- ─── Liquidity crowding (volume-based) ───
    turnover_percentile         REAL,                  -- raw, cross-sectional turnover percentile at trade_date
                                                      -- (uses finance_snapshots.turnover_pct + float_mcap)
    volume_ratio                REAL,                  -- raw = volume(trade_date) / avg(volume, 20d)
                                                      -- high = abnormal volume spike = attention

    -- ─── Volatility crowding ───
    realized_vol_20d            REAL,                  -- raw, 20-day realized volatility (std of daily returns)
                                                      -- high vol = event-driven attention = potentially crowded

    -- ─── Attention crowding ───
    abnormal_volume             REAL,                  -- raw = (volume_td - avg_20d) / std_20d
                                                      -- volume spike z-score
    price_gap                   REAL,                  -- raw = |daily_return(trade_date)| (event-driven attention)

    -- ─── Control variables (for Exp9 regression) ───
    market_cap                  REAL,                  -- from security_master.total_mv (static)
    float_mcap                  REAL,                  -- from security_master.float_mv (static)

    -- ─── Composite (diagnostic only, NOT production) ───
    crowding_score_v1           REAL,                  -- = zscore(turnover_pct) + zscore(return_20d)
                                                      --   + zscore(realized_vol_20d) + zscore(abnormal_volume)
                                                      --   equal-weight, diagnostic only
                                                      --   high = more crowded

    -- Metadata
    source                      TEXT DEFAULT 'tdx_kline+finance_snapshots',
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(security_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_crowd_security ON crowding_snapshot(security_id);
CREATE INDEX IF NOT EXISTS idx_crowd_date ON crowding_snapshot(trade_date);
CREATE INDEX IF NOT EXISTS idx_crowd_score ON crowding_snapshot(crowding_score_v1);
