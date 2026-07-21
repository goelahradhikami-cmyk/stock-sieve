# Stock Sieve 项目交接表

> **最新交接：** [PROJECT_HANDOVER_2026-07-19.md](PROJECT_HANDOVER_2026-07-19.md) - UI 终端主题改造（设计评审 / P0 已入库 cda3940 / P1 在未跟踪新页面 / P2 backlog）
> **v1.1 冻结增量（2026-07-21）：** Market Guardian v1.1 FROZEN，见下方「投资大脑（Investment Brain）」章节

**版本：** v0.1.0  
**日期：** 2026-07-15  
**总规模：** 81 文件 · 14,525 行 · 45 测试  

---

## 〇、投资大脑（Investment Brain）- 三层架构

> 2026-07-21 新增。这是 stock-sieve 在「AI 选股引擎」之上的核心架构升级。
> 冻结清单：`config/investment_brain_v1_freeze.yaml`（v1.1-frozen-defensive-core）
> 验证报告：`data/reports/market_guardian_validation_2026-07-21.md`

### 三层定位

```
Layer 1: Market Guardian ★★★★★ [v1.1 FROZEN 2026-07-21]
    ↓
Layer 2: Security Analyst ★☆☆☆☆ [未验证 - 等待 Reconstruction v2]
    ↓
Layer 3: Portfolio Construction ☆☆☆☆☆ [未验证]
```

**关键架构认知（v1.1）：Timing Intelligence ≠ Selection Intelligence**

Market Guardian 证明了「什么时候下注」可以独立产生价值。
「下注什么」（Security Analyst）现在是系统的瓶颈，是下一阶段的重点。

### Layer 1: Market Guardian（已冻结）

| 项目 | 值 |
|------|-----|
| **版本** | v1.1-frozen-defensive-core |
| **冻结日期** | 2026-07-21 |
| **验证基础** | 1204 episodes（2021-08-11 ~ 2026-07-10），10000 次 bootstrap |
| **职责** | 市场层风险防线（BUY/BLOCK 决策） |
| **核心代码** | `src/thesis/state_transition.py`（5 状态机）、`src/thesis/confidence_overlay.py`（置信度覆盖） |
| **决策记录** | `scripts/shadow_recorder.py` -> `data/shadow_trading.db` |
| **评估** | `scripts/shadow_outcome_evaluator.py`（T+20 反事实收益） |

**5 项冻结门槛（全 PASS）：**

| Gate | 描述 | 结果 |
|------|------|------|
| G1 | Regime Transfer（6-S.11.2） | ✅ Train 94.9% -> Test 94.4%，delta -0.6% |
| G2 | Bootstrap 显著性（6-S.11.3） | ✅ P(mean defensive_alpha ≤ 0) = 0.00% |
| G3 | Tail Risk Protection | ✅ 4/4 灾难性事件（cf<-10%）全避开，累计避免 +49.95% |
| G4-A1 | 灾难性 Timing（market ≤ -3%） | ✅ 8 个灾难性 false recovery，leak = 0 |
| G5 | 年度稳定性 | ✅ 所有年份 ≥ 89.14%（最低 2022 年） |

**G4 风险分层（重要方法论）：**

G4 按 risk tier 拆分，避免把选股层错误混入 timing 层评价：
- **G4-A1 灾难性 Timing**（冻结门槛）：market ≤ -3%，leak = 0 PASS
- **G4-A2 轻微 Timing 噪声**（诊断）：-3% < market < 0，3 个 leak（正常 recovery 波动，不阻塞）
- **G4-B 选股层**（诊断）：alpha < 0，3/133 = 2.3% leak，留给 Security Analyst Reconstruction v2

**诊断指标（不阻塞冻结，留给下一阶段）：**
- G4-A2: 3 个轻微 timing leak（-0.75% ~ -1.73% 小幅回调，非灾难性）
- G4-B: 3 个选股 leak，最严重 2025-08-13（alpha -12.36%，只选 1 只股票未分散）
- Def B: 18 个前向窗口 leak（CSI300 后续 20 日 < -3%），集中在 2022-07 假反弹

### Layer 2: Security Analyst（未验证）

| 项目 | 值 |
|------|-----|
| **状态** | 等待 Security Analyst Reconstruction v2 |
| **已知问题** | doctrine shuffle 失败（无增量 alpha）；G4-B 3/133 选股 leak；2025-08-13 只选 1 只股票 |
| **下一步方向** | (1) anomaly 真实性验证（relative strength / sector confirmation / earnings revision）<br>(2) recovery beta 分离（stock recovery - market recovery）<br>(3) 组合分散规则（min 5 只 / single position ≤ 25%） |

### Layer 3: Portfolio Construction（未验证）

| 项目 | 值 |
|------|-----|
| **状态** | Bayesian Allocation 已冻结但未实战验证 |
| **已知问题** | 缺少 min-position / max-concentration 规则 |

### Shadow Trading 系统（6-S.10.x 系列）

| Commit | 内容 | 文件 |
|--------|------|------|
| 6-S.10.0 | Investment Brain v1.0 冻结 | `config/investment_brain_v1_freeze.yaml` |
| 6-S.10.1 | Shadow Trading schema + 评估器 | `migrations/shadow_trading_schema_v1.sql`, `scripts/shadow_outcome_evaluator.py` |
| 6-S.10.2 | Shadow Episode Recorder | `scripts/shadow_recorder.py` |
| 6-S.11.2 | Regime Transfer 验证 | （对话分析，已持久化到 freeze YAML + 验证报告） |
| 6-S.11.3 | Bootstrap Validation + v1.1 冻结 | `scripts/run_bootstrap_validation.py`, `data/reports/market_guardian_validation_2026-07-21.md` |

**数据库：** `data/shadow_trading.db`（5 张表：shadow_episode / shadow_candidates / shadow_outcome / shadow_counterfactual / shadow_metrics）

**下一步路线：**
```
Market Guardian v1.1 FROZEN ✅ (当前)
    ↓
Security Analyst Reconstruction v2 (下一阶段重点)
    ↓
Evolution v4 (Selection 重建后启用)
```

> Evolution v4 原本以 shadow_trading_30_episodes 为门槛，现已远超（1204 episodes）。
> v1.1 重新以 Security Analyst Reconstruction 为门槛，因为继续优化 Market Guardian 的边际收益远低于重建选股层。

---

## 一、项目概述

Stock Sieve（筛股魔方）是一个多人格进化型AI选股引擎。核心创新点：
1. **8个投资人格**（深度价值/成长猎手/动量追逐/逆向/品质复利/红利贵族/内部人跟踪/量化极客）
2. **进化机制**（基于真实评估数据的自然选择、交叉繁殖、突变、沙盒验证）
3. **三层治理**（Thesis Validator → Investment Committee → Post-Mortem）
4. **全市场竞技场**（真实通达信本地数据驱动的多Agent排名）

---

## 二、协议栈（5 份 Frozen）

| # | 文件 | 位置 | 行数 | 内容 |
|---|------|------|------|------|
| 1 | `agent_contract_v1.1.md` | `docs/specs/` | 523 | 三角色架构(Research/Portfolio/Execution)、SecurityAnalysis规范、Thesis规范、6原则 |
| 2 | `personality_genome_schema_v3.2.yaml` | `docs/specs/` | 639 | Agent基因组：identity向量、doctrine、cognitive model、factor model、decision graph |
| 3 | `portfolio_policy_schema_v1.1.1.yaml` | 根目录 | 607 | 组合策略：conviction→position引擎、Half-Kelly、估值门控、4类回撤归因 |
| 4 | `evaluation_db_ddl_v2.sql` | 根目录 | 479 | 11张核心表DDL |
| 5 | `evolution_engine_spec_v1.yaml` | `docs/specs/` | 282 | 自然选择、4维绩效归因、淘汰/繁殖规则、沙盒验证、LLM边界 |

---

## 三、数据库（18 张表）

**主库：** `data/evaluation.db`（SQLite）
**缓存库：** `data/cache.db`（安全主数据、指数数据、过滤日志）

### 核心链路表

| 表 | 用途 | 关键字段 |
|----|------|----------|
| `research_decisions` | Research Agent 选股决策 | agent_id, security_id, thesis_* , alpha_score, confidence, factor_snapshot |
| `portfolio_decisions` | Portfolio Agent 仓位决策 | base_weight→kelly→regime→risk_penalty→final_weight 全链路 |
| `committee_decisions` | 委员会裁决 | 5角色评分, verdict, position_cap_modifier, monitoring_flags |
| `evaluation_results` | T+N 评估 | stock_return, alpha_vs_market/sector/peer, max_drawdown, verdict |
| `post_mortems` | 投资尸检 | error_type/subtype, wrong_assumption, missed_signal, suggested_actions |
| `agent_genome_snapshots` | Agent 基因组版本 | genome_yaml, parent_agent_id, generation, mutation_detail |

### 进化与学习表

| 表 | 用途 |
|----|------|
| `failure_patterns` | 失败模式库（用于Validator反证检查） |
| `candidate_rules` / `candidate_rules_v2` | 候选规则（**两张独立表**：`candidate_rules` 在 EvaluationDB、`candidate_rules_v2` 在 PostMortemAnalyzer 本地库；schema、库均不同，仅名字相近） |
| `post_mortems` | **EvaluationDB 规范表**：结构化失败分析，按 `research_decision_id` 关联，被 `memory/extractor` 与旧 `evolution/engine.py` 消费（含 `mutation_candidates` 列，由 `get_post_mortems_with_mutations` 使用） |
| `post_mortem_analysis` | **PostMortemEngine 独立表**（与 `post_mortems` 同名冲突故改名）：存 `PostMortemAnalyzer` 深度复盘结果 + `mutation_candidates`，按 `evaluation_id` 关联；`run_daily` 落库、`collect_recent_mutations` 读回喂 `EvolutionEngineV1.run_cycle`，闭合"失败→突变→进化" |
| `agent_learning_events` | Agent 学习事件（penalty/reward） |
| `evolution_events` | 进化事件日志（AGENT_BORN/FROZEN/REJECTED） |
| `sandbox_evaluation` | 沙盒验证记录（三层防护） |
| `evolution_arena_results` | 竞技场排名数据 |
| `thesis_outcomes` | Thesis模式长期验证 |
| `calibration_log` | Confidence校准记录 |
| `factor_memory` | 因子IC/IR按市场状态 |
| `agent_performance` | Agent净值与personality_score |

### 市场数据表

| 表 | 用途 |
|----|------|
| `security_master` | 9,256只全A股票主数据 |
| `market_regime_snapshots` | 市场状态分类(bull/bear/crisis/rotation) |
| `market_index_daily` | 指数日线（CSI 300/500/1000） |
| `universe_snapshot` | 每日可交易股票池快照 |
| `universe_filter_log` | 逐只过滤原因+阶段 |
| `tradable_universe` | 快速查询当天池子 |
| `trading_calendar` | 交易日历 |
| `signal_snapshot` | 选股信号快照 |

---

## 四、源码文件清单（按模块）

### 1. 数据层 `src/data/`（11 文件 · 2,782 行）

| 文件 | 行 | 职责 | 关键类/函数 |
|------|-----|------|------------|
| `provider.py` | 506 | 行情接口 | `DataProvider`(腾讯PE/PB/市值), `MarketDataProvider`(mootdx K线+快照) |
| `local_provider.py` | 98 | 通达信本地数据 | `LocalDataProvider`(读.day文件, 优先D:/new_tdx_mock, 1199根K线/股) |
| `financials.py` | 207 | 财报数据 | `FinancialDataProvider`(mootdx finance→ROE/净利率/FCF等) |
| `index_provider.py` | 127 | 指数数据 | `IndexDataProvider`(沪深300/500/1000, mootdx+腾讯回退) |
| `security_master.py` | 96 | 股票身份CRUD | `SecurityMaster`(upsert/get_active_universe/get_by_code, 9256只) |
| `universe.py` | 183 | 股票池同步 | `fetch_eastmoney_stock_list()`(东财API→102只内置回退), `sync_security_master()` |
| `universe_filter.py` | 226 | 过滤规则v2 | `UniverseFilter`(6阶段: active→ST→new→liquidity→dynamic→BJ, snapshot持久化) |
| `calendar.py` | 161 | 交易日历 | `TradingCalendar`(严格DB-only, is_trade_day/previous/next, seed_sample) |
| `market_brain.py` | 117 | 市场状态 | `MarketBrain`(bull/bear/crisis/rotation分类, classify/regime检测) |
| `evaluation_db.py` | 980 | 评估数据库 | `EvaluationDB`(init/migrate/insert/query 全套, DDL/DDL_V21常量, personality_score) |
| `__init__.py` | 4 | 导出 | DataProvider, MarketSnapshot, StockSnapshot, EvaluationDB 等 |

### 2. 因子引擎 `src/factors/`（2 文件 · 328 行）

| 文件 | 行 | 职责 |
|------|-----|------|
| `engine.py` | 326 | `FactorEngine`(6族31因子: value/quality/growth/momentum/risk/sentiment, compute_single_stock, 横截面标准化) |
| `__init__.py` | 2 | |

### 3. Agent 运行时 `src/agents/`（11 文件 · 1,954 行）

| 文件 | 行 | 职责 | 关键类 |
|------|-----|------|--------|
| `research_agent.py` | 434 | 研究员 | `ResearchAgent`(genome→thesis→SecurityAnalysis, 确定性, LLM不参与评分) |
| `portfolio_agent.py` | 391 | 基金经理 | `PortfolioAgent`(conviction_engine, 8步final_weight, Half-Kelly, 市场适配, 4类回撤归因) |
| `committee_agent.py` | 634 | 投资委员会 | `CommitteeAgent`(6角色评分→主席裁决, RuleOnlyLLMBridge, apply_committee_decision) |
| `__init__.py` | 18 | | |
| **五角色模块** `committee_roles/` | | | |
| `valuation_reviewer.py` | 58 | 估值审查 | PE分位/FCF收益/成长溢价 |
| `industry_reviewer.py` | 24 | 行业审查 | 行业动量/营收增速/催化剂 |
| `risk_controller.py` | 42 | 风控官 | 集中度/尾部风险/流动性 |
| `quant_auditor.py` | 62 | 量化审计 | Alpha持续性/IC/样本量 |
| `devil_advocate.py` | 83 | 魔鬼代言人 | 绝对断言/证据链/证伪条件可量化性 |
| `_common.py` | 82 | 公共工具 | normalize_severity, warning_matches, clamp |
| `__init__.py` | 36 | | |

### 4. 治理层（3 模块 · 2,974 行）

#### 4a. 验证 `src/validation/`（8 文件 · 1,022 行）

| 文件 | 行 | 职责 |
|------|-----|------|
| `thesis_validator.py` | 287 | `ThesisValidator`(证据/反证/历史/复杂性 4模块, routing_action: BLOCK/RESEARCH_ONLY/ALLOW_REDUCED_WEIGHT/ALLOW_COMMITTEE) |
| `evidence_checker.py` | 115 | 证据校验(支持 >X/<X/AND/OR) |
| `counter_evidence.py` | 99 | 反证风险评估(匹配failure_patterns) |
| `historical_pattern.py` | 100 | 历史模式分析(win_rate×0.4+alpha×0.3+risk_adj×0.2+sample×0.1) |
| `complexity_checker.py` | 136 | 复杂性检查(claims/causal_depth/dependencies/assumptions, 防叙事过拟合) |
| `rule_registry.py` | 156 | `RuleRegistry`(静态规则+动态规则生命周期) |
| `evidence_graph.py` | 91 | 因果链校验 |
| `__init__.py` | 10 | |

#### 4b. 评估 `src/evaluation/`（4 文件 · 1,505 行）

| 文件 | 行 | 职责 |
|------|-----|------|
| `post_mortem.py` | 719 | `PostMortemAnalyzer`(5类12子类错误分类决策树, 突变生成, LLM分析边界) |
| `evaluation_engine.py` | 387 | `EvaluationEngine`(T+N评估, 6项边界修复, Brinson归因, Beta Winsorize) |
| `batch_runner.py` | 342 | `BatchEvaluationRunner`(批量回填, run_pending, 真实K线前向收益, peer return) |
| `__init__.py` | 13 | |

#### 4c. 尸检 `src/postmortem/`（4 文件 · 447 行）

| 文件 | 行 | 职责 |
|------|-----|------|
| `engine.py` | 243 | `PostMortemEngine`(run_daily, 从evaluation_results消费, 生成candidate_rules_v2；并对每个失败评测调用 PostMortemAnalyzer 深度复盘、将结果+mutation_candidates 落 post_mortems 表，供 `collect_recent_mutations` 喂给进化引擎，闭合"失败→突变→进化"环路) |
| `classifier.py` | 120 | `FailureClassifier`(6类规则: market/stock/sector/timing_early/timing_late/execution) |
| `rule_miner.py` | 132 | `RuleMiner`(从failure_patterns自动挖掘高频模式→candidate_rules_v2，即 postmortem 本地库表，非 EvaluationDB 的 candidate_rules) |
| `__init__.py` | 4 | |

### 5. 进化引擎 `src/evolution/`（5 文件 · 1,741 行）

| 文件 | 行 | 职责 | 关键类/常量 |
|------|-----|------|------------|
| `engine.py` | 756 | 原始进化引擎 | `EvolutionEngine`, `SelectionEngine`, `MutationEngine`(4突变源), `CrossoverEngine`(α∈[0.3,0.7]), `SandboxValidator`, `SurvivalCriteria` |
| `engine_v1.py` | 342 | 生产级进化引擎 | `EvolutionEngineV1`(基于evaluation_results的fitness, 余弦距离多样性, 沙盒验证, dry-run) |
| `sandbox.py` | 302 | 沙盒验证v2 | `SandboxValidator`(三层防护: 最小交易笔数≥10, fitness改善≥5%, 回撤恶化≤1.2×) |
| `arena.py` | 335 | 进化竞技场 | `EvolutionArena`(多Agent同场竞赛, 真实交易模拟, 多维fitness排名), `PortfolioSimulator` |
| `__init__.py` | 13 | | |

### 6. UI `src/ui/`（11 文件 · 1,073 行）

| 文件 | 行 | 职责 |
|------|-----|------|
| `app.py` | 95 | Streamlit主入口(侧边栏+4Tab导航) |
| **页面** `views/` | | |
| `committee_room.py` | 65 | 🏛️委员会会议室(会议选择+评分卡+攻击点) |
| `leaderboard.py` | 118 | 🏆排行榜(排名表+雷达图+绩效趋势) |
| `thesis_tracker.py` | 63 | 🔍Thesis追踪器(股票搜索+决策链) |
| `genome_fusion.py` | 229 | 🧬人格融合(α滑块+identity预览+沙盒提交) |
| **组件** `components/` | | |
| `committee_card.py` | 115 | 委员会评分仪表盘(5角色条形图) |
| `agent_radar.py` | 50 | 8维雷达图(Plotly) |
| `decision_timeline.py` | 92 | 决策时间线(Research→Eval 5步) |
| **工具** `utils/` | | |
| `db_connector.py` | 22 | DB连接管理(@st.cache_resource) |
| `data_loader.py` | 223 | 数据加载+模拟数据(@st.cache_data) |

### 7. 工具层

| 文件 | 行 | 职责 |
|------|-----|------|
| `src/execution/simulator.py` | 98 | `ExecutionSimulator`(滑点+佣金+印花税+过户费) |
| `src/utils/report_exporter.py` | 387 | `ReportExporter`(委员会/绩效/Thesis/进化 × MD/XLSX) |
| `src/cli.py` | 243 | CLI(init/factor/screen/evolve/fuse/export/status) |
| `src/runner.py` | 343 | 单股/批量pipeline(数据→因子→Agent→委员会→评估→尸检) |
| `src/daily_run.py` | 174 | 每日自动运行(指数同步→Universe→Agent→评估→尸检) |
| `preview_child.py` | 216 | 子代预览实验室(真实K线对比父子代) |

### 8. 配置 `config/`（10 文件 · 828 行）

| 文件 | 行 | 内容 |
|------|-----|------|
| `personalities/value_purist.yaml` | 110 | 深度价值(valuation=90, patience=95) |
| `personalities/growth_hunter.yaml` | 104 | GARP成长猎手(growth=90) |
| `personalities/momentum_chaser.yaml` | 77 | 趋势跟随(momentum=95) |
| `personalities/contrarian.yaml` | 73 | 逆向投资(contrarian=95) |
| `personalities/quality_compounder.yaml` | 75 | 品质复利(quality=95) |
| `personalities/dividend_aristocrat.yaml` | 72 | 红利贵族(valuation=75) |
| `personalities/insider_follower.yaml` | 71 | 内部人跟踪(sentiment权重最高) |
| `personalities/quant_nerd.yaml` | 77 | 量化极客(均衡多因子) |
| `committee_protocol.yaml` | 113 | 委员会角色权重+裁决阈值+LLM配置 |
| `validation_rules.yaml` | 49 | 5条静态反证规则 |

### 9. 测试 `tests/`（6 文件 · 1,144 行 · 45 项）

| 文件 | 测试数 | 内容 |
|------|--------|------|
| `test_committee_agent.py` | 24 | 五角色评分+主席裁决+apply_decision+DB持久化 |
| `test_real_data.py` | 8 | K线+交易日历+执行模拟器 |
| `test_universe.py` | 6 | 股票列表+过滤规则+SecurityMaster CRUD |
| `test_postmortem.py` | 7 | 分类器+规则生成+生命周期 |
| `integration_test.py` | — | 全模块集成测试 |
| `conftest.py` | — | pytest配置 |

---

## 五、关键常量与阈值

### 进化引擎
```python
MIN_SAMPLES = 10          # 最小评估记录数
ELITE_FRACTION = 0.25     # 精英比例
BOTTOM_FRACTION = 0.20    # 淘汰比例
DIVERSITY_THRESHOLD = 0.3 # 余弦距离阈值（低于此值免淘汰）
SANDBOX_IMPROVEMENT = 0.05 # 沙盒改善要求 5%
SANDBOX_DAYS = 90         # 回测窗口 3个月
```

### 委员会
```python
FATAL_REJECT_THRESHOLD = 30  # 风控/魔鬼<30→否决
RETURN_THRESHOLD = 50        # ≥2维度<50→退回修改
APPROVE_THRESHOLD = 70       # 加权≥70→批准
CONDITIONAL_THRESHOLD = 60   # 加权≥60+弱项→有条件通过
MAX_REVISIONS = 1            # 最多修改1次
```

### Validator
```python
BLOCK_THRESHOLD = 40         # <40分→BLOCK
RESEARCH_ONLY_THRESHOLD = 50 # <50分→仅观察
REDUCED_WEIGHT_THRESHOLD = 70 # <70分→降低仓位
```

### 交易模拟
```python
STAMP_TAX_RATE = 0.001       # 0.1% 印花税（卖出）
COMMISSION_RATE = 0.0003     # 0.03% 佣金
MIN_COMMISSION = 5.0         # 最低5元
```

---

## 六、数据源

| 数据 | 来源 | 状态 |
|------|------|------|
| 实时行情(股价/PE/PB/市值) | 腾讯财经 HTTP | ✅ 可用 |
| K线历史(2021-2026) | D:/new_tdx_mock 通达信本地 | ✅ 9,256只, 1199根/股 |
| K线历史(旧) | C:/new_tdx 通达信本地 | ⚠️ 仅到2026-02 |
| 财报(ROE/营收/利润) | mootdx finance + Tencent | ⚠️ 部分字段为空 |
| 股票列表 | 东财API→102只内置回退 | ⚠️ 当前用内置(网络受限) |
| 交易日历 | akshare→seed_sample回退 | ⚠️ 简化版(周一到周五) |
| 指数日线 | mootdx(CSI500/1000) + Tencent(CSI300) | ⚠️ CSI300仅今日快照 |

---

## 七、启动命令

```bash
cd stock-sieve

# 初始化
python -m src.cli init

# 单股分析
python -m src.runner --code 600519

# 每日自动运行 (3只)
python -m src.daily_run -n 3

# 启动面板
streamlit run src/ui/app.py --server.port 8503

# 进化引擎 dry-run
python -c "from src.evolution.engine_v1 import EvolutionEngineV1; \
  EvolutionEngineV1(dry_run=True).run_cycle()"

# 竞技场锦标赛
python -c "from src.evolution.arena import EvolutionArena; \
  r = EvolutionArena().run_tournament(cycle_id=1, start_date='2024-06-01', end_date='2026-06-30'); \
  [print(f'#{x[\"rank\"]} {x[\"agent_id\"]} fit={x[\"fitness\"]:.3f}') for x in r['rankings']]"

# 全量测试
python -m pytest tests/ -v
```

---

## 八、当前状态

| 项目 | 状态 |
|------|------|
| 股票主数据 | ✅ 9,256只全A |
| 活跃Agent | 9个(6创始人+3子代) |
| 测试 | 45/45 全绿 |
| 面板 | 4 Tab可用 |
| CLI | 7条命令 |
| 真实K线 | 通达信本地2021-2026 |
| 网络依赖 | 无(离线可用) |
| 进化周期 | 已跑6周期, 产生3子代 |
| 竞技场 | 基于真实历史数据 |
| **Investment Brain** | **v1.1-frozen-defensive-core (2026-07-21)** |
| **Market Guardian (Layer 1)** | **✅ FROZEN v1.1 - 1204 episodes, 5/5 gates PASS** |
| **Security Analyst (Layer 2)** | ⚠️ 未验证 - 等待 Reconstruction v2 |
| **Portfolio Construction (Layer 3)** | ⚠️ 未验证 |
| **Evolution v4** | 🔒 disabled - gated on Security Analyst Reconstruction |
| **Shadow Trading** | 1204 episodes (2021-08-11 ~ 2026-07-10), BUY 150 / BLOCK 1054 |

---

## 九、依赖

```toml
[project]
requires-python = ">=3.10"
dependencies = [
    "mootdx>=0.10",      # 通达信行情+财报
    "pandas>=2.0",        # 数据处理
    "numpy>=1.24",        # 数值计算
    "streamlit>=1.28",    # Web面板
    "plotly>=5.18",       # 图表
    "pyyaml>=6.0",        # 配置文件
    "litellm>=1.0",       # LLM接口(可选)
]

[project.optional-dependencies]
dev = ["pytest>=7.0"]
```

**无网络依赖**：核心数据来自通达信本地 `.day` 文件，腾讯API仅用于PE/PB/市值快照（回退到本地缓存）。
