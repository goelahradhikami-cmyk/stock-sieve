# Market Guardian 解冻候选问题清单

> **性质说明**：本文档不是 freeze entry，不是新规则，不构成对 Guardian v1.1 冻结状态的任何变更。
> 它是 2026-07-23 特征化测试工程（REMEDIATION.md 第五、六轮）中记录的**已知问题与行为观察**，
> 供未来 Guardian 解冻评审时使用。每条都已被测试钉住现状，改动任何一条都会触发测试失败——
> 这正是设计意图：解冻时的行为变更必须是显式决策，不能悄悄发生。
>
> 相关测试：`tests/test_state_transition.py`（23 个）、`tests/test_confidence_overlay.py`（19 个）

---

## P1 — 解冻时应处理

### T-1. Confidence Overlay 模块 docstring 描述的是旧版公式

- **位置**：`src/thesis/confidence_overlay.py` 文件头 docstring（第 15-21 行）
- **现象**：docstring 写 `Confidence = 0.4 × breadth + 0.3 × vol + 0.3 × trend`，阈值为 30/50/70；
  实际代码是 6-S.5.5b 版：`0.10 × breadth + 0.50 × vol_repair + 0.40 × trend`，阈值 55/65/75。
  代码内注释（第 116-123 行）记录了 6-S.5.5b 的调整理由，但模块头文档没有同步。
- **证据**：`tests/test_confidence_overlay.py::TestCompositeAndBands` 钉住的是代码行为（0.10/0.50/0.40、55/65/75）
- **影响**：任何只读 docstring 的维护者会对 Guardian 的放行逻辑产生**完全错误**的理解——
  权重主从颠倒（breadth 从 40% 降到 10%，vol_repair 从 30% 升到 50%），阈值整体上移。
- **建议**：解冻时更新 docstring 至 6-S.5.5b 口径，或解冻窗口内作为纯文档修正单独提交。

### T-2. 两个 Guardian 核心组件对「因子快照表缺失」的容错不一致

- **位置**：
  - `src/thesis/confidence_overlay.py::_compute_breadth_recovery`（try/finally，**无 except**）
  - `src/thesis/state_transition.py::_get_breadth`（try/except，回退 0.5）
- **现象**：`stock_factor_snapshot` 表不存在时——
  - confidence 层抛 `sqlite3.OperationalError`，`compute()` 直接失败，当日无置信度输出；
  - state 层捕获异常，以 breadth=0.5（中性）继续运行。
- **证据**：`tests/test_confidence_overlay.py::TestBreadthRecovery::test_missing_table_raises_operational_error`
- **影响**：同一数据故障下，Guardian 一半静默降级、一半直接崩溃。若快照表在重建窗口被删，
  confidence 层会成为单点故障；而 state 层的静默降级又可能掩盖数据缺失。
- **建议**：解冻时统一策略。两个候选方向：
  1. 都显式失败（fail-fast）——数据缺失是运维事故，不该静默继续；
  2. 都降级但**上报**（日志 + 状态位），保留可观测性。
  当前状态（一崩一默）是最差组合。

---

## P2 — 行为观察（不一定是缺陷，解冻时应知情）

### O-1. 无因子快照时，状态机降级是逐级的，PANIC 会迟到

- **位置**：`src/thesis/state_transition.py::_classify_day` + `_get_breadth`
- **现象**：无快照日 breadth=0.5（> 0.35）。崩盘首日 vol_20d 尚未越过 0.25（20 日窗口含 19 天平静数据），
  且 breadth=0.5 不满足 PANIC 第二条件（vol 扩张 + breadth < 0.40），
  因此首日目标态是 STABILIZING——状态机**先降级到 STABILIZING**，
  待 vol_20d 累积超过 0.25 后才到 PANIC。
- **证据**：`tests/test_state_transition.py::TestTransitionRules::test_downgrade_is_immediate`
- **影响**：在「快照缺失 + 市场崩盘」组合场景下，Guardian 的风险防线到达 PANIC 会比直觉晚几天。
  这与 G4-A1「灾难性 Timing leak = 0」的冻结成绩基于的历史数据（快照齐全）不同，
  属于数据降级场景下的未验证区域。
- **建议**：解冻评审时确认这是可接受的保守行为，还是需要为「快照缺失」定义独立的降级路径。

### O-2. `_get_breadth` 每个交易日新建一次 sqlite 连接

- **位置**：`src/thesis/state_transition.py::_get_breadth`（第 268-283 行）
- **现象**：`run()` 处理约 1200 个交易日时，`_get_breadth` 被逐日调用，
  每次都 `sqlite3.connect` + 查询 + `close`。功能正确，但全量历史重建时有可感知的开销。
- **影响**：性能问题，非正确性问题。
- **建议**：解冻时可改为单连接复用或批量预取 breadth 序列。

### O-3. 快照表缺失时 state 层逐日刷 warning 日志

- **位置**：`src/thesis/state_transition.py::_get_breadth`（2026-07-23 第二轮整改后）
- **现象**：静默吞错改为日志告警后，若快照表不存在，全量历史 run() 会逐日输出
  `no such table: stock_factor_snapshot` warning（约 1200 条）。
- **影响**：日志噪音。这是整改「消除静默失败」的副作用——故障现在可见了，但重复可见。
- **建议**：解冻时可对同一故障原因做去重（首日 warning + 后续 debug），或配合 T-2 统一处理。

---

## 处理原则（解冻时）

1. 修改上述任何行为前，先更新对应的特征化测试——测试钉住的是**现状**，
   解冻决策落地后测试应钉住**新现状**。
2. T-1（文档）与 T-2（容错策略）可以独立处理；O-1 需要 Guardian 验证管线回归
   （`scripts/run_bootstrap_validation.py` 等）确认不影响冻结门槛口径。
3. 本文档不随 v1.1 冻结；问题解决后直接从清单移除对应条目。
