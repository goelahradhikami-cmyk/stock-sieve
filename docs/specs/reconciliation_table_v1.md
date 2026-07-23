# decision_reconciliation — 原子级决策对账表（v1.0）

## 1. 目的

stock-sieve 的决策链横向分片在 6 张表，全部以 `research_decision_id` 为锚，但从不缝合：

```
research_decisions → committee_decisions → portfolio_decisions
                   → portfolio_execution → evaluation_results  (evolution 只读其 fitness 窗口)
```

`decision_reconciliation` 把 6 阶段漏斗 + 漂移 + 三价交叉 + 扣成本净 alpha + 进化可见性，一次性缝合为**每个 `research_decision_id` 一行**的原子事实表。

- **原子级**：每个字段不可再分；每个阶段都有 `has_*`(0/1) + `*_missing_reason`（明确枚举）。
- **只读缝合**：只做 LEFT JOIN 与派生计算，**永远不改 6 张源表 schema**。
- **只标注不修复**：`evaluation_results.portfolio_decision_id` 历史上从不写入，本表只用 `eval_portfolio_link_broken` 标注该 bug，不动源数据。
- **进化只读**：复制 `engine_v1._calculate_fitness` 的可见性窗口（见 §5），不修改进化引擎。

## 2. 表结构（10 组字段）

PK = `research_decision_id`（一对一，无自增 id）。

| 组 | 字段 | 类型 | 说明 |
|----|------|------|------|
| A 锚点 | `research_decision_id` (PK, FK→research_decisions) | INTEGER | 主键 |
| | `decision_hash`, `agent_id`, `security_id`, `entry_date` | TEXT/DATE | 锚点元数据 |
| B 研究 | `rd_alpha_score`, `rd_confidence`, `rd_entry_price`, `rd_genome_hash` | REAL/TEXT | 研究快照（总是存在） |
| C 信号镜像 | `has_signal_snapshot`, `ss_alpha_score`, `ss_confidence`, `alpha_drift`, `confidence_drift`, `signal_snapshot_missing_reason` | INT/REAL/TEXT | 漂移检测 |
| D 委员会 | `has_committee`, `committee_verdict`, `committee_weighted_score`, `committee_position_cap`, `committee_missing_reason` | INT/REAL/TEXT | |
| E 组合 | `has_portfolio`, `portfolio_action`, `portfolio_final_weight`, `portfolio_base_weight`, `portfolio_kelly_weight`, `portfolio_missing_reason` | INT/TEXT/REAL | |
| F 执行 | `has_execution`, `exec_fill_price`, `exec_quantity`, `exec_total_cost`, `exec_slippage`, `price_slippage_vs_signal`, `execution_missing_reason` | INT/REAL/INT/TEXT | |
| G 评估 | `has_eval`, `eval_horizon_days`, `eval_stock_return`, `eval_alpha_vs_market`, `eval_verdict`, `eval_alpha_error`, `eval_missing_reason` | INT/INT/REAL/TEXT | |
| H 派生 | `net_alpha_after_cost`, `cost_drag_pct`, `pipeline_stage_reached`, `three_price_mismatch_flag`, `eval_portfolio_link_broken` | REAL/INT/INT | 核心原子指标 |
| I 进化 | `counted_in_fitness`, `fitness_invisible_reason` | INT/TEXT | 只读复制进化窗口 |
| J 元数据 | `anomaly_flags` (JSON 数组), `reconciled_at`, `reconciliation_version` (默认 `'1.0'`) | TEXT/TIMESTAMP/TEXT | |

索引：`(agent_id, entry_date)`、`(pipeline_stage_reached)`、`(has_eval)`、`(decision_hash)`。

## 3. `*_missing_reason` 枚举

| 字段 | 取值 |
|------|------|
| `signal_snapshot_missing_reason` | `NULL` / `'not_reached'` |
| `committee_missing_reason` | `NULL` / `'validator_block'` |
| `portfolio_missing_reason` | `NULL` / `'verdict_reject'` / `'no_decision'` / `'approved_but_no_portfolio'` |
| `execution_missing_reason` | `NULL` / `'no_portfolio'` / `'zero_lot'` |
| `eval_missing_reason` | `NULL` / `'pending_t_plus_n'` |
| `fitness_invisible_reason` | `NULL` / `'no_eval'` / `'out_of_window'` / `'cold_start'` |

判定逻辑（单一事实来源，集中在 `ReconciliationBuilder._assemble`）：

- 研究 `alpha_score < 4` 的决策**根本不会插入** `research_decisions`（daily_run 在插入前 `continue`），因此不出现在本表。
- 有 `research_decision_id` 但无 `committee_decisions` 行 → 只能是 validator `BLOCK` 在委员会插入前中断 → `'validator_block'`。
- 有委员会但 `verdict` 非 APPROVE → `'verdict_reject'`；有委员会且 APPROVE 但无 portfolio 行 → `'approved_but_no_portfolio'`；无委员会 → `'no_decision'`。
- 有 portfolio 但无 execution → 上游 `order_qty<=0` → `'zero_lot'`；无 portfolio → `'no_portfolio'`。
- 无 evaluation 行 → `'pending_t_plus_n'`（T+N 尚未到达或被 `run_pending` 跳过）。

## 4. 派生指标定义

- `alpha_drift` = `rd_alpha_score − ss_alpha_score`；`confidence_drift` = `rd_confidence − ss_confidence`（均无则 NULL）。
- `price_slippage_vs_signal` = `(exec_fill_price − rd_entry_price) / rd_entry_price`（缺失则 NULL）。
- `cost_drag_pct` = `exec_total_cost / (exec_fill_price × exec_quantity)`（仅 `has_execution=1` 且分母>0 时非 NULL）。
- `net_alpha_after_cost` = `eval_alpha_vs_market − cost_drag_pct`（**仅 `has_execution=1` 且 `has_eval=1` 时非 NULL**，此前从未计算）。
- `pipeline_stage_reached` (1–6)：各里程碑（Research/Signal/Committee/Portfolio/Execution/Eval）可达性的**最大值**。注意 **T+N 评估独立于委员会/组合**——被 BLOCK 的决策仍会被 `run_pending` 评估，因此其 `has_eval=1` 但 `has_committee=0`，`pipeline_stage_reached` 取 6。
- `three_price_mismatch_flag` = 1 当 `has_execution=1` 且 `|fill_price − rd_entry_price| / rd_entry_price > 1%`。

### 三价交叉的设计说明

`evaluation_results` **没有独立的入场价列**；评估引擎复用了 `research_decisions.entry_price`（`batch_runner._evaluate_single` 取 `row['entry_price']`）。同时 `signal_snapshot.entry_price` 在当前写入路径中恒为 NULL。因此三个价格点实际坍缩为两个独立来源：

- 信号价 = `research_decisions.entry_price`（`rd_entry_price`）
- 成交价 = `portfolio_execution.fill_price`（`exec_fill_price`）
- 评估入场价 = `research_decisions.entry_price`（**设计上等同于信号价**）

对账表以"信号价 vs 成交价"的偏差作为三价一致性检查，并明确记录"评估入场价=信号价"这一事实。

### 评估多 horizon 的聚合规则

`evaluation_results` 按 `(research_decision_id, horizon_days)` 可有**多行**（5/20/30/…/365）。本表是 1 rid = 1 行，因此：

- `has_eval = 1` 只要该 rid 有任意 evaluation 行。
- 所有标量评估字段（`eval_horizon_days` / `eval_stock_return` / `eval_alpha_vs_market` / `eval_verdict` / `eval_alpha_error`）取** horizon 最大且 `alpha_vs_market` 非 NULL** 的那一行（最成熟信号）；若全部 `alpha_vs_market` 为 NULL（如 `insufficient_data`），回退到任意一行。

### `eval_portfolio_link_broken`

`evaluation_results.portfolio_decision_id` 历史上从不写入（实测 220 行全为 NULL），属源表 bug。本表只**标注**不改：当 `has_eval=1` 且该列 NULL 时置 1。

## 5. 进化可见性（只读复制 `engine_v1`）

`counted_in_fitness = 1` 当且仅当：

1. `has_eval = 1`；
2. 该 evaluation 的 `eval_date >= now − 1 year`；
3. 该 agent 在 1 年窗口内的 evaluation 样本数 `>= 10`（`EVOLUTION_MIN_SAMPLES`，与 `engine_v1.MIN_SAMPLES` 对齐）。

否则 `fitness_invisible_reason` 取 `'no_eval'` / `'out_of_window'` / `'cold_start'`。**不修改进化引擎**。

## 6. `anomaly_flags` 标签

JSON 数组，可能包含：

| 标签 | 触发条件 |
|------|----------|
| `alpha_drift>1` | `|alpha_drift| > 1` |
| `confidence_drift>1` | `|confidence_drift| > 1` |
| `price_mismatch` | `three_price_mismatch_flag = 1` |
| `eval_portfolio_link_broken` | `eval_portfolio_link_broken = 1` |
| `portfolio_without_committee` | `has_portfolio=1` 但 `has_committee=0` |
| `committee_reject_but_traded` | 委员会 REJECT 类 verdict 却 `has_portfolio=1`（防御性） |
| `approved_but_no_portfolio` | 委员会 APPROVE 却无 portfolio 行 |
| `approved_but_not_executed` | APPROVE + portfolio 却 `has_execution=0` |
| `zero_lot` | `has_execution=1` 但 `exec_quantity` 为 0/NULL |
| `negative_net_alpha_after_cost` | `net_alpha_after_cost < 0` |

## 7. 刷新时机（双轨 + 全量回填）

| 轨 | 触发点 | 覆盖字段 | 失败处理 |
|----|--------|----------|----------|
| 同步轨 | `daily_run` 每个决策落库后（`try/finally`，覆盖 BLOCK/REJECT 全漏斗） | B–F | `try/except` 仅 `logger.warning`，绝不阻塞主管道 |
| 异步轨 | `evaluator.run_pending()` 后，按 `date(evaluated_at)=today` 取 rids 回填 | G–H、I | 同上 |
| 全量回填 | `reconcile --range` / `reconcile --decision` / `reconcile`（全部） | 全字段重建 | 只读，幂等 |

`upsert` 为 `INSERT OR REPLACE`，重复运行行数不变、内容稳定。

## 8. CLI

```
python -m src.cli reconcile                       # 全量重建
python -m src.cli reconcile --range 2026-01-01:2026-07-17
python -m src.cli reconcile --decision <id>       # 打印 wide 行 + anomaly_flags
python -m src.cli reconcile --funnel              # 各阶段独立计数 + pipeline_stage_reached 分布
```

`--funnel` 分别打印各阶段独立计数（暴露 345→65→1→0→220 这类非嵌套断崖），而非仅按 `pipeline_stage_reached` 分组——因为评估与委员会/组合并非严格嵌套。
