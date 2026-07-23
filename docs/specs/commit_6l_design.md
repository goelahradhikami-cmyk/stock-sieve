# Commit 6-L: Investment Personality Expression Engine

> Stock Sieve 从"参数进化系统"升级为"投资文明进化系统"的分水岭。

## 核心问题

之前:8 个 agent 的 identity 向量(8 维性格)和 factor_model 权重手工保持一致,但**没有机器强制派生**。进化时杂交只混合 identity,不混合 factor_model(抄父代 A),导致"进化的是标签,不是能力"。

## 架构(五层进化)

```
Identity Genome
      ↓
Doctrine Genome (可独立繁殖:parent_doctrine_id + mutation_history)
      ↓
Expression Genome (Factor DNA + Thesis DNA + Confidence DNA)
      ↓
Portfolio Genome -> Risk Genome -> Market Ecology -> Committee -> Natural Selection
```

## 实施内容

### Phase 2: 表达层

#### 6-L.1 Doctrine Engine (`src/agents/doctrine_engine.py`)
- 12 个 Genesis 原型(`config/doctrine_archetypes.yaml`)
- `DoctrineEngine.classify(identity)` - 最近邻 + 连续插值
- `DoctrineEngine.crossover(a, b)` - Doctrine 直接繁殖(补丁1)
- `DoctrineEngine.mutate(d)` - 加权 mutation distance(补丁3)
- `DoctrineRegistry` - 生命周期管理 archetype|generated|extinct(补丁2)
- `doctrine_genome` 表 - 可进化的 doctrine DNA

#### 6-L.2 Factor Expression
`DoctrineGenome.factor_bias` 替换 `_compute_alpha` 的 `factor_weights`。v2 模式下权重从 identity 派生,不读 YAML。

#### 6-L.3 Legacy Bridge (v1/v2 双轨)
- Feature flag: `research_engine_version: v2_identity_driven`
- v1:读 YAML factor_model(可复现)
- v2:DoctrineEngine 派生(进化)
- 8 个 YAML 升级(加 `derived: true`)

#### 6-L.4 Thesis Genome
`DoctrineGenome.thesis_priority` 替换 `thesis_scoring`。不同 doctrine 偏好不同 thesis pattern。

#### 6-L.5 Confidence Genome + 校准层
`DoctrineGenome.confidence_model` 替换固定公式。价值派看 ROE/毛利率,动量派看 momentum_3m/成交量。`confidence_calibration` 表(补丁5)。

### Phase 1: 数据基建

#### 6-L.6 前置
- `finance_snapshots` 扩展:持久化 PE/PB/mcap/float_mcap/turnover_pct
- `backfill_financials.py`:5328 股批量回填
- `stock_factor_snapshot` 表:6 个 score + 6 个 percentile(补丁4)
- `FactorSnapshotBuilder`:批量因子计算 + 横截面标准化

### Phase 3: 真实回测

#### 6-L.6 Evolution Arena v2
- 单 Agent 真回测:替换 `sandbox._generate_child_evaluations` 的假回测
  - 用 doctrine.factor_bias 对 `stock_factor_snapshot` 打分(SQL join)
  - 选 Top N -> 算真实 K 线 forward return
  - 无快照数据时降级到合成回测
- Multi-Agent 竞争 Arena(`competitive_arena.py`,补丁6)
  - 多 doctrine 同时选股
  - 拥挤度 / 重叠率 / alpha 衰减
  - 连接 6-K.1 Market Ecology

## 7 条审查补丁

| # | 补丁 | 实现 |
|---|------|------|
| 1 | Doctrine Genome 进化池 | `doctrine_genome` 表 + crossover/mutate 直接繁殖 |
| 2 | Archetype 非天花板 | DoctrineRegistry 管理 archetype\|generated\|extinct |
| 3 | 加权 Mutation Distance | `sqrt(Σ(cost_i × delta_i²))`,patience cost=2.0 |
| 4 | Factor Percentile | `stock_factor_snapshot` 6 个 percentile 列 |
| 5 | Confidence Calibration | `confidence_calibration` 表 |
| 6 | Multi-Agent Arena | `competitive_arena.py` + 拥挤度/alpha衰减 |
| 7 | Phase 2 顺序 | Doctrine->Factor->Bridge->Thesis->Confidence |

## 验证结果

- ✅ v1 可复现:旧逻辑结果不变
- ✅ v2 identity->doctrine->factor_bias->alpha 连锁
- ✅ 杂交子代 factor_bias 介于双亲之间(不再抄父代A)
- ✅ 不同 doctrine 同一只股打分不同(茅台:value=5.30, growth=4.65, momentum=3.60)
- ✅ confidence 人格化(动量派看 momentum,价值派看 ROE)
- ✅ Doctrine 直接繁殖(parent_doctrine_id + generation)
- ✅ 加权 mutation(patience cost 是 valuation 的 1.414x)
- ✅ 快照构建:不同 doctrine 选出不同 Top 股
- ✅ 沙盒双轨:真回测 + 合成降级
- ✅ Multi-Agent Arena:拥挤度计算
- ✅ 25 个测试全绿

## 约束

**不进 Commit 7 实盘。先跑通 Evolution Arena v2,验证核心假设:**
> 不同投资人格,经过长期进化后,是否真的会自然分化出不同 Alpha 来源。

## 文件清单

### 新建(11)
- `src/agents/doctrine_engine.py` - Doctrine Engine + Registry
- `src/factors/snapshot_builder.py` - 因子快照构建器
- `src/factors/snapshot_schema.py` - 快照表 DDL
- `src/evolution/competitive_arena.py` - Multi-Agent 竞争 Arena
- `config/doctrine_archetypes.yaml` - 12 个 Genesis 原型
- `scripts/backfill_financials.py` - 5328 股回填
- `scripts/build_factor_snapshots.py` - 快照构建 CLI
- `tests/test_doctrine_engine.py` - 25 个测试
- `docs/specs/commit_6l_design.md` - 本文档

### 修改(8)
- `src/agents/research_agent.py` - v1/v2 双轨
- `src/evolution/sandbox.py` - 真回测替换假回测
- `src/data/financials.py` - PE/PB/mcap 持久化
- `src/data/evaluation_migration.py` - migrate_v2_6_doctrine
- `config/personalities/*.yaml` ×8 - v2 升级
