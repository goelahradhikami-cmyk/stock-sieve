-- evaluation_db_ddl_v2.1.sql（增量，相对 v2.0）
-- Stock Sieve — 记忆中枢升级 v2.1
-- 新增：failure_patterns 表、committee_decisions 表
-- 扩展：post_mortems 表增加字段

-- ═══════════════════════════════════════════════════════════
-- §6 扩展：post_mortems 表新增字段
-- ═══════════════════════════════════════════════════════════

-- ALTER TABLE post_mortems ADD COLUMN error_subtype TEXT;
-- ALTER TABLE post_mortems ADD COLUMN rule_trigger TEXT;         -- JSON
-- ALTER TABLE post_mortems ADD COLUMN mutation_candidates TEXT;  -- JSON

-- Note: SQLite ALTER TABLE is limited. For fresh installs,
-- the full DDL in evaluation_db.py already includes these columns.
-- The migration below is for existing databases.

-- ═══════════════════════════════════════════════════════════
-- §13 新增：失败模式库
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS failure_patterns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id          TEXT NOT NULL UNIQUE,
    pattern_name        TEXT NOT NULL,
    error_category      TEXT NOT NULL,
    error_subtype       TEXT,

    -- 模式特征
    common_factors      TEXT,          -- JSON: 关联的因子信号
    common_thesis_types TEXT,          -- JSON: 关联的 thesis_pattern
    common_regimes      TEXT,          -- JSON: 多发市场状态

    -- 统计
    occurrence_count    INTEGER DEFAULT 0,
    last_occurrence     DATE,
    avg_loss_magnitude  REAL,

    -- 验证信息
    pattern_confidence  REAL DEFAULT 0.0,
    validated_by        TEXT DEFAULT 'rule_engine',

    -- 预防措施
    preventive_action   TEXT,
    suggested_filter    TEXT,

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fp_category ON failure_patterns(error_category);
CREATE INDEX IF NOT EXISTS idx_fp_confidence ON failure_patterns(pattern_confidence);

-- ═══════════════════════════════════════════════════════════
-- §14 新增：投资委员会决策记录
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS committee_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    committee_id        TEXT NOT NULL UNIQUE,
    research_decision_id INTEGER NOT NULL,

    -- 各委员评分
    valuation_score     REAL,
    industry_score      REAL,
    risk_score          REAL,
    quant_score         REAL,

    -- 主席裁定
    chairman_score      REAL,
    verdict             TEXT NOT NULL,  -- APPROVE / APPROVE_WITH_CONDITIONS / REJECT / RETURN_FOR_REVISION
    monitoring_flags    TEXT,           -- JSON: 需持续监控的指标

    -- LLM 生成的质询记录
    debate_transcript   TEXT,           -- JSON: 完整辩论过程

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (research_decision_id) REFERENCES research_decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_cd_research ON committee_decisions(research_decision_id);
CREATE INDEX IF NOT EXISTS idx_cd_verdict ON committee_decisions(verdict);

-- ═══════════════════════════════════════════════════════════
-- 版本控制
-- ═══════════════════════════════════════════════════════════
-- schema_version: 2.1.0
-- parent_version: 2.0.0
-- freeze_date: 2026-07-14
-- status: FROZEN
-- changelog:
--   v2.1.0: 新增 failure_patterns 表（失败模式库，支持 pattern-based learning）
--           新增 committee_decisions 表（投资委员会决策记录）
--           扩展 post_mortems 表（error_subtype, rule_trigger, mutation_candidates）
