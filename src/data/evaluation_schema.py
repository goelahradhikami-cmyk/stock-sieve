"""
Evaluation Database Schema — DDL constants for all tables.

Extracted from evaluation_db.py to keep schema definitions separate from
CRUD logic and migration code.

Implements all 11 tables from evaluation_db_ddl_v2.sql:
  1. research_decisions      — SecurityAnalysis snapshots
  2. portfolio_decisions     — PortfolioDecision full trace
  3. evaluation_results      — T+N Counterfactual Alpha
  4. thesis_outcomes         — Pattern-level thesis performance
  5. calibration_log         — Confidence calibration tracking
  6. post_mortems            — Structured failure analysis
  7. agent_genome_snapshots  — Evolution lineage
  8. decision_events         — Complete audit trail
  9. factor_memory           — Factor performance by regime
  10. agent_performance      — AI fund manager NAV
  11. market_regime_snapshots — Market state archive
"""

# ═══════════════════════════════════════════════════════════
# DDL — All 11 tables
# ═══════════════════════════════════════════════════════════

DDL = """
-- §1 Research Decisions
CREATE TABLE IF NOT EXISTS research_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT NOT NULL,
    genome_hash         TEXT NOT NULL,
    security_id         TEXT NOT NULL,
    security_name       TEXT,
    thesis_id           TEXT NOT NULL,
    thesis_family       TEXT,
    thesis_pattern      TEXT,
    thesis_claim        TEXT NOT NULL,
    thesis_evidence     TEXT,
    thesis_mechanism    TEXT,
    thesis_catalyst     TEXT,
    thesis_invalidation TEXT NOT NULL,
    thesis_horizon      TEXT,
    alpha_score         REAL NOT NULL,
    confidence          REAL NOT NULL,
    factor_snapshot     TEXT NOT NULL,
    factor_snapshot_id  TEXT,
    risk_assessment     TEXT,
    decision_hash       TEXT NOT NULL UNIQUE,
    input_hash          TEXT NOT NULL,
    model_version       TEXT,
    entry_price         REAL,
    entry_date          DATE NOT NULL,
    status              TEXT DEFAULT 'active',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rd_agent_date ON research_decisions(agent_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_rd_security ON research_decisions(security_id);
CREATE INDEX IF NOT EXISTS idx_rd_thesis_pattern ON research_decisions(thesis_pattern);
CREATE INDEX IF NOT EXISTS idx_rd_status ON research_decisions(status);

-- §2 Portfolio Decisions
CREATE TABLE IF NOT EXISTS portfolio_decisions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    research_decision_id    INTEGER NOT NULL,
    policy_id               TEXT NOT NULL,
    agent_id                TEXT NOT NULL,
    base_weight             REAL NOT NULL,
    kelly_weight            REAL,
    regime_multiplier       REAL NOT NULL,
    risk_penalty            REAL,
    liquidity_penalty       REAL,
    valuation_gate_applied  INTEGER DEFAULT 0,
    final_weight            REAL NOT NULL,
    market_regime           TEXT,
    market_risk_score       REAL,
    cash_level              REAL,
    portfolio_herfindahl    REAL,
    sector_exposure_current REAL,
    decision_trace          TEXT,
    execution_instruction   TEXT DEFAULT 'normal',
    status                  TEXT DEFAULT 'active',
    decision_date           DATE NOT NULL,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (research_decision_id) REFERENCES research_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_pd_research ON portfolio_decisions(research_decision_id);
CREATE INDEX IF NOT EXISTS idx_pd_agent_date ON portfolio_decisions(agent_id, decision_date);
CREATE INDEX IF NOT EXISTS idx_pd_regime ON portfolio_decisions(market_regime);

-- §3 Evaluation Results
CREATE TABLE IF NOT EXISTS evaluation_results (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    research_decision_id    INTEGER NOT NULL,
    portfolio_decision_id   INTEGER,
    horizon_days            INTEGER NOT NULL,
    eval_date               DATE NOT NULL,
    stock_return            REAL,
    market_return           REAL,
    sector_return           REAL,
    agent_top10_ew_return   REAL,
    alpha_vs_market         REAL,
    alpha_vs_sector         REAL,
    alpha_vs_peer           REAL,
    max_drawdown_during     REAL,
    max_profit_during       REAL,
    is_profitable           INTEGER DEFAULT 0,
    alpha_positive          INTEGER DEFAULT 0,
    verdict                 TEXT,
    attribution_json        TEXT,
    evaluated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (research_decision_id) REFERENCES research_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_er_research ON evaluation_results(research_decision_id);
CREATE INDEX IF NOT EXISTS idx_er_horizon ON evaluation_results(horizon_days);
CREATE INDEX IF NOT EXISTS idx_er_verdict ON evaluation_results(verdict);

-- §4 Thesis Outcomes
CREATE TABLE IF NOT EXISTS thesis_outcomes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_pattern      TEXT NOT NULL,
    thesis_family       TEXT,
    agent_id            TEXT,
    evaluation_period   TEXT NOT NULL,
    sample_size         INTEGER NOT NULL,
    win_rate            REAL NOT NULL,
    avg_alpha_vs_market REAL,
    avg_alpha_vs_sector REAL,
    sharpe_contribution REAL,
    max_drawdown_avg    REAL,
    failure_modes       TEXT,
    confidence_adjustment REAL DEFAULT 0.0,
    weight_adjustment     REAL DEFAULT 0.0,
    last_updated        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_to_pattern ON thesis_outcomes(thesis_pattern);
CREATE INDEX IF NOT EXISTS idx_to_agent ON thesis_outcomes(agent_id);

-- §5 Calibration Log
CREATE TABLE IF NOT EXISTS calibration_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT NOT NULL,
    calibration_period  TEXT NOT NULL,
    confidence_bucket   TEXT NOT NULL,
    sample_size         INTEGER NOT NULL,
    predicted_success_rate REAL NOT NULL,
    actual_success_rate REAL NOT NULL,
    calibration_error   REAL NOT NULL,
    calibration_score   REAL,
    status              TEXT,
    penalty_applied     REAL DEFAULT 0.0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cl_agent_period ON calibration_log(agent_id, calibration_period);

-- §6 Post Mortems
CREATE TABLE IF NOT EXISTS post_mortems (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    research_decision_id    INTEGER NOT NULL,
    agent_id                TEXT NOT NULL,
    failure_date            DATE NOT NULL,
    error_type              TEXT NOT NULL,
    primary_cause           TEXT NOT NULL,
    wrong_assumption        TEXT,
    missed_signal           TEXT,
    ignored_factor          TEXT,
    regime_mismatch         TEXT,
    detail_analysis         TEXT,
    suggested_actions       TEXT,
    applied                 INTEGER DEFAULT 0,
    applied_to_genome_hash  TEXT,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (research_decision_id) REFERENCES research_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_pm_agent ON post_mortems(agent_id);
CREATE INDEX IF NOT EXISTS idx_pm_error_type ON post_mortems(error_type);
CREATE INDEX IF NOT EXISTS idx_pm_applied ON post_mortems(applied);

-- §7 Agent Genome Snapshots
CREATE TABLE IF NOT EXISTS agent_genome_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT NOT NULL,
    strategy_genus      TEXT NOT NULL,
    strategy_species    TEXT NOT NULL,
    generation          INTEGER NOT NULL,
    parent_agent_id     TEXT,
    parent_genome_hash  TEXT,
    mutation_reason     TEXT,
    mutation_detail     TEXT,
    genome_hash         TEXT NOT NULL UNIQUE,
    genome_yaml         TEXT NOT NULL,
    evaluation_score    REAL,
    calibration_score   REAL,
    birth_date          DATE NOT NULL,
    status              TEXT DEFAULT 'active',
    frozen_date         DATE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ags_agent ON agent_genome_snapshots(agent_id);
CREATE INDEX IF NOT EXISTS idx_ags_genus_gen ON agent_genome_snapshots(strategy_genus, generation);
CREATE INDEX IF NOT EXISTS idx_ags_parent ON agent_genome_snapshots(parent_agent_id);

-- §8 Decision Events
CREATE TABLE IF NOT EXISTS decision_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    reference_id        INTEGER,
    reference_type      TEXT,
    event_data          TEXT,
    event_summary       TEXT,
    event_timestamp     TIMESTAMP NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_de_agent_time ON decision_events(agent_id, event_timestamp);
CREATE INDEX IF NOT EXISTS idx_de_type ON decision_events(event_type);
CREATE INDEX IF NOT EXISTS idx_de_reference ON decision_events(reference_type, reference_id);

-- §9 Factor Memory
CREATE TABLE IF NOT EXISTS factor_memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_name     TEXT NOT NULL,
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    market_regime   TEXT,
    ic_mean         REAL,
    ic_std          REAL,
    ir              REAL,
    win_rate        REAL,
    top_quantile_return     REAL,
    bottom_quantile_return  REAL,
    spread                  REAL,
    universe        TEXT DEFAULT 'all',
    sample_size     INTEGER,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fm_factor_regime ON factor_memory(factor_name, market_regime);
CREATE INDEX IF NOT EXISTS idx_fm_period ON factor_memory(period_start, period_end);

-- §10 Agent Performance
CREATE TABLE IF NOT EXISTS agent_performance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    period_type     TEXT NOT NULL,
    period_start    DATE,
    period_end      DATE,
    total_return    REAL,
    annual_return   REAL,
    sharpe_ratio    REAL,
    sortino_ratio   REAL,
    max_drawdown    REAL,
    volatility      REAL,
    win_rate        REAL,
    hit_ratio       REAL,
    alpha_vs_market REAL,
    alpha_vs_sector REAL,
    information_ratio REAL,
    avg_num_positions   INTEGER,
    turnover_rate       REAL,
    avg_cash_level      REAL,
    personality_score   REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ap_agent_period ON agent_performance(agent_id, period_type, period_start);

-- §11 Market Regime Snapshots
CREATE TABLE IF NOT EXISTS market_regime_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_date            DATE NOT NULL UNIQUE,
    regime_type         TEXT,
    risk_score          REAL,
    growth_env_score    REAL,
    value_env_score     REAL,
    momentum_env_score  REAL,
    defensive_env_score REAL,
    liquidity_score     REAL,
    market_pe_percentile    REAL,
    market_pb_percentile    REAL,
    sector_valuation_json   TEXT,
    indicators_json     TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mrs_date ON market_regime_snapshots(obs_date);
CREATE INDEX IF NOT EXISTS idx_mrs_regime ON market_regime_snapshots(regime_type);
"""

# ═══════════════════════════════════════════════════════════
# DDL v2.1 — Incremental additions
# ═══════════════════════════════════════════════════════════

DDL_V21 = """
-- §13 Failure Patterns (v2.1)
CREATE TABLE IF NOT EXISTS failure_patterns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id          TEXT NOT NULL UNIQUE,
    pattern_name        TEXT NOT NULL,
    error_category      TEXT NOT NULL,
    error_subtype       TEXT,
    common_factors      TEXT,
    common_thesis_types TEXT,
    common_regimes      TEXT,
    occurrence_count    INTEGER DEFAULT 0,
    last_occurrence     DATE,
    avg_loss_magnitude  REAL,
    pattern_confidence  REAL DEFAULT 0.0,
    validated_by        TEXT DEFAULT 'rule_engine',
    preventive_action   TEXT,
    suggested_filter    TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fp_category ON failure_patterns(error_category);
CREATE INDEX IF NOT EXISTS idx_fp_confidence ON failure_patterns(pattern_confidence);

-- §14 Committee Decisions (v2.1)
CREATE TABLE IF NOT EXISTS committee_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    committee_id        TEXT NOT NULL UNIQUE,
    research_decision_id INTEGER NOT NULL,
    valuation_score     REAL,
    industry_score      REAL,
    risk_score          REAL,
    quant_score         REAL,
    chairman_score      REAL,
    verdict             TEXT NOT NULL,
    monitoring_flags    TEXT,
    debate_transcript   TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (research_decision_id) REFERENCES research_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_cd_research ON committee_decisions(research_decision_id);
CREATE INDEX IF NOT EXISTS idx_cd_verdict ON committee_decisions(verdict);

-- §15 Candidate Rules (v2.2)
-- NOTE: This table lives in the CENTRAL EvaluationDB only. It is a DISTINCT
-- store from `candidate_rules_v2` in src/postmortem/engine.py (the
-- PostMortemAnalyzer's own local postmortem DB). The two share only a name —
-- different schemas, different databases, no inheritance/versioning. Do NOT
-- merge them or treat candidate_rules_v2 as an upgrade of this table.
CREATE TABLE IF NOT EXISTS candidate_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name TEXT NOT NULL,
    description TEXT,
    source_pattern_id INTEGER,
    condition_json TEXT NOT NULL,
    action_type TEXT,
    status TEXT DEFAULT 'candidate',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_pattern_id) REFERENCES failure_patterns(id)
);
CREATE INDEX IF NOT EXISTS idx_cr_status ON candidate_rules(status);

-- §16 Thesis Patterns (v2.3)
CREATE TABLE IF NOT EXISTS thesis_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name TEXT NOT NULL,
    pattern_family TEXT,
    sample_size INTEGER DEFAULT 0,
    win_rate REAL,
    avg_alpha REAL,
    avg_drawdown REAL,
    market_regime TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tp_name ON thesis_patterns(pattern_name);

-- §17 Portfolio Execution (v2.4) — paper-trade order fills
CREATE TABLE IF NOT EXISTS portfolio_execution (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_decision_id   INTEGER NOT NULL,
    research_decision_id    INTEGER NOT NULL,
    agent_id                TEXT NOT NULL,
    security_id             TEXT NOT NULL,
    action                  TEXT NOT NULL,
    order_price             REAL,
    fill_price              REAL,
    quantity                INTEGER,
    slippage                REAL,
    commission              REAL,
    stamp_tax               REAL,
    transfer_fee            REAL,
    total_cost              REAL,
    execution_mode          TEXT DEFAULT 'PAPER',
    execution_status        TEXT DEFAULT 'filled',
    execution_date          DATE NOT NULL,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_decision_id) REFERENCES portfolio_decisions(id),
    FOREIGN KEY (research_decision_id) REFERENCES research_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_pe_portfolio ON portfolio_execution(portfolio_decision_id);
CREATE INDEX IF NOT EXISTS idx_pe_agent_date ON portfolio_execution(agent_id, execution_date);
CREATE INDEX IF NOT EXISTS idx_pe_security ON portfolio_execution(security_id);
"""
