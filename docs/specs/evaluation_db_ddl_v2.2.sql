-- ============================================================
-- Stock Sieve — 数据模型升级 v2.2
-- 新增: security_master, trading_calendar, portfolio_execution
-- 变更: evaluation_results 增加多周期、Beta、Jensen Alpha 及冗余字段
-- ============================================================

-- 1. 股票主数据
CREATE TABLE IF NOT EXISTS security_master (
    security_id     TEXT PRIMARY KEY,          -- 如 '600519.SH'
    code            TEXT NOT NULL,             -- 纯数字代码 '600519'
    exchange        TEXT NOT NULL,             -- 'SH' / 'SZ' / 'BJ'
    name            TEXT NOT NULL,
    ipo_date        DATE,
    list_days       INTEGER DEFAULT 0,        -- 上市天数（便于过滤新股）
    status          TEXT DEFAULT 'active',     -- 'active','suspended','delisted'
    industry        TEXT,                     -- 中信一级行业
    industry_index  TEXT,                     -- 对应行业指数代码（如 '399673'）
    total_mv        REAL,                     -- 总市值（亿元）
    float_mv        REAL,                     -- 流通市值（亿元）
    avg_turnover_20d REAL,                   -- 20日均换手率(%)
    avg_amount_20d  REAL,                     -- 20日均成交额（万元）
    is_st           INTEGER DEFAULT 0,
    is_new_stock    INTEGER DEFAULT 0,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sm_code ON security_master(code);
CREATE INDEX IF NOT EXISTS idx_sm_industry ON security_master(industry);
CREATE INDEX IF NOT EXISTS idx_sm_status ON security_master(status);

-- 2. 交易日历
CREATE TABLE IF NOT EXISTS trading_calendar (
    trade_date  DATE PRIMARY KEY,
    is_trading  INTEGER DEFAULT 1,
    week_of_year INTEGER,
    month       INTEGER,
    quarter     INTEGER
);

-- 3. 交易执行记录（拆分执行层绩效）
CREATE TABLE IF NOT EXISTS portfolio_execution (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_decision_id INTEGER NOT NULL,
    security_id         TEXT NOT NULL,
    action              TEXT NOT NULL,        -- 'BUY','SELL','ADD','REDUCE'
    order_price         REAL,                 -- 下单价格
    fill_price          REAL,                 -- 实际成交均价
    quantity            REAL,                 -- 成交数量（股）
    slippage            REAL,                 -- 滑点（bps）
    commission          REAL,                 -- 手续费
    execution_date      DATE NOT NULL,
    execution_status    TEXT DEFAULT 'filled', -- 'filled','rejected','partial'
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (portfolio_decision_id) REFERENCES portfolio_decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_pex_decision ON portfolio_execution(portfolio_decision_id);
CREATE INDEX IF NOT EXISTS idx_pex_date ON portfolio_execution(execution_date);

-- 4. 评估结果升级（增量）
ALTER TABLE evaluation_results ADD COLUMN agent_id TEXT;
ALTER TABLE evaluation_results ADD COLUMN genome_version TEXT;
ALTER TABLE evaluation_results ADD COLUMN market_regime TEXT;
ALTER TABLE evaluation_results ADD COLUMN thesis_pattern TEXT;

-- 5. portfolio_execution 成本明细补充
ALTER TABLE portfolio_execution ADD COLUMN stamp_tax REAL DEFAULT 0.0;
ALTER TABLE portfolio_execution ADD COLUMN transfer_fee REAL DEFAULT 0.0;
ALTER TABLE portfolio_execution ADD COLUMN total_cost REAL DEFAULT 0.0;
ALTER TABLE portfolio_execution ADD COLUMN execution_mode TEXT DEFAULT 'PAPER';
ALTER TABLE evaluation_results ADD COLUMN beta REAL;
ALTER TABLE evaluation_results ADD COLUMN alpha_jensen REAL;

-- 新增归因表
CREATE TABLE IF NOT EXISTS evaluation_attribution (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id       INTEGER NOT NULL,
    factor_contribution_json TEXT,    -- 因子贡献
    market_contribution REAL,
    sector_contribution REAL,
    stock_alpha         REAL,
    FOREIGN KEY (evaluation_id) REFERENCES evaluation_results(id)
);
