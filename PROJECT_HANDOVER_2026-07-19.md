# Stock Sieve 交接补充 · 2026-07-19（UI 终端主题改造）

**类型：** 增量交接（主交接表见 `PROJECT_HANDOVER.md`，2026-07-15）
**范围：** Streamlit 面板设计评审 → P0/P1 修复 → 截图验证 → 部分入库
**提交：** `cda3940`（2026-07-24 仓库拆分后新 hash：`6545137`） `style(ui): terminal theme consistency fixes from design review`

---

## 一、本轮做了什么

对 9 个页面逐一截图评审（真实数据，端口 8503），按优先级修了两批：

### P0 — 已入库（commit cda3940，拆分后 6545137，6 文件 +52/−29）

| 文件 | 改动 |
|------|------|
| `.streamlit/config.toml`（新增） | 原生深色主题：`base="dark"`、`primaryColor=#00bcd4`、背景/文字与 `theme.py` 对齐。 dataframe、radio、slider 等原生组件**不吃注入 CSS**，必须靠它 |
| `.gitignore` | `.streamlit/` → `.streamlit/*` + `!.streamlit/config.toml`（secrets.toml 仍被忽略） |
| `src/ui/components/committee_card.py` | 删掉 `template="plotly_white"`（深色页面里一块白图），改用 `terminal_layout()` |
| `src/ui/utils/theme.py` | ① 导航 CSS 重写：直接锁定 `stSidebar div[role="radiogroup"]`，`> div:first-child` 隐藏圆圈，`:has(input:checked)` 做 cyan 选中态；② 补上 `[data-testid="stDeployButton"]` / `stToolbar` 隐藏 |
| `src/ui/views/committee_room.py` | 摘要卡去掉与 committee_card 重复的 Alpha / 仓位上限，只留 日期 / 标的 / Confidence |
| `src/ui/components/decision_timeline.py` | thesis 截断 60→80 且只在真截断时加"…"；删掉卡片间多余 divider |

### P1 — 未入库（改动在 4 个**未跟踪**新页面文件里，随 9 页重构一起提交时自然带入）

| 文件 | 改动 |
|------|------|
| `src/ui/views/data_stage.py` | 指数卡固定 4 列（单卡不再撑满整行）；删 `NULL AS last_trade_date` 空列；"最近刷新"截短到日期 |
| `src/ui/views/factors_stage.py` | 市场状态卡固定 4 列 |
| `src/ui/views/pipeline_overview.py` | 研究阶段状态语义：今日跑过→绿 / 有历史但今日未跑→黄 / 从未产出→红；"进入 →"按钮改 ghost 风格（透明底+细边框+hover cyan） |
| `src/ui/views/execution_stage.py` | 摩擦拆解补上 **佣金 ¥405.80** 和 **过户费 ¥15.34**（原来只有总摩擦/滑点/印花税） |

### 仍未提交的其他工作区改动（本轮之前就有，未动）

- 9 页重构本体：`src/ui/app.py`(M)、`src/ui/nav.py`、`src/ui/views/{pipeline_overview,data_stage,factors_stage,portfolio_stage,execution_stage}.py`(??)
- 8 个人格 YAML、`src/agents/*`、`src/validation/*`、`src/evolution/*`、`src/runner.py` 等核心逻辑改动
- 新增模块 `src/audit/`

---

## 二、已知坑（下次改 UI 前必读）

1. **Streamlit 缓存已 import 的模块**：改 `theme.py`、任何 views/components 文件后**必须重启 server 才生效**（本次验证就踩过：页面reload没用，进程重启才加载新 CSS）。开发建议加 `--server.runOnSave true`。
2. **`st.markdown('<div class="x">')` 包不住后面的组件**：Streamlit 每个元素独立容器，开放标签被浏览器自动闭合。旧导航 CSS 失效的真正原因就是 `.nav-radio .stRadio` 这个祖先选择器永远匹配不到。要圈定组件，用 Streamlit 自带的 testid 定位（如 `section[data-testid="stSidebar"]`）。
3. **`config.toml` 只在 server 启动时读取**，改完必须重启。
4. **CSS 选择器随 Streamlit 版本漂移**：能用 `config.toml` 原生主题解决的（颜色、背景、字体）就不要写 CSS；CSS 只留卡片/徽章等自定义组件。当前 DOM 结构（1.58）：radio label 的第一个子 div 是圆圈，第二个是文字。
5. 截图会保留滚动位置：自动化截图前先 `window.scrollTo(0,0)`。

---

## 三、验证记录

- 改前全 9 页截图：`C:\Users\xiayo\Documents\kimi\workspace\shots\01~09_*.jpg`
- P0 验证：`shots\fixed_*.jpg`、`shots\fixed2_overview.jpg`
- P1 验证：`shots\p1_*.jpg`、`shots\p1b_*.jpg`
- 启动命令：`streamlit run src/ui/app.py --server.port 8503`
- 验证要点对照：表格深色✓ 滑块/导航 cyan✓ 委员会图深色✓ 无 Deploy 按钮✓ 导航无圆圈、选中态 cyan 左边框✓ 指数卡 1/4 宽✓ 研究阶段黄点✓ 摩擦拆解 5 项✓

---

## 四、P2 Backlog

### 已修（2026-07-19 晚 · 未入库，随 9 页重构提交）

| 项 | 修法 |
|----|------|
| SYSTEM STATUS 硬编码 | `app.py` 侧边栏接真值：Agent/会议计数 + `signal_snapshot` 最新 market_regime（牛市/熊市/轮动/震荡/危机，带涨跌色）；DB 不可达时显示 DEGRADED + "—"，删掉 8/8 假回退 |
| 时间范围未接线 | `data_loader.load_decision_timeline` 加 `days` 参数（近30天/90天/1年）。SQL 层验证：600519 全部=410、近30天=0、近90天=0、近1年=410 |
| 排行榜空态 + 雷达图偏移 | 空态文案精简；`terminal_polar` legend 从 `x=1.05`（把图挤偏）改为底部水平居中 |

### 剩余

| 项 | 位置 | 说明 |
|----|------|------|
| 红涨绿跌 vs 绿涨红跌 | `theme.py` GAIN/LOSS | 现 Bloomberg 配色（绿涨），A股直觉相反，产品决策待定 |
| 执行页数据洞察 | 数据层非 UI | 佣金占总摩擦 96%（77 笔小额单全触 ¥5 最低佣金），模拟单金额过小会系统性高估摩擦 |
| 管线数据问题（非 UI） | `daily_run` | 7-17 日志：股票池一次 1 只一次 40 只（vs 主数据 9,256）；178 条新决策但 Pending evaluations = 0；Post-Mortem 连续 0 产出 |
| Baostock 接入粗糙 | `_baostock_run.log` | 每只股一次 login/logout（近百次），应复用会话 |

---

## 五、快速上手（下个会话）

```bash
cd C:\Users\xiayo\ZCodeProject\stock-sieve
streamlit run src/ui/app.py --server.port 8503 --server.runOnSave true
```

改 UI 先读：`src/ui/utils/theme.py`（全部设计 token + CSS）、`src/ui/nav.py`（页面注册表）。
设计评审原文与截图由 Kimi 会话产出（2026-07-19），对比图见上文路径。
