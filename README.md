# cffex-trader · CFFEX 多品种期货多因子轮动（开源分享版）

中金所 8 品种（IF/IH/IC/IM 股指 + TS/TF/T/TL 国债）的**多因子截面轮动**交易编排框架，
整合「数据更新 → 信号生成 → 委托执行 → 风控止损 → 换月检测」完整闭环，为 Claude Code
skill 生态设计，也可独立作为 Python CLI 使用。

> **仅供模拟交易 / 学习研究使用，不构成投资建议。** 期货交易风险极高，请勿接入真实资金。

> **⚠️ 策略引擎不随本仓库分发**：`futures_multifactor_engine.py` 需用户手动放入本目录
> （见下方「策略引擎获取」）。缺省时查询/风控命令可用，策略类命令会明确报错。

## 架构

```
┌─────────────┐   OHLC    ┌──────────────────┐  signal.json  ┌─────────────────┐
│ futures-     │ ───────▶ │                  │ ────────────▶ │                 │
│ trader (DB)  │          │  orchestrator.py │               │ 模拟交易 API     │
└─────────────┘          │  (本仓库)         │ ◀──────────── │ (账户/下单)      │
┌─────────────┐  basis/  │                  │   确认后下单   └─────────────────┘
│ Wind/akshare│  YTM     │                  │
└─────────────┘ ───────▶ └──────────────────┘
┌─────────────┐  新闻    ┌──────────────────┐
│ mx-search   │ ───────▶ │  消息面情绪 overlay│
│ (可选)       │          └──────────────────┘
```

- **策略引擎**（用户自备）`futures_multifactor_engine.py`：vol/mom/carry 三因子截面排名加权，
  做多前 2、做空后 2；Carry 按股债分组排名（股指 basis / 国债 YTM）。
- **编排器** `orchestrator.py`：CLI 入口 + 下单执行 + 增量调仓 + 风控（本仓库核心）。

## 功能特性

| 模块 | 说明 |
|---|---|
| 增量调仓 | 对照当前持仓只下差异单；先平后开；同向跨月自动滚动 |
| 实时对手价 | TqSdk 免费行情：买报卖一、卖报买一；失败回退收盘参考价 |
| 消息面因子 | mx-search 新闻情绪按组打分，W_NEWS 权重并入综合得分（可关） |
| 自动换月检测 | 合约到期预警 / 映射失效报警（下单前硬阻断）/ 持仓跨月提示 |
| 软件侧止损 | 浮亏 ≤ −8 万 或 使用率 ≥ 95% 自动平仓（盯盘任务在线时） |
| 信号反馈闭环 | signal_log.jsonl 归档 + report 回放多空方向命中率 |
| 交易记录同步 | 下单后自动把账户快照/委托/持仓写进 Markdown 日志（幂等） |
| 部署自检 | `doctor` 命令逐项检查依赖/凭据/数据/映射，自动生成上手教程 |

## 快速开始

```bash
# ⓪ 放入策略引擎（不随仓库分发，见「策略引擎获取」）
#    放置路径：本目录，文件名 futures_multifactor_engine.py

# ① 安装依赖
pip install pandas requests akshare tqsdk

# ② 配置环境变量（CFAPIKEY 必需，其余可选）
export CFAPIKEY="<你的模拟交易用户ID>"

# ③ 部署自检（首次运行会生成 GETTING_STARTED.md 个性化教程）
python orchestrator.py doctor

# ④ 日常使用
python orchestrator.py update       # 更新 OHLC 数据
python orchestrator.py signal       # 生成信号
python orchestrator.py orders       # 预览委托（dry-run）
python orchestrator.py orders --yes # 确认后下单（还有 y 二次确认）
```

## 策略引擎获取（重要）

**本仓库只分发编排框架，策略引擎 `futures_multifactor_engine.py` 需手动加入文件夹**：

1. 从你的私有渠道获取策略文件（自行编写 / 课程资料 / 私有仓库）。
2. 放到 `orchestrator.py` 同一目录，文件名保持 `futures_multifactor_engine.py`。
3. 验证：`python -c "import futures_multifactor_engine"`，再跑 `doctor` 应显示
   「策略引擎 ✅」。

引擎需实现编排器依赖的最小接口（可参考任意一份实现）：

```python
SYMBOLS        # ["IF0", ..., "TL0"] 8 品种连续代码
NAMES          # {symbol: 中文名}
MAIN_CONTRACT  # {连续代码: 带月份合约}，换月需更新
DB_PATH        # 本地 SQLite 路径（futures_ohlc 表：date/close 列）
generate_signals()   # -> (sig_df, longs, shorts)
```

引擎缺失时的行为：
- `update` / `signal` / `orders` / `report` / `trade` → 明确报错并指引放置路径。
- `holdings` / `account` / `risk` / `stop` / `contracts` → **正常可用**（纯查询/风控）。
- `doctor` → 第 0 项检查失败，数据库/换月检测自动跳过。

## 前置 skill（上游依赖）

| skill | 作用 | 必需? | 默认路径（可用环境变量覆盖） |
|---|---|---|---|
| futures-trader | 期货 OHLC 本地 SQLite | ✅ | `~/.claude/skills/futures-trader` |
| dfcfqh-zjsbsimrace | 模拟赛账户查询/下单 | ✅ | `~/.claude/skills/dfcfqh-zjsbsimrace` |
| mx-search | 资讯搜索 → 情绪因子 | ⭕ | `~/skills/mx-search` |

路径环境变量：`CFFEX_FUTURES_TRADER_DIR` / `CFFEX_DFCFQH_SCRIPT` /
`CFFEX_MX_SKILL_DIR` / `CFFEX_DB_PATH`（详见各文件头部注释）。

## 环境变量

| 变量 | 必需 | 用途 |
|---|---|---|
| `CFAPIKEY` | ✅ | 模拟交易 API 认证（下单/查询） |
| `TQSDK_USER` / `TQSDK_PASS` | ⭕ | 快期账户，拉实时五档（免费注册 shinnytech） |
| `MX_APIKEY` | ⭕ | mx-search 资讯搜索（缺省消息面降级纯量化） |

## 安全设计（不可跳过的铁律）

1. **下单前必须列完整委托清单并获用户明确确认**；`orders` 默认 dry-run。
2. `--yes` 执行前仍需输入 `y` 二次确认；`--force` 仅限用户预先授权的定时任务。
3. **交易失败/超时后禁止自动重试、改价、补单**，只能查询状态。
4. 平仓前必须重查持仓方向（空头→买平、多头→卖平）。
5. 自动止损绕过逐笔确认——启用前请确认接受该行为。

## 重要免责与风险提示

- 本项目为**模拟交易**工具，收益不代表真实收益；不构成任何投资建议。
- 因子方向（vol/mom/carry 的组内升/降序）**样本期敏感**，使用前请自行回测验证
  （引擎内有对应注释与可调开关），勿直接照搬默认参数实盘。
- 软件止损依赖盯盘进程在线：App/脚本关闭期间**不生效**，且为周期轮询非毫秒级。
- 满仓/激进模式使用率接近 100%，任一品种反向 1~2% 即可能触发强平，请理解边界后使用。

## License

MIT
