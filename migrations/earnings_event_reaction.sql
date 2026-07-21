-- Earnings Event Reaction Table - Commit 6-S.15.1 (v3.3 Phase 1)
--
-- Event-study data: how did the market react to earnings announcements?
-- This is NOT a factor table - it is event-study infrastructure for the
-- Expectation Gap Engine (EGE).
--
-- Core question EGE asks: "did the market underreact to earnings improvement?"
-- To answer, we need the price reaction AFTER the announcement, not momentum
-- (which spans the announcement and contaminates the signal).
--
-- Primary signal: sector_adjusted_return_t5
--   (5-day post-announcement return, stripped of sector beta)
-- Supporting curve: t1, t5, t10, t20 for event-reaction shape

CREATE TABLE IF NOT EXISTS earnings_event_reaction (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id             TEXT NOT NULL,
    announcement_date       DATE,                  -- report_date (财报报告日)
    available_date          DATE NOT NULL,         -- 公告日（事件锚点，vintage-aware）

    -- Earnings side (vintage-aware, from akshare_financials)
    earnings_yoy_current    REAL,                  -- most recent period YoY
    earnings_yoy_previous   REAL,                  -- prior period YoY
    earnings_yoy_previous2  REAL,                  -- two periods prior (for 2nd derivative)
    earnings_acceleration   REAL,                  -- current - previous (1st derivative)
    earnings_acceleration_2nd REAL,                -- (cur-prev) - (prev-prev2) (2nd derivative)
    frm_direction           TEXT,                  -- improving/stable/deteriorating

    -- Price reaction (raw, from TDX kline)
    -- All returns measured from next_trading_day(available_date) forward
    return_t1               REAL,                  -- 1-day post-announcement
    return_t5               REAL,                  -- 5-day (PRIMARY signal window)
    return_t10              REAL,                  -- 10-day
    return_t20              REAL,                  -- 20-day

    -- Market adjusted (HS300 benchmark)
    market_return_t1        REAL,
    market_return_t5        REAL,
    market_return_t10       REAL,
    market_return_t20       REAL,
    market_adjusted_t1      REAL,                  -- return_t1 - market_return_t1
    market_adjusted_t5      REAL,
    market_adjusted_t10     REAL,
    market_adjusted_t20     REAL,

    -- Sector adjusted (industry_daily_returns, market-cap weighted)
    sector_code             TEXT,
    sector_return_t1        REAL,
    sector_return_t5        REAL,
    sector_return_t10       REAL,
    sector_return_t20       REAL,
    sector_adjusted_t1      REAL,                  -- return_t1 - sector_return_t1
    sector_adjusted_t5      REAL,                  -- PRIMARY SIGNAL
    sector_adjusted_t10     REAL,
    sector_adjusted_t20     REAL,

    -- Fully neutral (market + sector)
    residual_t5             REAL,                  -- return_t5 - market - sector
    residual_t20            REAL,

    -- Metadata
    source                  TEXT DEFAULT 'tdx+akshare',
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(security_id, available_date)
);

CREATE INDEX IF NOT EXISTS idx_eer_security ON earnings_event_reaction(security_id);
CREATE INDEX IF NOT EXISTS idx_eer_date ON earnings_event_reaction(available_date);
CREATE INDEX IF NOT EXISTS idx_eer_direction ON earnings_event_reaction(frm_direction);
CREATE INDEX IF NOT EXISTS idx_eer_sector ON earnings_event_reaction(sector_code, available_date);

INSERT OR REPLACE INTO schema_version (version, description) VALUES
    ('v4.0', 'earnings_event_reaction table for v3.3 EGE event-study infrastructure (6-S.15.1)');
