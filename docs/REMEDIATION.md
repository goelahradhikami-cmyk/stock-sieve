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

### 第五轮：Market Guardian 核心特征化测试（2026-07-23，observability）

> 原则：特征化测试只**记录**冻结引擎当前行为，不规定应然行为。降级路径测试中初版断言
> 与实测不符（首日降级至 STABILIZING 而非 PANIC），经核实符合既定分类规则后，按**实测行为**固定断言。

| 项 | 内容 |
|---|---|
| 新增测试 | `tests/test_state_transition.py`（23 个）：状态常量与 anomaly 权重、`_classify_day` 全部 6 分支 + 分支优先级（EUPHORIA 先于 CONFIRMED_RECOVERY）、升级需 3 日确认/降级立即生效（合成价格序列端到端）、`get_state` 最近前序回退、`allows_anomaly` 0.5 阈值、`_get_breadth` 快照计算与 0.5 默认、状态分布与转移事件提取 |
| 行为观察 | ① 无因子快照时 breadth=0.5，崩盘首日只能降到 STABILIZING，vol_20d 累积超 0.25 后才到 PANIC——降级是逐级的；② `_get_breadth` 每交易日各开一次 sqlite 连接（冻结代码，未改动） |
| 验证 | **201 passed**（178 + 23 新），零回归；冻结文件 `state_transition.py` 零改动 |
| 测试覆盖进展 | `thesis/` 包从近零覆盖到 Guardian 核心完整特征化；剩余：`confidence_overlay.py`（另一个冻结核心）及 thesis/ 其余模块 |

### 第六轮：Confidence Overlay 特征化测试（2026-07-23，observability）

| 项 | 内容 |
|---|---|
| 新增测试 | `tests/test_confidence_overlay.py`（19 个）：复合公式（0.10/0.50/0.40 权重）、55/65/75 全部档位边界（含 54.9/55、64.9/65、74.9/75 临界）、`allows_anomaly` 仅 normal/full 放行、三个子分数公式与中性默认值、MA20>MA60 的 +10 加成 |
| **行为发现 1** | `confidence_overlay.py` 模块 docstring 描述的是**旧版**公式（0.4/0.3/0.3 权重、30/50/70 阈值），实际代码是 6-S.5.5b 版（0.10/0.50/0.40、55/65/75）。测试钉住的是**代码行为**。docstring 未改（冻结文件），建议解冻窗口修正文档 |
| **行为发现 2** | `_compute_breadth_recovery` 的 try/finally **没有 except**——`stock_factor_snapshot` 表缺失时抛 `sqlite3.OperationalError`；而 `state_transition._get_breadth` 同类场景捕获异常回退 0.5。两个 Guardian 核心组件的错误处理不一致，已用测试分别钉住现状，留待解冻决策 |
| 解冻候选清单 | 两条发现 + 三条行为观察已整理为独立文档 [GUARDIAN_THAW_CANDIDATES.md](GUARDIAN_THAW_CANDIDATES.md)（observability 性质，非 freeze entry） |
| 验证 | **220 passed**（201 + 19 新），零回归；冻结文件 `confidence_overlay.py` 零改动 |

### 第七轮：零调用竞技场归档（2026-07-23，工程卫生）

| 项 | 内容 |
|---|---|
| 归档决策 | `tournament.py` 与 `crowding_arena.py` 经核实为 v2-v3 演进遗留的研究工具（零生产调用），非 v4.0 运营组件。**归档不删除**——10 个特征化测试随行保留 |
| 执行 | 两文件移至 `src/evolution/archive/`，新增 `archive/__init__.py` 说明归档原因与复活程序（移回 + 跑测试 + 按当前协议重新评估）；两文件 docstring 加 ARCHIVED 标记 |
| 引用同步 | `test_evolution_arenas.py` 导入路径、`evolution/__init__.py` 模块地图、`PROJECT_HANDOVER.md` 两处引用全部更新 |
| 验证 | **220 passed**，零回归；`ruff check src/` 零错误 |

### 验证

- `compileall` 全量通过（src + scripts）
- pytest **160 passed / 1 skipped / 2 failed**——与改动前完全一致；2 个失败均为 `test_real_data.py` 依赖 mootdx 实时行情库（环境依赖，非改动引入）
- 冻结兼容原则：所有修复均为行为等价变换（日志增强、死代码删除、`strict=False`、AST 等价格式化）；未修改任何 `config/*.yaml` 冻结协议与冻结门槛逻辑

### 第八轮（2026-07-24）：仓库拆分执行（P2 第 7 项）

| 项 | 内容 |
|---|---|
| 前置评估 | `docs/REPO_SPLIT_ASSESSMENT.md`——67 个相关提交、272 个跟踪文件、零交叉引用、零敏感文件；**CI 从未生效**（`.github/workflows/` 埋在子目录）为首要拆分理由 |
| 执行 | 镜像克隆（`--no-local` 物理隔离）→ `git filter-repo --subdirectory-filter stock-sieve` → 工作仓库验证 → hash 映射批注 → 新远程推送。原目录全程只读未动 |
| 结果 | 217MB → **1.3MB**；80 → 68 提交（纯 stock-sieve 历史）；`.github/` 升至仓库根，**CI 首次生效** |
| 验证 | pytest 220 passed / 1 skipped / 2 failed（与拆分前逐项一致，2 失败仍为 mootdx 环境依赖）；ruff `src/` 零错误、scripts+tests 37 处既有告警逐条相同；远程 CI 两次运行（`a2de09f`、`3de7374`）**均 success** |
| hash 映射 | `cda3940`→`6545137`、`e2429db`→`b109ab4`、`471f88c`→`084c6bc`、`8e94efb`→`a2de09f`；批注于 `PROJECT_HANDOVER.md` ×1 + `PROJECT_HANDOVER_2026-07-19.md` ×2，完整 68 条映射存 `docs/COMMIT_MAP_REPO_SPLIT.txt` |
| 远程布局 | 旧仓库改名 `zcode-misc-archive`（全量历史在线归档）；新 `stock-sieve` 仓库名实相符，承接拆分后历史 |

### 第九轮（2026-07-27）：thesis/ 测试全覆盖 + 归档收尾 + 门禁收紧

> 原则不变：特征化测试只钉现状不改行为；归档不删除；冻结层零改动。
> 本轮测试总量 220 → **582 passed / 1 skipped / 2 failed**（2 失败仍为 mootdx 环境依赖）。

| 项 | 内容 |
|---|---|
| thesis/ 特征化测试全覆盖 | 20 个模块共 **332 个测试**（八批提交）：market_anomaly/signal_engine/market_recovery(63)、thesis_validator/residualizer(26)、doctrine_underwriting/bayesian_allocation(28)、event_reaction/expectation_gap(26)、fundamental_recovery/factor_momentum/sector_confirmation(46)、investment_memory/thesis_ledger(28，含冻结 KillCriteria)、timing_layer/counterfactual(22)、candidate_generator/crowding_calculator/sustainability_calculator(93) |
| thesis/ 零调用模块归档 | `adaptive_router.py`（6-Q.5b softmax 路由器）、`market_state_machine.py`（6-S.5.3 四态分类器，已被冻结的 state_transition.py 取代）→ `src/thesis/archive/`；先补 30 个特征化测试随行，`archive/__init__.py` 记录原因与复活程序 |
| scripts/ 归档 | 46 → 20 保留 + 25 归档：保留全部 `backfill_*`（数据重建路径）、`shadow_*`/`monthly_belief_audit`（日常运营）、`run_bootstrap_validation`（冻结门槛复现，被冻结配置引用 5 次）、`run_evolution_observation`（Phase 4 观察）；25 个一次性研究脚本（实验/解剖/消融/replay）移入 `scripts/archive/` 并附 README 路径映射（冻结文档中的旧路径引用 → 新位置）。已核实脚本间零交叉导入、测试与 CI 零引用 |
| print→logger | 业务模块 **54 处** `print()` 全部改走 `src.utils.logger`（14 文件，按 ⚠️/❌/🚨/💀 分级 warning，其余 info）；CLI 入口（`cli.py`/`runner.py`/`daily_run.py`/`paper_trading/runner.py` 共 166 处）**有意保留**——logger 输出走 stderr 带时间戳，转换会改变面向用户的 stdout 协议，属 UX 行为变更 |
| ruff 全面清零 | 修复 19 处残余（SIM115 上下文管理器 ×7、B007 循环变量 ×7、SIM105 suppress ×3、F401/SIM300 自动修复 ×3，其中 6 处为本轮新写测试引入，已自查自修）；19 个测试文件补齐 `ruff format`；`scripts/archive/` 加入 `extend-exclude`（归档历史脚本不再 lint）。**`ruff check src tests scripts` 与 `ruff format --check` 全绿**（修正注：此结论当时对 `src/data/` 不成立——`ruff.toml` 的 `extend-exclude` 里裸 `"data"` 无斜杠 glob 匹配任意层级，导致整个 `src/data/` 包从未被 lint/format，CI 同样盲区；第十轮已修正为 `"data/**"` 并清掉暴露的 155 处） |
| mypy 首轮收紧 | `check_untyped_defs = True` 启用（此前无注解函数体完全不检查）。收紧只暴露 25 个增量错误，其中 23 个源于 `candidate_generator._funnel_log_buffer` 一处注解错误（`list[tuple]` 实为 `list[dict]`），另 2 个为 `ra`/`out` 缺变量注解——3 处注解 bug 已修复（纯注解，零行为变更，被 26 个特征化测试保护）。基线保持 **186 错误 / 50 文件**；包分布：data 56、thesis 25、factors 22、agents 21、evolution 18、validation 15，simulation 等零错误。下一步按包清零后再开 `disallow_untyped_defs` |

**本轮钉住的行为 QUIRK**（测试内有 `QUIRK (pinned)` 注释，供解冻窗口评估）：

- `sustainability_calculator`：`accel_q2 = accel_q1` 占位符 → `reversal_count` 实际恒 ≤1，`CONSISTENCY_MAX_REVERSALS=1` 检查形同虚设
- `crowding_calculator`：`compute()` 的 `crowding_score_v1` **恒为 None**（横截面复合分只在 backfill 脚本里算）
- `candidate_generator`：Stage 2 自 v3.3 起**无硬门控**（`RS_HARD_GATE_THRESHOLD=0.0` 是死常量）；`recovery_score` 字段被 EGE 占用（`gap_score*50+50`）；Stage 1 公式实际值域 [20,80]，0-100 钳制永不触发
- `market_state_machine`（已归档）：`recovery_prob == 0.48` 恰好不满足 `>0.48` 也不满足 `<0.48`，落入默认 uncertain 分支
- `adaptive_router`（已归档）：docstring 示例数值有误（0.45×-0.02+0.15×-0.03=-0.0135，非 -0.013）
- 既有清单延续：EGE/counterfactual 浮点残差、timing_layer `+1` blend、factor_momentum `or 50` 吞 0.0（见各测试文件注释）

### 第十轮（2026-07-27）：mypy 全库清零 + strict 层扩编

> 原则不变：注解随实修、行为零变更；先测试后重构；冻结层零改动。
> 全量验证：mypy **0 errors / 145 files**；ruff check + format 全绿；**582 passed / 1 skipped / 2 failed**（基线不变）。

第九轮 `check_untyped_defs = True` 后基线 **186 错误 / 50 文件**，本轮按包分五步清零（每步 pytest + ruff 双门禁后提交）：

| 步 | 范围 | 基线变化 |
|---|---|---|
| 1 | strict 层首批：api/market/memory/simulation/paper_trading/execution 六包开 `disallow_untyped_defs` | 186（不变，六包本已零错误） |
| 2 | 6 个小包清零 17 错（audit/execution 等），2 包进 strict | 186 → 169 |
| 3 | utils + validation 清零 22 错 | 169 → 147 |
| 4 | factors / evolution / agents / thesis 清零（`evolution/archive` 与 `thesis/archive` 用 mypy.ini **模块级** `ignore_errors` 排除——路径正则 exclude 在 Windows 反斜杠下失效，已踩过） | 147 → 63 |
| 5 | data 包 + daily_run + 下游连锁清零 | 63 → **0** |

关键修复（均为注解/防御性守卫，零行为变更）：

- **ruff.toml 排除 bug**：`extend-exclude` 裸 `"data"` 无斜杠匹配任意层级，整个 `src/data/` 从未被 lint/format（CI 同盲区）→ 改为 `"data/**"` 锚定仓库根，暴露 155 处：`--fix` 吃掉 119（UP045 ×60、I001 ×18 等），手修 36（index_provider 10 处 `logger` 漏 import——print→logger 转换遗漏、SIM105→`contextlib.suppress`、E731 lambda→def、F841 死变量、B905 `strict=False`）
- `evaluation_crud` 18 处 Optional 默认值（`str = None` → `str | None = None`）
- `db.py` 动态属性 `conn._managed_finalizer` 加 `# type: ignore[attr-defined]`（惯用法，setattr 写法被 ruff B010 拦回）
- `daily_run.py`：排序键 `perf[r[0]][0] or 0.0` 防 None 混入崩溃；ReconciliationBuilder `build_for_decision` 返回 `dict | None`，两个调用点补 None 守卫（原路径 upsert(None) 必抛 AttributeError 被外层 except 吞成 warning，守卫后静默跳过——少一条误导性 warning）
- `MispricingObject` 五字段诚实扩宽为 `float | None`（运行时本就可能 None），`to_dict` 的 `margin_change` 补 None 守卫（潜在崩溃路径 bug fix）
- mypy.ini：误放在 `[mypy-src.execution.*]` 段下的全局 `exclude`（mypy 静默忽略 + 警告）移回 `[mypy]` 全局段

**strict 层扩编**：清零后逐包测量 `disallow_untyped_defs` 增量——utils 1、factors 2、thesis 7，修完 10 处缺失注解后三包晋升 strict 层（现共 **9 包**：api/market/memory/simulation/paper_trading/execution/utils/factors/thesis）。剩余待晋升：validation 11、evolution 25、agents 28、data 88、audit 10 个 untyped def。

**CI mypy 门禁**：**已转硬门禁**（2026-07-27，第十轮收尾）——`ruff==0.15.22` 与 `mypy==2.3.0` 钉入 `pyproject.toml` dev extras（与 pre-commit 及本地基线一致），CI 安装改为 `pip install -e ".[dev]"`，mypy 步骤移除 `continue-on-error`。任何类型错误回潮将直接红 CI。

---

## 二、待办 backlog（按优先级）

### P1 — 测试与质量门禁

1. ~~**测试覆盖不足**~~：**已完成**（2026-07-27，见上方第九轮）——`thesis/` 全部 20 个模块 332 个特征化测试补齐；全量 582 passed。剩余已知空白：`test_real_data.py` 2 个失败为 mootdx 环境依赖（需本机安装 mootdx 才能转绿）。
2. ~~CI 门禁收紧~~、~~静默吞错~~、~~F841~~：**已完成**（见上方第二轮）。mypy 首轮收紧（第九轮 `check_untyped_defs = True`）、**186 错误全库清零**与 **CI mypy 硬门禁**均已完成（第十轮，0 errors / 145 files，`mypy==2.3.0` 钉入 dev extras）。剩余路线：validation/evolution/agents/data/audit 五包补齐 untyped def 注解后晋升 strict 层（增量 11/25/28/88/10）。
3. ~~**tests/ 与 scripts/ 残余风格项**~~：**已完成**（2026-07-27，见上方第九轮）——19 处全部修复，`ruff check src tests scripts` 与 format 检查全绿；归档脚本排除出 lint 范围。

### P1 — 模块冗余/演进残留

4. ~~进化引擎双轨并存~~：**已完成**（见上方第三轮）——`genome.py` 抽取 + `engine.py`→`spec_engine.py` 重命名，职责边界：genome.py=数据类 / spec_engine.py=协议级季度机制 / engine_v1.py=生产级每日引擎。
5. ~~三竞技场并存~~：**已完成**（见上方第四、七轮）——职责核实为「锦标赛 / 拥挤度 / 生存选择」三者分工；`arena.py`→`tournament.py`、`competitive_arena.py`→`crowding_arena.py` 重命名消歧后，两个零调用模块已归档至 `src/evolution/archive/`（测试随行），`survival_arena.py` 保留生产使用。
6. ~~数据层四文件边界~~：**已核实，无需重构**（2026-07-23）——四文件已是清晰的 Facade + Mixin 结构：`evaluation_schema.py`=DDL 常量（391 行）、`evaluation_crud.py`=CRUD Mixin（663 行）、`evaluation_migration.py`=迁移 Mixin（424 行）、`evaluation_db.py`=门面（101 行，仅 `__init__`/`init_db`/`connect` + 再导出）。CRUD 与 Migration 两个 Mixin 零方法名冲突，全库 14 处导入均经门面路径，`test_evaluation_db_connections.py` + `test_db_connection_governance.py` 10 个测试覆盖有效。

### P2 — 工程卫生

7. ~~仓库根不独立~~：**已完成**（见上方第八轮）——已按方案 A 拆分为独立仓库（`git filter-repo --subdirectory-filter`），旧仓库改名 `zcode-misc-archive` 归档，新 `stock-sieve` 仓库 CI 首次生效且两次运行全绿。拆分前未跟踪文件仍留在原目录，不随拆分迁移。
8. ~~**scripts/ 归档**~~：**已完成**（2026-07-27，见上方第九轮）——25 个一次性研究脚本移入 `scripts/archive/`（附 README 路径映射），20 个运营/冻结复现脚本保留原位。
9. ~~**print/logger 混用**~~：**已完成**（2026-07-27，见上方第九轮）——业务模块 54 处统一走 `src.utils.logger`；CLI 入口 166 处 print 经评估为有意 stdout UX，保留不改。
10. ~~pre-commit 与 CI 的 ruff 版本对齐~~：**已完成**——pre-commit 升级至 `v0.15.22`，与基线一致。

---

## 三、整改原则（建议）

- **冻结层不动**：凡 `PROJECT_HANDOVER.md` 标记 Frozen 的协议与 `config/investment_brain_v1_freeze.yaml`，代码重构不得改变其行为语义，重构前后用 `scripts/run_*_validation.py` 回归。
- **门禁只收不放**：CI 硬门禁规则只增不减，新代码不得引入新 lint 错误。
- **先测试后重构**：P1 第 4-6 项的模块合并，必须先补对应包的回归测试（P1 第 1 项）。
