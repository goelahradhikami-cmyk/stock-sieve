-- Shadow Candidates v3 Table - Commit 6-S.13.6
--
-- Independent audit trail for v3 CandidateGenerator outputs.
-- Separated from shadow_candidates (v1/v2) because v3 uses a completely
-- different funnel (Recovery -> RS -> Mispricing) and produces a candidate
-- pool with 0 overlap with v1/v2. Mixing them would corrupt attribution.

CREATE TABLE IF NOT EXISTS shadow_candidates_v3 (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id              TEXT NOT NULL,
    trade_date              DATE NOT NULL,
    security_id             TEXT NOT NULL,
    funnel_rank             INTEGER,        -- rank within v3 funnel (1=top)

    -- Stage 1: Recovery Eligibility
    frm_direction           TEXT,           -- improving/stable/deteriorating
    frm_score               REAL,           -- 0-100
    earnings_acceleration   REAL,           -- earnings_yoy_current - previous
    recovery_score          REAL,           -- Stage 1 composite 0-100
    liquidity_pass          INTEGER,        -- 1/0
    volume_ratio            REAL,

    -- Stage 2: Relative Strength
    relative_strength       REAL,           -- stock_return - sector_return
    sector_strength         REAL,           -- sector_return - market_return
    rs_score                REAL,           -- Stage 2 composite 0-100
    rs_data_available       INTEGER,        -- 1 if sector data available (post-2024-06)

    -- Stage 3: Mispricing
    divergence_score        REAL,           -- from anomaly detector
    price_drawdown_12m      REAL,
    market_pessimism        REAL,
    business_strength       REAL,

    -- Attribution (filled by backfill_candidate_attribution.py --source v3)
    stock_return_t20        REAL,
    market_return_t20       REAL,
    sector_return_t20       REAL,
    sector_code             TEXT,
    market_beta             REAL,           -- stock_return - market_return
    sector_beta             REAL,           -- stock_return - sector_return
    residual_alpha          REAL,           -- stock_return - market_return - sector_return

    -- Group tag for A/B comparison
    ab_group                TEXT,           -- 'v2_anomaly' / 'v3_recovery' / 'v3_recovery_rs'

    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (episode_id) REFERENCES shadow_episode(episode_id)
);

CREATE INDEX IF NOT EXISTS idx_scv3_episode ON shadow_candidates_v3(episode_id);
CREATE INDEX IF NOT EXISTS idx_scv3_date ON shadow_candidates_v3(trade_date);
CREATE INDEX IF NOT EXISTS idx_scv3_group ON shadow_candidates_v3(ab_group);
CREATE INDEX IF NOT EXISTS idx_scv3_rank ON shadow_candidates_v3(episode_id, funnel_rank);

INSERT OR REPLACE INTO schema_version (version, description) VALUES
    ('v3.1', 'shadow_candidates_v3 table for v3 CandidateGenerator audit trail (6-S.13.6)');
