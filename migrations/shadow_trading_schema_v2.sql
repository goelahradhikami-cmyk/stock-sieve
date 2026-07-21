-- Shadow Trading Database Schema v2.0
-- Commit 6-S.12 (Security Analyst Reconstruction v2)
--
-- This migration extends the v1 schema with:
--   1. Recovery Beta Decomposition (6-S.12.1)
--      - Splits stock_return into market_beta + sector_beta + residual_alpha
--      - Enables attribution: "did we pick a good stock, or just ride the market/sector?"
--   2. Fundamental Recovery Layer (6-S.12.2)
--      - FRM Score (earnings_yoy revision proxy, offline-first)
--   3. Portfolio Construction Validation (6-S.12.4)
--      - Independent table to isolate Layer 3 failures from Layer 2
--
-- NOTE: industry_daily_returns data only covers 2024-06-04 onwards.
-- Episodes before 2024-06 will have NULL sector_return_t20 / sector_beta /
-- residual_alpha. market_beta is always available (HS300 data covers 2021+).
-- This limitation is recorded in the validation report, not hidden.

-- ═════════════════════════════════════════════════════════
-- 1. shadow_candidates: Recovery Beta Decomposition (6-S.12.1)
-- ═════════════════════════════════════════════════════════

-- stock_return_t20 already exists from v1 (populated by shadow_outcome_evaluator).
-- These new columns hold the attribution breakdown.

ALTER TABLE shadow_candidates ADD COLUMN market_return_t20 REAL;
-- HS300 return over [trade_date, T+20] for the episode

ALTER TABLE shadow_candidates ADD COLUMN sector_code TEXT;
-- The stock's industry (from security_master.industry), for reproducibility

ALTER TABLE shadow_candidates ADD COLUMN sector_return_t20 REAL;
-- Industry return over [trade_date, T+20]. NULL if industry data unavailable
-- (pre-2024-06 episodes or stocks without industry classification).

ALTER TABLE shadow_candidates ADD COLUMN market_beta REAL;
-- stock_return_t20 - market_return_t20
-- Positive = stock outperformed the market (market-adjusted alpha)

ALTER TABLE shadow_candidates ADD COLUMN sector_beta REAL;
-- stock_return_t20 - sector_return_t20
-- Positive = stock outperformed its sector (sector-adjusted alpha).
-- NULL when sector_return_t20 is NULL.

ALTER TABLE shadow_candidates ADD COLUMN residual_alpha REAL;
-- stock_return_t20 - market_return_t20 - sector_return_t20
-- The TRUE stock-picking alpha, stripped of both market and sector beta.
-- This is the metric Security Analyst v2 is designed to produce.
-- NULL when sector_return_t20 is NULL.

-- ═════════════════════════════════════════════════════════
-- 2. shadow_candidates: Fundamental Recovery Layer (6-S.12.2)
-- ═════════════════════════════════════════════════════════

ALTER TABLE shadow_candidates ADD COLUMN frm_score REAL;
-- Fundamental Recovery Momentum score (0-100).
-- Measures whether the business itself is recovering, using earnings_yoy
-- change as a revision proxy (offline-first, no external analyst data).

ALTER TABLE shadow_candidates ADD COLUMN earnings_yoy_current REAL;
-- Most recent vintage earnings_growth_1y at decision time

ALTER TABLE shadow_candidates ADD COLUMN earnings_yoy_previous REAL;
-- Prior vintage earnings_growth_1y (one quarter before current)

ALTER TABLE shadow_candidates ADD COLUMN earnings_revision_direction TEXT;
-- 'improving' / 'stable' / 'deteriorating'
-- improving = earnings_yoy_current > earnings_yoy_previous (trend reversal)
-- Used to detect "cheap but broken" failure pattern

ALTER TABLE shadow_candidates ADD COLUMN frm_earnings_acceleration REAL;
-- Subscore (0-100): earnings_yoy change direction and magnitude

ALTER TABLE shadow_candidates ADD COLUMN frm_margin_change REAL;
-- Subscore (0-100): margin stabilization (reuses anomaly margin_change)

ALTER TABLE shadow_candidates ADD COLUMN frm_revenue_acceleration REAL;
-- Subscore (0-100): revenue growth acceleration

-- ═════════════════════════════════════════════════════════
-- 3. shadow_outcome: sector alpha (6-S.12.1)
-- ═════════════════════════════════════════════════════════

ALTER TABLE shadow_outcome ADD COLUMN alpha_vs_sector REAL;
-- portfolio_return_t20 - sector_return_t20 (episode-level, BUY episodes)

-- ═════════════════════════════════════════════════════════
-- 4. shadow_portfolio_construction (6-S.12.4) - NEW TABLE
-- ═════════════════════════════════════════════════════════
--
-- Independent Layer 3 validation table. Isolates portfolio construction
-- failures (concentration, diversification) from selection failures (Layer 2).
-- Does NOT replace PortfolioAgent - it observes and records, so failure
-- attribution is unambiguous.

CREATE TABLE IF NOT EXISTS shadow_portfolio_construction (
    episode_id              TEXT PRIMARY KEY,

    -- Counts
    candidate_count         INTEGER,    -- stocks passing anomaly filter
    selected_count          INTEGER,    -- stocks with selected=1 (equal-weight basket)

    -- Concentration metrics (equal-weight assumption: weight = 1/selected_count)
    top1_weight             REAL,       -- max single-stock weight (1/n for equal-weight)
    herfindahl_index        REAL,       -- sum(weight_i^2), 0-1, higher = more concentrated

    -- Sector concentration
    sector_count            INTEGER,    -- distinct industries among selected
    max_sector_weight       REAL,       -- largest sector's weight in basket

    -- Gate results (1=pass, 0=fail)
    min_positions_pass      INTEGER,    -- selected_count >= 5
    max_concentration_pass  INTEGER,    -- top1_weight <= 0.25
    max_sector_pass         INTEGER,    -- max_sector_weight <= 0.40
    diversification_pass    INTEGER,    -- all three pass

    -- Diagnosis
    failure_reason          TEXT,       -- 'PASS' / 'FAIL_MIN_POSITIONS' / 'FAIL_CONCENTRATION' / 'FAIL_SECTOR' / 'FAIL_MIN_POSITIONS+CONCENTRATION' / ...
    -- 2025-08-13 will be FAIL_CONCENTRATION (selected_count=1, top1_weight=100%)

    evaluated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (episode_id) REFERENCES shadow_episode(episode_id)
);

CREATE INDEX IF NOT EXISTS idx_spc_pass ON shadow_portfolio_construction(diversification_pass);
CREATE INDEX IF NOT EXISTS idx_spc_fail ON shadow_portfolio_construction(failure_reason);

-- ═════════════════════════════════════════════════════════
-- 5. Schema version marker
-- ═════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS schema_version (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

INSERT OR REPLACE INTO schema_version (version, description) VALUES
    ('v1.0', 'Initial shadow trading schema (6-S.10.1)'),
    ('v2.0', 'Security Analyst Reconstruction v2: Recovery Beta Decomposition + FRM + Portfolio Construction (6-S.12)');
