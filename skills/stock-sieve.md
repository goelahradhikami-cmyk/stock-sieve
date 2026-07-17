# /stock-sieve — Stock Sieve AI 投资机构

## Description

与 Stock Sieve（筛股魔方）交互——一个多人格进化型 AI 选股引擎。
通过此 Skill 触发选股、启动投资委员会辩论、查看 Agent 排行榜、
管理 Agent 进化、导出报告。

## 系统架构

```
Research Agent → Thesis Validator → Investment Committee → Portfolio Agent
       ↑                                                    │
       └────────── Evolution Engine ←── Post-Mortem ────────┘
```

## Commands

### 选股
触发指定人格对全市场扫描，返回 Top N 推荐及 Thesis。

```
/stock-sieve screen <personality> [--top N] [--market <all|csi300|csi500>]
```

示例：
- `/stock-sieve screen value_purist --top 10` — 用深度价值人格选股
- `/stock-sieve screen growth_hunter --top 20 --market csi300` — 成长猎手在沪深300选股

可选 personality：`value_purist`, `growth_hunter`, `momentum_chaser`, `contrarian`, `quality_compounder`, `dividend_aristocrat`, `insider_follower`, `quant_nerd`

### 投资委员会辩论
对指定股票启动一次完整的投资委员会辩论（五角色评分 → 主席裁决）。

```
/stock-sieve debate <stock_code>
```

示例：
- `/stock-sieve debate 600519` — 对贵州茅台启动委员会辩论
- `/stock-sieve debate 300308` — 对中际旭创启动委员会辩论

辩论结果可通过 Streamlit 面板查看：`streamlit run src/ui/app.py`

### 排行榜
显示当前所有活跃 Agent 的信誉排名和绩效指标。

```
/stock-sieve leaderboard [--top N]
```

### Agent 进化
手动触发 Agent 的进化流程（需满足最低寿命、最少决策数等条件）。

```
/stock-sieve evolve [--agent <id>] [--simulate]
```

- `--agent <id>`: 指定要进化的 Agent（默认：所有满足条件的 Agent）
- `--simulate`: 使用模拟数据运行（仅测试用）

### 人格融合
混合两个 Agent 的基因组，生成候选子代。

```
/stock-sieve fuse --parent-a <id> --parent-b <id> [--alpha <0.3-0.7>]
```

- `--alpha`: 融合比例（默认随机在0.3-0.7范围内采样）
- 生成的候选子代需通过沙盒验证后才能激活

### 决策链查询
查看某只股票的完整决策历史——从 Research Thesis 到 T+N 评估。

```
/stock-sieve validate <stock_code> [--days 90]
```

示例：
- `/stock-sieve validate 600519` — 查看茅台最近90天的决策链

### 系统状态
显示 Stock Sieve 当前运行状态。

```
/stock-sieve status
```

输出内容：
- 活跃 Agent 数量 / 总数
- 当前市场状态 (MarketBrain 分类)
- 今日委员会会议数量
- 最近进化事件
- 协议栈版本号

### 报告导出
导出委员会决策、Agent 绩效、Thesis 追踪等数据。

```
/stock-sieve export --type <committee|performance|thesis|evolution> --format <md|xlsx> [--output <path>]
```

示例：
- `/stock-sieve export --type committee --format md` — 导出委员会报告为 Markdown
- `/stock-sieve export --type performance --format xlsx --output reports/july.xlsx`

## Constraints

- **绝不直接修改基因组 YAML** — 所有变更必须通过 Evolution Engine 的命令接口，经沙盒验证后执行。
- **绝不绕过投资委员会** — 任何选股结果必须经过 Validator → Committee 治理流程才能进入组合。
- **绝不伪造评分** — LLM 只能生成解释文本，不得参与任何评分（alpha_score, confidence, committee scores 等）。
- **展示数据前必须验证时效性** — 如果数据超过缓存 TTL，必须明确告知用户数据可能不是最新的。

## Backend

所有命令通过 `src/cli.py` 执行，核心逻辑分布在以下模块：
- `src/agents/research_agent.py` — 选股
- `src/agents/committee_agent.py` — 委员会辩论
- `src/evolution/engine.py` — 进化与融合
- `src/evaluation/post_mortem.py` — 尸检分析
- `src/utils/report_exporter.py` — 报告导出

## 快速开始

```bash
cd stock-sieve
pip install -e .
python -m src.cli init
streamlit run src/ui/app.py
```
