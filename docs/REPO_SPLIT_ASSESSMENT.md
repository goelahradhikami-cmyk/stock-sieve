# stock-sieve 独立仓库拆分 — 影响评估

> 日期：2026-07-24。纯评估文档，未做任何变更。
> 结论先行：**建议拆分，用 `git filter-repo` 保留历史**。最大理由不是整洁，
> 而是 **CI 从未生效过**——这是功能性缺陷，不是工程洁癖。

---

## 一、现状事实

| 事实 | 数据 |
|---|---|
| git 根 | `C:\Users\xiayo\ZCodeProject`（stock-sieve 是其子目录） |
| 远程 | `github.com/goelahradhikami-cmyk/stock-sieve.git` — **名为 stock-sieve，内容是整个 ZCodeProject 根** |
| 仓库总提交 | 79；其中涉及 `stock-sieve/` 的 **67** |
| stock-sieve 跟踪文件 | **272 个**（src 144 / scripts 45 / tests 24 / data 21 / config 12 / 其余配置文档），未跟踪 0 |
| 仓库内非项目文件 | `phase2-demo/`（4）、根 `docs/`（2）、`checkout-ux-optimizer.html`（1）— 共 7 个 |
| 跟踪内容体积 | 3.3 MB（很轻）；`.git` 对象库 217 MB（历史包袱，3198 个松散对象） |
| 兄弟目录交叉引用 | **零**——src/config/scripts/tests 中无任何对兄弟项目的引用 |
| 敏感文件 | **零**——无 .env / 密钥 / 证书被跟踪，历史中亦无 |
| data/ 目录 | 174 MB 主要是未跟踪的 DB/缓存（git 只跟踪 21 个报告类文件） |

## 二、关键发现

### F-1. CI 从未生效（功能性缺陷，拆分的首要理由）

`.github/workflows/ci.yml` 位于 `stock-sieve/` 子目录内，但 GitHub Actions **只读取仓库根**的
`.github/workflows/`。根目录没有 `.github/`。

后果：本次整改建立的**所有门禁——ruff 硬失败、format 检查、pytest——在 GitHub 上从未运行过**。
本地跑绿的 220 个测试没有任何云端守门。clone 该远程仓库的协作者拿到的是一个
CI 配置存在但永不执行的仓库。

### F-2. 远程仓库名实不符

GitHub 上的 `stock-sieve` 仓库，根目录浏览体验是：一个 `stock-sieve/` 文件夹
外加 `phase2-demo/`、`docs/`、一个无关 HTML 文件。README 无法自动渲染在仓库首页
（它在子目录里），对任何外部访问者都是困惑的。

### F-3. 冻结治理文档引用 commit hash（拆分的主要成本）

`PROJECT_HANDOVER.md` 与 `PROJECT_HANDOVER_2026-07-19.md` 共 **3 处**引用提交
`cda3940`（UI 终端主题 P0）。`git filter-repo` 重写历史后**所有 hash 改变**，这些引用将失效。
数量少（1 个 hash × 3 处），可通过「旧→新 hash 映射表 + 文档批注」缓解。

### F-4. .git 对象库 217 MB 的历史包袱

跟踪内容仅 3.3 MB，但对象库 217 MB（松散对象，未打包），说明历史上提交过大对象
（疑似数据库或数据文件，后已删除）。filter-repo 拆分时只保留 stock-sieve 子目录历史，
新仓库体积会骤降到 MB 级；原仓库的包袱也得以隔离。

### F-5. 拆分不会触及的东西

- 未跟踪的兄弟目录（QuantDinger、TradingAgents-astock 等）——本就不在 git 内
- `data/` 下 174 MB DB/缓存——未被跟踪；新仓库 clone 后需按既有脚本重建数据（应文档化）
- 67 个提交的作者、时间戳、消息——filter-repo 全部保留

## 三、方案对比

| 方案 | 历史保留 | hash 变化 | 工作量 | 评价 |
|---|---|---|---|---|
| **A. `git filter-repo` 子目录拆分** | ✅ 67 个提交全保留 | 全部重写 | 中 | **推荐**。历史是这个项目的治理资产（冻结审计轨迹），不能丢 |
| B. 新仓库从零 `git init` | ❌ 全部丢失 | — | 小 | 不可接受：v4.0 冻结过程、Monthly Belief Audit 基线等审计轨迹会消失 |
| C. 维持单仓，只把 workflow 移到根目录 | ✅ | 无 | 极小 | 只治 F-1，不治 F-2/F-4；根目录 `.github/` 会作用于**整个混杂仓库**（对 phase2-demo 等无意义） |

### 推荐执行路径（方案 A，供决策，未执行）

1. **备份**：`xcopy ZCodeProject ZCodeProject.bak /E /I`（或至少 `git clone --mirror`）
2. 安装：`pip install git-filter-repo`
3. 拆分：`git filter-repo --subdirectory-filter stock-sieve`（在镜像克隆上操作，不在原仓库）
4. 处理非项目文件：`phase2-demo/`、根 `docs/`、`checkout-ux-optimizer.html` 留在原仓库归档
5. 记录 **旧→新 commit hash 映射**（filter-repo 自动生成 `commit-map`），
   在 `PROJECT_HANDOVER.md` 批注 `cda3940` 的新 hash
6. 新远程：GitHub 新建干净仓库（或重命名现有仓库为 `zcode-misc-archive` 后复用 `stock-sieve` 名），
   force-push 拆分结果
7. 拆分后 CI **自动生效**（`.github/workflows/ci.yml` 成为仓库根路径）——
   首次推送即是一次完整的 ruff/format/pytest 云端验证
8. 新仓库补一份「数据重建指南」：哪些 DB 由哪些 `scripts/backfill_*.py` 生成

## 四、风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 历史重写后 `cda3940` 引用失效 | 低 | commit-map 批注（仅 1 hash × 3 处） |
| 操作失误丢历史 | 中 | 必须先在镜像克隆上操作，原目录保持只读 |
| 原远程仓库已有他人 clone | 低 | 当前为个人项目迹象明显（单人提交），force-push 前确认无其他协作者 |
| 冻结协议引用的「提交边界」语义变化 | 中 | v4.0 冻结以提交 `e2429db`/`471f88c` 等为节点，拆分后应在 PROJECT_HANDOVER 补记新 hash 映射，保持审计链连续 |

## 五、决策建议

拆分的实际收益排序：**F-1（CI 生效）> F-4（甩掉 217MB 包袱）> F-2（名实相符）**。
成本是一次性的历史重写 + 3 处 hash 批注。建议执行方案 A；
若近期有冻结评审节点，可在评审通过后立即执行，避免评审期内 hash 语境切换。
