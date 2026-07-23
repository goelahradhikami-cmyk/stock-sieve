"""
Stock Factor Snapshot Schema - Commit 6-L.6 data infrastructure.

The stock_factor_snapshot table precomputes daily factor scores for the entire
universe (~5328 stocks) so that backtesting a child agent only requires
reweighting (a SQL join), not recomputing factors from scratch (100x speedup).

补丁4: includes both absolute scores AND market percentiles, because many
investment philosophies use ranking/percentile rather than absolute scores.
"""

DDL_STOCK_FACTOR_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS stock_factor_snapshot (
    trade_date    DATE NOT NULL,
    security_id   TEXT NOT NULL,
    -- 6 family absolute scores (cross-sectionally normalized, 0-100)
    quality_score REAL,
    value_score REAL,
    growth_score REAL,
    momentum_score REAL,
    risk_score REAL,
    sentiment_score REAL,
    -- 补丁4: 6 family market percentiles (0.0-1.0)
    -- Many doctrines use ranking, not absolute scores
    quality_percentile REAL,
    value_percentile REAL,
    growth_percentile REAL,
    momentum_percentile REAL,
    risk_percentile REAL,
    sentiment_percentile REAL,
    -- 31 raw factor values as JSON (for thesis pattern matching)
    factor_values_json TEXT,
    PRIMARY KEY (trade_date, security_id)
);
CREATE INDEX IF NOT EXISTS idx_sfs_date ON stock_factor_snapshot(trade_date);
CREATE INDEX IF NOT EXISTS idx_sfs_date_quality ON stock_factor_snapshot(trade_date, quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_sfs_date_value ON stock_factor_snapshot(trade_date, value_score DESC);
CREATE INDEX IF NOT EXISTS idx_sfs_date_growth ON stock_factor_snapshot(trade_date, growth_score DESC);
CREATE INDEX IF NOT EXISTS idx_sfs_date_momentum ON stock_factor_snapshot(trade_date, momentum_score DESC);
"""
