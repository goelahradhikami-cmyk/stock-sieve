# Stock Sieve（筛股魔方）

一个以 **择时纪律** 为核心的 A 股量化研究系统：在市场层面用状态机 + 置信度模型决定「什么时候可以买」，在此之上叠加多性格进化选股引擎决定「买什么」。

目前最成熟、经过严格验证的是 **Market Guardian（市场择时防线）**——它已在 5 年历史数据上证明能系统性避开灾难性回撤，现已冻结并投入每日影子运行。

## 核心亮点：Market Guardian v1.1（已冻结）

Guardian 回答一个问题：**"当前市场状态下注，值不值？"** 它不看个股，只看市场，输出 BUY / BLOCK 决策。

验证基础：**1204 个影子 episode（2021-08-11 ~ 2026-07-10）、10000 次 bootstrap**，五项冻结门槛全部通过：

| 门槛 | 内容 | 结果 |
|------|------|------|
| G1 Regime Transfer | 训练集 → 测试集迁移稳定性 | 94.9% → 94.4%（delta −0.6%） |
| G2 Bootstrap 显著性 | 防御性 alpha 统计显著性 | P(mean defensive_alpha ≤ 0) = 0.00% |
| G3 尾部风险保护 | 灾难性事件（反事实回撤 < −10%） | **4/4 全部避开，累计避免 +49.95% 回撤** |
| G4-A1 灾难性 Timing | 大盘单日 ≤ −3% 时的误放行 | leak = 0 |
| G5 年度稳定性 | 各年份表现一致性 | 所有年份 ≥ 89.14% |

核心机制：

- **5 状态机**（`src/thesis/state_transition.py`）：识别市场所处阶段（如 PANIC / STABILIZING / …）
- **置信度覆盖**（`src/thesis/confidence_overlay.py`）：`置信度 = 0.1 × 广度修复 + 0.5 × 波动率修复 + 0.4 × 趋势确认`，按置信度分档放行仓位（<30 禁止 / 30-50 小仓 / 50-55 正常 / ≥55 全仓）
- **影子记录与复盘**（`scripts/shadow_recorder.py` → `data/shadow_trading.db`）：每个交易日记录决策，T+20 后由 `scripts/shadow_outcome_evaluator.py` 做反事实收益评估
- **月度纪律审计**（`scripts/monthly_belief_audit.py`）：检查实际运行是否偏离冻结宪法

## 三层架构（投资大脑）

```
Layer 1: Market Guardian    ★★★★★  [v1.1 FROZEN — 已验证，每日运行]
Layer 2: Security Analyst   ★☆☆☆☆  [未验证 — 等待 Reconstruction v2]
Layer 3: Portfolio Construction ☆☆☆☆☆  [未验证]
         │
         └── Evolution Engine（多性格基因组进化）
```

> 关键架构认知：**Timing Intelligence ≠ Selection Intelligence**。
> Guardian 证明了「什么时候下注」可以独立产生价值；「下注什么」（Layer 2）是当前系统的瓶颈和下一阶段重点。

## 快速开始

要求：Python ≥ 3.10。

```bash
git clone https://github.com/goelahradhikami-cmyk/stock-sieve.git
cd stock-sieve
pip install -e .
stock-sieve init        # 初始化数据库
stock-sieve --help      # 查看全部命令
```

开发环境（含测试与静态检查，版本与 CI 对齐）：

```bash
pip install -e ".[dev]"
pytest                  # 运行测试
ruff check . && mypy    # lint + 类型检查（CI 硬门槛）
```

## 数据准备（重要）

择时与选股管道依赖 A 股日线数据，来源为通达信格式的本地 `.day` 文件：

```bash
# 通过 mootdx 从券商服务器增量下载/更新全市场日线（约 5200 只，首次较慢）
python scripts/update_vipdoc_from_mootdx.py

# 基于本地日线构建因子快照
python scripts/build_factor_snapshots.py
```

指数数据（沪深300 / 中证500 / 中证1000）由 `src/data/index_provider.py` 同步（内置腾讯行情 HTTP 回退）。

## 每日影子管道

```bash
# 1. 更新日线数据
python scripts/update_vipdoc_from_mootdx.py

# 2. 构建当日因子快照
python scripts/build_factor_snapshots.py

# 3. 记录当日择时 episode（BUY/BLOCK + 置信度 + 理由码）
python scripts/shadow_recorder.py --date 2026-08-07

# 4. T+20 后评估历史决策的反事实收益
python scripts/shadow_outcome_evaluator.py

# 每月 1 日：对上月做纪律审计
python scripts/monthly_belief_audit.py --month 2026-07
```

## CLI 一览

| 命令 | 用途 |
|------|------|
| `stock-sieve init` | 初始化数据库与系统 |
| `stock-sieve factor 600519` | 计算个股因子 |
| `stock-sieve screen -p value_purist -n 10` | 用指定性格筛股 |
| `stock-sieve evolve [--simulate]` | 运行进化周期 |
| `stock-sieve fuse -a <idA> -b <idB>` | 融合两个 agent 基因组 |
| `stock-sieve export -t committee\|performance\|thesis\|evolution` | 导出报告（md / xlsx） |
| `stock-sieve status` | 系统状态 |
| `stock-sieve reconcile --funnel` | 决策对账（6 表 → 1 行/决策） |

## 协议栈（冻结规范）

| # | 文件 | 状态 |
|---|------|------|
| 1 | `docs/specs/agent_contract_v1.1.md` | ✅ Frozen |
| 2 | `docs/specs/personality_genome_schema_v3.2.yaml` | ✅ Frozen |
| 3 | `portfolio_policy_schema_v1.1.1.yaml` | ✅ Frozen |
| 4 | `evaluation_db_ddl_v2.sql` | ✅ Frozen |
| 5 | `docs/specs/evolution_engine_spec_v1.yaml` | ✅ Frozen |

冻结清单：`config/investment_brain_v1_freeze.yaml`（v1.1-frozen-defensive-core）
验证报告：`data/reports/market_guardian_validation_2026-07-21.md`

## 项目状态与路线图

- ✅ **Phase 4 影子管道**：每日自动运行（数据更新 → 因子快照 → episode 记录），带缺失自动补跑
- ✅ **Market Guardian v1.1**：冻结，纪律审计逐月 PASS
- 🚧 **Security Analyst（Layer 2）**：选股层重建中，是当前研究重点
- ⬜ **Portfolio Construction（Layer 3）**：未开始

更详细的内部交接与设计决策见 `PROJECT_HANDOVER.md` 与 `docs/`。

## 许可

[MIT](LICENSE) — 可自由使用、修改、分发（包括商业用途），保留版权声明即可。

> ⚠️ 本项目为研究性质，所有输出不构成投资建议；据此操作，风险自负。
