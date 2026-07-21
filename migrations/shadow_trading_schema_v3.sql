-- Shadow Trading Database Schema v3.0
-- Commit 6-S.13.1 (Security Analyst v3 Design Freeze)
--
-- This migration adds the v3 Candidate Generator funnel infrastructure:
--   1. shadow_funnel_log: records every stock's fate through the 3-stage funnel
--   2. shadow_candidates v3 fields: v3 stage scores persisted
--   3. security_candidate_universe_snapshot: universe entering Stage 1
--
-- The funnel reverses v1/v2 logic:
--   v1/v2: Universe -> Anomaly (entrance) -> Ranking
--   v3:    Universe -> Recovery Eligibility -> Relative Strength -> Mispricing (last gate)

-- ═══════════════════════════════════════════════════════════
-- 1. shadow_funnel_log: full funnel audit trail (6-S.13)
-- ═══════════════════════════════════════════════════════════
--
-- shadow_candidates only stores Stage 3 survivors. This table captures
-- the full picture: which stocks entered, which stage rejected them, why.

CREATE TABLE IF NOT EXISTS shadow_funnel_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id              TEXT NOT NULL,
    trade_date              DATE NOT NULL,
    stock_code              TEXT NOT NULL,

    -- Stage 1: Recovery Eligibility
    stage1_liquidity_pass   INTEGER,        -- 1/0/NULL
    stage1_volume_ratio     REAL,           -- the liquidity metric used
    stage1_frm_direction    TEXT,           -- improving/stable/deteriorating
    stage1_frm_score        REAL,           -- 0-100
    stage1_earnings_accel   REAL,           -- earnings_yoy_current - previous
    stage1_recovery_score   REAL,           -- composite 0-100
    stage1_pass             INTEGER,        -- 1/0

    -- Stage 2: Relative Strength
    stage2_rs_vs_sector     REAL,           -- stock_return - sector_return
    stage2_sector_vs_market REAL,           -- sector_return - market_return
    stage2_rs_score         REAL,           -- composite 0-100
    stage2_data_available   INTEGER,        -- 1 if sector data available (post-2024-06)
    stage2_pass             INTEGER,        -- 1/0/NULL (NULL if Stage 1 failed)

    -- Stage 3: Mispricing (anomaly, last gate)
    stage3_divergence_score REAL,
    stage3_pass             INTEGER,        -- 1/0/NULL

    -- Final outcome
    final_pass              INTEGER,        -- 1 if all three stages pass
    rejection_stage         TEXT,           -- NULL / 'stage1' / 'stage2' / 'stage3'
    rejection_reason        TEXT,           -- LOW_LIQUIDITY / DETERIORATING / WEAK_RS / NO_MISPRICING

    evaluated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (episode_id) REFERENCES shadow_episode(episode_id)
);

CREATE INDEX IF NOT EXISTS idx_sfl_episode ON shadow_funnel_log(episode_id);
CREATE INDEX IF NOT EXISTS idx_sfl_date ON shadow_funnel_log(trade_date);
CREATE INDEX IF NOT EXISTS idx_sfl_stage ON shadow_funnel_log(final_pass, rejection_stage);
CREATE INDEX IF NOT EXISTS idx_sfl_reason ON shadow_funnel_log(rejection_reason);

-- ═══════════════════════════════════════════════════════════
-- 2. shadow_candidates: v3 stage scores (6-S.13)
-- ═══════════════════════════════════════════════════════════

ALTER TABLE shadow_candidates ADD COLUMN v3_recovery_score REAL;
-- Stage 1 composite score (0-100). NULL for pre-v3 candidates.

ALTER TABLE shadow_candidates ADD COLUMN v3_rs_score REAL;
-- Stage 2 relative strength composite score (0-100).

ALTER TABLE shadow_candidates ADD COLUMN v3_candidate_stage TEXT;
-- Which stage this candidate reached: 'stage3_pass' for survivors.

ALTER TABLE shadow_candidates ADD COLUMN v3_funnel_rank INTEGER;
-- Rank within the v3 funnel (1 = top candidate by divergence_score).

-- ═══════════════════════════════════════════════════════════
-- 3. security_candidate_universe_snapshot (6-S.13)
-- ═══════════════════════════════════════════════════════════
--
-- Snapshot of the universe that entered Stage 1 for each trade_date.
-- Lets us answer: "why didn't stock X enter the v3 candidate pool?"
-- (vs shadow_candidates which only has survivors)

CREATE TABLE IF NOT EXISTS security_candidate_universe_snapshot (
    trade_date      DATE NOT NULL,
    security_id     TEXT NOT NULL,
    source          TEXT DEFAULT 'stock_factor_snapshot',
    liquidity_pass  INTEGER,        -- 1/0
    volume_ratio    REAL,
    eligible        INTEGER,        -- 1 if passed Stage 1 liquidity
    reject_reason   TEXT,           -- NULL / 'LOW_LIQUIDITY' / 'NOT_IN_SNAPSHOT'
    PRIMARY KEY (trade_date, security_id)
);

CREATE INDEX IF NOT EXISTS idx_scus_date ON security_candidate_universe_snapshot(trade_date);
CREATE INDEX IF NOT EXISTS idx_scus_eligible ON security_candidate_universe_snapshot(trade_date, eligible);

-- ═══════════════════════════════════════════════════════════
-- 4. Schema version marker
-- ═══════════════════════════════════════════════════════════

INSERT OR REPLACE INTO schema_version (version, description) VALUES
    ('v3.0', 'Security Analyst v3 Candidate Generator: funnel log + v3 fields + universe snapshot (6-S.13)');
