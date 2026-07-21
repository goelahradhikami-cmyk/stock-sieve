-- Shadow Trading Database Schema v1.0
-- Commit 6-S.10.1
--
-- This DB records Investment Brain decisions for long-term validation.
-- It is NOT a trading log. It is a Decision Recording System.
--
-- Key principle: every BLOCK records what WOULD have been bought (counterfactual).
-- This allows answering: "did the Brain avoid trash, or miss opportunities?"

-- ═════════════════════════════════════════════════════════
-- 1. shadow_episode: core decision record
-- ═════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS shadow_episode (
    episode_id          TEXT PRIMARY KEY,           -- e.g. "E20260801"
    trade_date          DATE NOT NULL,
    
    -- Decision Snapshot
    market_state        TEXT NOT NULL,              -- PANIC/STABILIZING/EARLY_RECOVERY/CONFIRMED_RECOVERY/EUPHORIA
    confidence          REAL NOT NULL,              -- 0-100
    confidence_band     TEXT NOT NULL,              -- blocked/small/normal/full
    decision            TEXT NOT NULL,              -- BUY / BLOCK
    position_target     REAL DEFAULT 0.0,           -- 0-1.0 (from confidence band)
    
    -- Market features at decision time
    vol_20d             REAL,
    vol_change          REAL,
    trend_ma60          REAL,
    breadth             REAL,
    recovery_prob       REAL,
    
    -- Reason codes (why this decision)
    reason_codes        TEXT DEFAULT '[]',          -- JSON array: ["FALSE_RECOVERY_RISK", ...]
    
    -- Doctrine explanations (frozen, explanation only)
    quality_explanation TEXT,
    contrarian_explanation TEXT,
    value_explanation   TEXT,
    
    -- Metadata
    brain_version       TEXT DEFAULT '1.0-defensive-core',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- Status
    status              TEXT DEFAULT 'pending'      -- pending / evaluated / archived
);

CREATE INDEX IF NOT EXISTS idx_se_date ON shadow_episode(trade_date);
CREATE INDEX IF NOT EXISTS idx_se_state ON shadow_episode(market_state);
CREATE INDEX IF NOT EXISTS idx_se_decision ON shadow_episode(decision);
CREATE INDEX IF NOT EXISTS idx_se_status ON shadow_episode(status);

-- ═════════════════════════════════════════════════════════
-- 2. shadow_candidates: what the Brain WOULD have bought
-- ═════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS shadow_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id          TEXT NOT NULL,
    stock_code          TEXT NOT NULL,
    
    -- Anomaly features at selection time
    anomaly_type        TEXT,
    price_drawdown_12m  REAL,
    roe                 REAL,
    margin_change       REAL,
    market_pessimism    REAL,
    business_strength   REAL,
    divergence_score    REAL,
    confidence          REAL,
    
    -- Kill criteria result
    killed              INTEGER DEFAULT 0,
    kill_reason         TEXT,
    
    -- Doctrine verdicts (explanation only)
    quality_verdict     TEXT,
    contrarian_verdict  TEXT,
    value_verdict       TEXT,
    
    -- Was this stock selected for the shadow portfolio?
    selected            INTEGER DEFAULT 0,
    
    -- Per-stock outcome (filled at T+20)
    stock_return_t20    REAL,
    
    FOREIGN KEY (episode_id) REFERENCES shadow_episode(episode_id)
);

CREATE INDEX IF NOT EXISTS idx_sc_episode ON shadow_candidates(episode_id);
CREATE INDEX IF NOT EXISTS idx_sc_code ON shadow_candidates(stock_code);
CREATE INDEX IF NOT EXISTS idx_sc_selected ON shadow_candidates(selected);

-- ═════════════════════════════════════════════════════════
-- 3. shadow_outcome: T+20 / T+60 results
-- ═════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS shadow_outcome (
    episode_id          TEXT PRIMARY KEY,
    
    -- Portfolio outcome (if BUY)
    portfolio_return_t20    REAL,                   -- T+20 equal-weight return
    portfolio_return_t60    REAL,                   -- T+60 (filled later)
    portfolio_max_drawdown  REAL,                   -- max DD during holding
    
    -- Market benchmarks
    market_return_t20       REAL,                   -- CSI 300 return
    market_return_t60       REAL,
    csi_all_return_t20      REAL,                   -- CSI All Share
    equal_weight_return_t20 REAL,                   -- equal-weight universe
    
    -- Alpha calculations
    alpha_vs_hs300          REAL,
    alpha_vs_csiall         REAL,
    alpha_vs_equal          REAL,
    
    -- Decision quality
    win                     INTEGER,                -- 1 if portfolio_return > 0
    alpha_positive          INTEGER,                -- 1 if alpha > 0
    failure_type            TEXT,                   -- FALSE_RECOVERY / WRONG_STOCK / TIMING_ERROR / none
    
    evaluated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (episode_id) REFERENCES shadow_episode(episode_id)
);

-- ═════════════════════════════════════════════════════════
-- 4. shadow_counterfactual: "what if we had bought?"
-- ═════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS shadow_counterfactual (
    episode_id          TEXT PRIMARY KEY,
    
    -- If Brain said BLOCK, what would have happened if we bought?
    counterfactual_return    REAL,                  -- hypothetical portfolio return
    counterfactual_alpha     REAL,                  -- vs market
    
    -- Classification
    avoided_loss             REAL,                  -- positive = Brain was right to block
    missed_gain              REAL,                  -- positive = Brain missed opportunity
    
    -- Verdict
    block_quality            TEXT,                  -- CORRECT_BLOCK / INCORRECT_BLOCK / UNKNOWN
    
    evaluated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (episode_id) REFERENCES shadow_episode(episode_id)
);

-- ═════════════════════════════════════════════════════════
-- 5. shadow_metrics: rolling summary (updated per episode)
-- ═════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS shadow_metrics (
    metric_date         DATE PRIMARY KEY,
    
    -- Episode counts
    total_episodes      INTEGER DEFAULT 0,
    buy_episodes        INTEGER DEFAULT 0,
    block_episodes      INTEGER DEFAULT 0,
    
    -- False recovery tracking
    false_recovery_blocked  INTEGER DEFAULT 0,
    false_recovery_leaked   INTEGER DEFAULT 0,
    
    -- Alpha tracking (rolling)
    rolling_alpha_median    REAL,
    rolling_alpha_p5        REAL,
    rolling_alpha_p95       REAL,
    rolling_p_negative      REAL,
    
    -- Avoidance tracking
    avg_avoided_loss        REAL,
    avg_missed_gain         REAL,
    block_accuracy          REAL,                   -- correct_blocks / total_blocks
    
    -- Market coverage
    bull_episodes           INTEGER DEFAULT 0,
    bear_episodes           INTEGER DEFAULT 0,
    sideway_episodes        INTEGER DEFAULT 0,
    recovery_episodes       INTEGER DEFAULT 0,
    
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
