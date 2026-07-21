-- Earnings Sustainability Table - Commit 6-S.16.1 (v3.4 Phase 1)
--
-- Sustainability signals for the Expectation Quality Engine (EQE).
-- This is a DIAGNOSTIC OBJECT, not an alpha factor (yet). Phase 1.5
-- Ablation will decide whether sustainability has any alpha content
-- before it is promoted to a factor.
--
-- Design principles (6-S.16.0a freeze amendment):
--   1. STORE RAW VALUES + derived flags. Raw values enable threshold
--      tuning in v3.4.1 without re-backfill. Flags are tunable defaults.
--   2. Vintage-safe: report_date + available_date BOTH stored.
--      Queries MUST gate on available_date <= as_of_date. report_date
--      alone is NOT vintage-safe (report known before announcement).
--   3. operating_margin substituted for gross_margin (gross_margin table
--      financial_indicators has 0 rows; operating_profit/revenue derivable
--      from 159961 akshare_financials rows, 99.9% coverage).
--   4. Industry-standardized zscores for margin (company vs own history
--      AND vs industry cross-section) to avoid mis-killing growth industries.
--
-- Three sub-components (each raw + flag):
--   Alignment   : revenue-earnings decoupling detection
--   Persistence : 3-quarter acceleration trend / reversal / volatility
--   Margin norm : peak-margin mean-reversion risk (company + industry zscore)
--
-- Joins 1:1 with earnings_event_reaction on (security_id, available_date).

CREATE TABLE IF NOT EXISTS earnings_sustainability (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity + vintage (vintage-safe)
    security_id                 TEXT NOT NULL,
    report_date                 DATE,                  -- 财报报告日 (e.g. 2024-03-31)
    available_date              DATE NOT NULL,         -- 公告日 (event anchor, vintage gate)

    -- ─── Alignment: revenue-earnings decoupling ───
    revenue_yoy_current         REAL,                  -- raw, fractional (0.20 = +20%)
    earnings_yoy_current        REAL,                  -- raw, fractional (0.20 = +20%)
    profit_elasticity           REAL,                  -- raw = earnings_yoy / revenue_yoy
                                                      --   software ~4 (ok), manufacturing ~15 (suspect)
                                                      --   stored raw; industry standardization later
    alignment_flag              INTEGER,               -- derived (tunable):
                                                      --   1 = sign match AND |rev| >= 0.3*|earn|
                                                      --   0 = earnings up / revenue down OR extreme decouple

    -- ─── Persistence: 3-quarter acceleration trend ───
    -- accel_qN = earnings_yoy_qN - earnings_yoy_q(N+1)  (1st derivative, per period)
    -- q0 = most recent, q1 = prior, q2 = two periods prior
    accel_q0                    REAL,                  -- raw, current-period acceleration
    accel_q1                    REAL,                  -- raw, prior-period acceleration
    accel_q2                    REAL,                  -- raw, two-period-prior acceleration
    accel_trend                 REAL,                  -- raw = accel_q0 - accel_q2 (direction across 3p)
                                                      --   positive = acceleration increasing (sustained)
                                                      --   negative = acceleration fading (spike risk)
    accel_volatility            REAL,                  -- raw = std([accel_q0, accel_q1, accel_q2])
    reversal_count              INTEGER,               -- raw = sign flips across 3 periods
                                                      --   0 = monotonic, 2 = V-shape (spike)
    consistency_flag            INTEGER,               -- derived (tunable):
                                                      --   1 = accel_q0 > 0 AND reversal_count <= 1
                                                      --   0 = V-shape spike OR acceleration reversed

    -- ─── Margin normalization: peak-margin mean-reversion risk ───
    -- operating_margin = operating_profit / revenue (substituted for gross_margin)
    operating_margin_current    REAL,                  -- raw, current period
    operating_margin_3q_median  REAL,                  -- raw, median of current + 2 prior periods
    operating_margin_3q_std     REAL,                  -- raw, std of current + 2 prior periods
    company_margin_zscore       REAL,                  -- raw = (current - 3q_median) / 3q_std
                                                      --   high = margin at own-history peak
    industry_margin_zscore      REAL,                  -- raw = (current - industry_median_same_period) / industry_std
                                                      --   high = margin at industry peak (cross-sectional)
    margin_normalization_flag   INTEGER,               -- derived (tunable):
                                                      --   1 = max(company_z, industry_z) < 1.5
                                                      --   0 = at peak relative to own history OR industry

    -- ─── Composite ───
    sustainability_pass         INTEGER,               -- binary: 1 = all 3 flags pass, 0 = any fail
                                                      --   (hard AND in v3.4; soft scoring deferred to v3.5)
    failure_reason              TEXT,                  -- NULL if pass, else one of:
                                                      --   ALIGNMENT_DECOUPLE, CONSISTENCY_SPIKE,
                                                      --   MARGIN_PEAK, INSUFFICIENT_DATA

    -- Metadata
    industry                    TEXT,                  -- security_master.industry (for audit)
    as_of_date                  DATE,                  -- decision date this row was computed for
                                                      --   (vintage gate: available_date <= as_of_date)
    source                      TEXT DEFAULT 'akshare_financials+security_master',
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(security_id, available_date)
);

CREATE INDEX IF NOT EXISTS idx_es_security ON earnings_sustainability(security_id);
CREATE INDEX IF NOT EXISTS idx_es_available ON earnings_sustainability(available_date);
CREATE INDEX IF NOT EXISTS idx_es_as_of ON earnings_sustainability(as_of_date);
CREATE INDEX IF NOT EXISTS idx_es_pass ON earnings_sustainability(sustainability_pass);
CREATE INDEX IF NOT EXISTS idx_es_industry ON earnings_sustainability(industry, available_date);

-- schema_version lives in shadow_trading.db, not cache.db (where this table
-- resides). The backfill script records the version there separately; this
-- file is idempotent CREATE TABLE IF NOT EXISTS only.
