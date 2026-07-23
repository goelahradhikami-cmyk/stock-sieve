# Stock Sieve 技术债整改清单

> 创建：2026-07-23，基于全库静态分析（142 源文件 / 32,210 行，20 测试文件 / 3,267 行）。
> 本文档记录「已完成整改」与「待办 backlog」两部分。

---

## 一、已完成（2026-07-23）

### 第一轮：工程卫生 + 基线清理

| 项 | 内容 |
|---|---|
| 根目录清理 | 删除 `_baostock_run.log` / `_run.log` / `_smoke1.log`；`baostock_growth_verification.html` 移入 `data/reports/` |
| 调试脚本归位 | `preview_child.py` 移入 `scripts/`，并修正其 `evaluation.db` 相对路径（原为脚本同目录，现指向项目根） |
| 死测试文件 | 删除 `tests/integration_test.py.bak` |
| 真实 bug 修复 | B006 可变默认参数 ×2：`src/evaluation/batch_runner.py`、`src/evolution/arena.py`（`horizons` 列表默认值改为 `None` 哨兵） |
| Lint 基线清理 | `ruff check --fix` 安全修复 241 处：未使用 import ×85、`Optional[X]`→`X\|None` ×116、import 排序 ×17、f-string 无占位符 ×5 等 |

### 第二轮：全量修理（v4.0 冻结兼容）

| 项 | 内容 |
|---|---|
| 静默吞错消除 | 53 处 `except Exception: pass` 全部改为日志记录（31 个文件）。默认 `logger.warning`；幂等迁移/连接回退/UI 尽力而为路径用 `logger.debug`（`evaluation_migration.py`、`db.py`、`batch_runner.py`、`baostock_provider.py`、`ui/`）。无残留静默失败点 |
| F841 未使用变量 | 17 处全部处理：删除纯赋值死代码；Streamlit 控件保留调用仅去赋值（`st.button`/`st.columns` 渲染行为不变）；含冻结核心 `state_transition.py` 的 `amounts` 死变量（纯推导，无副作用） |
| zip() 显式 strict | 27 处 `zip()` 全部补 `strict=False`（src 21 + scripts 6）——与原隐式行为**完全一致**，不触碰冻结语义 |
| 风格规则清零 | SIM102/103/105/108、E701/E702、W293、B007 全部修复（注释保留并迁移到合并后的语句上方） |
| 裸 except | `scripts/run_thesis_reality_check_v2.py` 2 处 `except:` → `except Exception:` |
| 全库格式化 | `ruff format` 应用于 src/ + tests/ + scripts/（165 文件重排版，AST 等价） |
| **src/ 零错误** | `ruff check src/` 与 `ruff format --check src/` 全部通过 |
| CI 门禁升级 | `ruff check src/` 与 `ruff format --check src/` 转为**硬失败**（不再 continue-on-error）；mypy 保持 informational |
| 工具链对齐 | pre-commit 的 ruff `v0.5.7` → `v0.15.22`，与本次基线及 CI 一致 |

### 第三轮：进化引擎双轨合并（2026-07-23，先测试后重构）

> Anti-Rule-Creep 评估：纯代码结构重构——不新增规则、不新增进化能力、不修改任何
> 冻结协议行为。属 Phase 4 允许的 "bug fix / observability" 范畴。

| 项 | 内容 |
|---|---|
| 先补回归测试 | 新增 `tests/test_evolution_v1_regression.py`（8 个特征化测试）：固定 fitness 公式数值、MIN_SAMPLES 门控、冷启动保护、dry-run 零状态变更、余弦多样性、交叉插值不变量、变异权重钳制——定义「行为不变」的判据 |
| 抽取 genome.py | `AgentGenome` / `PerformanceRecord` / `MutationCandidate` / `SelectionResult` 四个数据类从 engine.py 移至 `src/evolution/genome.py`（单一事实来源）；spec_engine 再导出保持兼容 |
| 命名歧义消除 | `engine.py` → **`spec_engine.py`**（协议级季度机制 `EvolutionEngine`）；`engine_v1.py` 保留（生产级每日引擎 `EvolutionEngineV1`）；`evolution/__init__.py` 增加模块地图注释；两个引擎文件 docstring 互相指认 |
| 导入方更新 | `research_agent.py` 改从 `genome` 导入；`test_integration.py` 3 处改指 `spec_engine` |
| 顺带修复 | 恢复 `test_integration.py` 中 14 个被以往 F401 自动修复清空的 import 冒烟测试（补 assert 防止再次被清空） |
| 验证 | 重构前基线 168 passed → 重构后 **168 passed**，完全一致；`ruff check src/` 零错误；冻结兼容：未触碰选择/变异/沙盒/冻结任何逻辑，仅移动代码位置 |

### 第四轮：三竞技场职责澄清（2026-07-23，先测试后重构）

> 职责核实结论：三者**并非重复实现**——competitive_arena 测拥挤度、survival_arena 做归因生存选择、
> arena 做锦标赛排名，但命名都含 "arena" 造成歧义。调用核实：`survival_arena` 被 3 个脚本
> 活跃使用；`arena`/`competitive_arena` 零代码调用（仅交接文档示例）。

| 项 | 内容 |
|---|---|
| 先补特征化测试 | 新增 `tests/test_evolution_arenas.py`（10 个测试）：固定锦标赛 fitness 排名公式、genome 评分公式、月度交易日生成（12 个月上限）、Jaccard 重叠率、因子偏向选股排序、拥挤度指标（stock_crowding/max_crowding/avg_overlap）、T+20 日历偏移、fitness_history 聚合、8 基身份向量形状 |
| 重命名消歧 | `arena.py` → **`tournament.py`**（锦标赛排名）；`competitive_arena.py` → **`crowding_arena.py`**（单日拥挤度度量）；`survival_arena.py` 保留（生产活跃）；类名均不变 |
| 文档同步 | `evolution/__init__.py` 模块地图补三竞技场职责；两个重命名模块 docstring 互相指认 + 标注与 `competition.py`（6-N.2 竞争矩阵）的边界；`PROJECT_HANDOVER.md` 两处引用更新 |
| 验证 | 重构后 **178 passed**（168 + 10 新），零回归；`ruff check src/` 零错误；未触碰任何 tournament/crowding/survival 逻辑，仅移动与重命名 |

### 验证

- `compileall` 全量通过（src + scripts）
- pytest **160 passed / 1 skipped / 2 failed**——与改动前完全一致；2 个失败均为 `test_real_data.py` 依赖 mootdx 实时行情库（环境依赖，非改动引入）
- 冻结兼容原则：所有修复均为行为等价变换（日志增强、死代码删除、`strict=False`、AST 等价格式化）；未修改任何 `config/*.yaml` 冻结协议与冻结门槛逻辑

---

## 二、待办 backlog（按优先级）

### P1 — 测试与质量门禁

1. **测试覆盖不足**：测试/源码行数比约 10%，`evolution/`、`thesis/` 两个最核心包几乎无直接测试。建议优先为 `evolution/engine_v1.py`、`thesis/state_transition.py`（已冻结的 Market Guardian 核心）补回归测试。
2. ~~CI 门禁收紧~~、~~静默吞错~~、~~F841~~：**已完成**（见上方第二轮）。lint 与 format 已转硬门禁，剩余路线仅剩 mypy 按包逐步取消 `check_untyped_defs = False`。
3. **tests/ 与 scripts/ 残余风格项**：约 37 处（F841/B007/E741/SIM115 等）遗留在一次性脚本与测试中，不影响 `src/` 零错误基线与 CI 门禁，可随用随清。

### P1 — 模块冗余/演进残留

4. ~~进化引擎双轨并存~~：**已完成**（见上方第三轮）——`genome.py` 抽取 + `engine.py`→`spec_engine.py` 重命名，职责边界：genome.py=数据类 / spec_engine.py=协议级季度机制 / engine_v1.py=生产级每日引擎。
5. ~~三竞技场并存~~：**已完成**（见上方第四轮）——职责核实为「锦标赛 / 拥挤度 / 生存选择」三者分工，`arena.py`→`tournament.py`、`competitive_arena.py`→`crowding_arena.py` 重命名消歧，`survival_arena.py` 保留。遗留：`tournament.py` 与 `crowding_arena.py` 目前零代码调用，若长期无调用方可在下一轮评估归档。
6. ~~数据层四文件边界~~：**已核实，无需重构**（2026-07-23）——四文件已是清晰的 Facade + Mixin 结构：`evaluation_schema.py`=DDL 常量（391 行）、`evaluation_crud.py`=CRUD Mixin（663 行）、`evaluation_migration.py`=迁移 Mixin（424 行）、`evaluation_db.py`=门面（101 行，仅 `__init__`/`init_db`/`connect` + 再导出）。CRUD 与 Migration 两个 Mixin 零方法名冲突，全库 14 处导入均经门面路径，`test_evaluation_db_connections.py` + `test_db_connection_governance.py` 10 个测试覆盖有效。

### P2 — 工程卫生

7. **仓库根不独立**：git 根在父目录 `ZCodeProject`，stock-sieve 与多个兄弟项目混在一个 repo，且大量源文件未被 git 跟踪（`git status` 显示成片 `??`）。建议拆分为独立仓库，或至少将 `src/`、`config/` 全部纳入版本控制。
8. **scripts/ 归档**：46 个一次性脚本 / 14,536 行（约为源码 45%）。已冻结阶段的验证脚本建议移入 `scripts/archive/` 或按 milestone 分子目录。
9. **print/logger 混用**：`src/` 内 222 处 `print()`，业务模块建议统一走 `src.utils.logger`。
10. ~~pre-commit 与 CI 的 ruff 版本对齐~~：**已完成**——pre-commit 升级至 `v0.15.22`，与基线一致。

---

## 三、整改原则（建议）

- **冻结层不动**：凡 `PROJECT_HANDOVER.md` 标记 Frozen 的协议与 `config/investment_brain_v1_freeze.yaml`，代码重构不得改变其行为语义，重构前后用 `scripts/run_*_validation.py` 回归。
- **门禁只收不放**：CI 硬门禁规则只增不减，新代码不得引入新 lint 错误。
- **先测试后重构**：P1 第 4-6 项的模块合并，必须先补对应包的回归测试（P1 第 1 项）。
