---
name: cffex-trader
description: CFFEX 8 品种（IF/IH/IC/IM 股指 + TS/TF/T/TL 国债）多因子截面轮动交易编排：数据更新 → 信号生成 → 委托执行 → 风控止损 → 换月检测完整闭环。当用户要求更新期货数据、生成交易信号、查询模拟账户/持仓、或下单中金所品种时使用。
---

# cffex-trader · 交易编排 skill（开源分享版）

> **仅供模拟交易/学习研究，不构成投资建议。**
> **策略引擎 `futures_multifactor_engine.py` 不随仓库分发**，需用户手动放入本目录
> （最小接口见 README「策略引擎获取」）；缺省时查询/风控命令可用，策略类命令报错。
> 部署后先跑 `python orchestrator.py doctor` 自检并生成个性化教程（GETTING_STARTED.md）。

## 编排流程（三阶段闭环）

```bash
cd <本目录>

# ⓪ 首次部署：放入策略引擎 + 自检（依赖/环境变量/数据/合约映射，生成上手教程）
#    （策略引擎文件放置方法见 README「策略引擎获取」）
python orchestrator.py doctor

# ① 更新期货数据（每次调仓前）
python orchestrator.py update

# ② 生成多空信号 + 委托清单（写 signal.json；默认带消息面，--no-news 跳过）
python orchestrator.py signal

# ③ 生成增量下单清单（dry-run；对照当前持仓只下差异腿，先平后开）
python orchestrator.py orders
python orchestrator.py orders --yes      # 真正下单（执行前还要求输入 y 二次确认）

# 关闭对冲 → α 满仓轮动；--lots N 统一每腿手数（默认按保证金折算）
python orchestrator.py orders --yes --no-hedge --lots 1

# 辅助查询与风控
python orchestrator.py contracts   # 当前可交易合约（核对主力月份）
python orchestrator.py holdings    # 持仓
python orchestrator.py account     # 资金/权益
python orchestrator.py risk        # 爆仓/强平边界 + 逐腿风险
python orchestrator.py stop        # 止损状态 + 一次性检查（触发则自动平仓）
python orchestrator.py report      # 信号回放：多空方向命中率
```

## 因子定义

综合得分 `score = W_VOL·vol_rank + W_MOM·mom_rank + W_CARRY·carry_rank`
（默认 0.3/0.2/0.5），降序排序，做多前 2、做空后 2。

| 因子 | 来源 | 计算 |
|---|---|---|
| 波动 volatility | 本地 SQLite | 20 日收益率标准差 |
| 动量 momentum | 本地 SQLite | 20 日累计收益 |
| Carry | Wind 或 akshare | 股指=基差 basis；国债=到期收益率 YTM |

- Carry 按股债**分组截面排名**（basis 负值与 YTM 正值量纲不可比），映射到 [1,n] 尺度。
- **组内打分方向样本期敏感**：`INDEX_ASCENDING` / `BOND_ASCENDING` 及 vol/mom 的 rank
  方向都应自行回测验证后再信任默认值（引擎内有详细注释）。

## 消息面因子（可选，mx-search）

- `signal` 时按股债分组各查一个 broad query，关键词规则算组级情绪 ∈ [-1,1]，
  映射排名后以 `W_NEWS`（默认 0.15）并入得分：`score = 0.85·raw + 0.15·news`。
- **降级**：某组查询失败/全部过期 → 该组退化为纯量化（不被中性值稀释）；未装
  mx-search / 无 `MX_APIKEY` 时整体降级。
- **时效过滤**：只统计近 `NEWS_MAX_AGE_DAYS`(3) 天的带日期资讯，旧闻/科普文剔除。
- 词表/权重/查询语句都在 `orchestrator.py` 头部，可自行调整。

## 增量调仓（对照持仓，只下差异腿）

| 对比结果 | 处理 |
|---|---|
| 无该品种持仓 | 正常开仓 |
| 同向且手数 ≥ 目标 | `[跳过]` |
| 同向但不足 | 差额加仓 |
| 反方向 | 先平旧仓 + 反向开新仓 |
| 持仓在、目标外 | 自动平仓（旧腿） |

- 平仓单用**持仓实际月份合约**（从持仓中文名还原月份），换月过渡期正确滚动。
- 持仓查询失败退化为全量目标并醒目警告——此时务必先 `holdings` 核对防重复下单。

## 自动换月检测

`signal`（软提示）与 `orders`（硬阻断）自动执行三项检测：

1. **映射失效**：`MAIN_CONTRACT` 指向的合约不在可交易列表 → 给出建议新月份，
   `orders` 拒绝下单直到更新 `futures_multifactor_engine.py` 的映射。
2. **到期预警**：估算交割日（股指=当月第三周周五、国债=第二周周五）≤5 天报警。
3. **持仓跨月**：持仓月份 ≠ 主力月份 → 提示「先平旧月、再开新月」。

## 实时盘口（TqSdk，可选）

- 买（买开/买平）报**卖一**，卖（卖开/卖平）报**买一**（对手价，保证成交）。
- 拉不到时静默回退收盘参考价，打印标「参考价」。需 `TQSDK_USER/PASS`。

## 风险对冲（默认开启，`--no-hedge` 关闭）

账户有固定现货底仓时（如比赛账户 沪深300ETF + 国债现券），现货是天然 β 多头：

- 期货买开与现货 β 同源品种（IF/T）→ 加倍该 β 风险 → **跳过**。
- 期货卖开腿天然对冲现货 β → 保留，每腿 1 手最小对冲单位。
- 纯期货账户直接 `--no-hedge` 走 α 满仓轮动。

## 激进模式（可选满仓档）

- `--aggressive` 强制开 / `--no-aggressive` 强制关；默认按日期：today ≥
  赛程窗口（`PRELIM_START/END`）最后 3 个调仓周一的第一天时自动开启。
- 行为：关闭对冲 + 4 腿全开，使用率目标 `AGGRESSIVE_USE_RATE`(1.0)，手数折算按
  `min(目标, AGGRESSIVE_USE_CAP)`(0.92) 预留 ~8% 安全垫，避开 95% 止损线。

## 软件侧止损

模拟接口无交易所侧条件单，用软件盯盘实现：

- **触发**（任一）：期货总浮亏 ≤ −8 万，或使用率 ≥ 95%（`STOP_LOSS_*` 可调）。
- 每次成功下单后自动挂载（`arm_stop` 写 `stop_state.json`）；`stop` 手动检查、
  `stop-watch` 供定时任务无交互调用。
- 平仓方向按实时持仓自动判定（空头→买平、多头→卖平）。
- **边界**：依赖盯盘进程在线；App 关闭期间不生效；周期轮询非毫秒级，极端跳空可能超损。

## 记录文件

| 文件 | 何时写 | 用途 |
|---|---|---|
| `signal_log.jsonl` | 每次 signal | report 回放信号命中率 |
| `trade_log.jsonl` | 每次真实下单 | 复盘「信号→下单→盈亏」 |
| `交易执行记录.md` | 下单后自动同步 | 账户快照+委托+持仓日志（幂等覆盖当日小节） |
| `GETTING_STARTED.md` | 首次 doctor 自动生成 | 个性化部署教程 |

临时数据（接口输出 + signal.json）下单流程结束后自动清理。

## 安全规则（不可跳过）

1. 下单/撤单前必须列完整委托清单并获用户明确确认；`orders` 默认 dry-run。
2. `--yes` 执行前还需输入 `y`；`--force` 仅限预先授权的定时任务。
3. 交易失败/超时后**禁止自动重试、改价、补单**，只能查询状态。
4. 平仓前必须重查持仓方向。

## 环境依赖

- Python：`pandas`、`requests`、`akshare`（数据更新 + YTM 回退）；`tqsdk` 可选（实时盘口）。
- 环境变量：`CFAPIKEY`（必需）、`TQSDK_USER/PASS`、`MX_APIKEY`（可选）。
- 上游 skill：futures-trader、dfcfqh-zjsbsimrace（必需）；mx-search（可选）。
  路径可用 `CFFEX_FUTURES_TRADER_DIR` / `CFFEX_DFCFQH_SCRIPT` / `CFFEX_MX_SKILL_DIR`
  / `CFFEX_DB_PATH` / `CFFEX_TRADE_RECORD_MD` 覆盖。
- Windows 控制台需 UTF-8（脚本已自动 reconfigure）。
