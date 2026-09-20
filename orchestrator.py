#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cffex-trader · 编排器（开源分享版）
三阶段闭环：① 更新期货数据 → ② 生成多空信号 → ③ 生成/执行下单清单

子命令：
    python orchestrator.py update       # 更新 8 个品种主力连续 OHLC（futures-trader）
    python orchestrator.py signal       # 跑策略引擎，输出多空信号，写 signal.json
    python orchestrator.py contracts    # 查询当前可交易合约（核对主力月份映射）
    python orchestrator.py orders       # 读 signal.json，生成下单命令（默认 dry-run 只打印）
    python orchestrator.py orders --yes # 真正执行下单（执行前仍需人工逐笔确认）
    python orchestrator.py doctor       # 部署自检：依赖/环境变量/数据/合约映射 逐项检查

安全规则（不可跳过）：下单前必须列出完整委托信息并获得用户明确确认；
交易失败/超时后禁止自动重试、改价、补单，只能查询状态。

仅供模拟交易/学习研究使用，不构成投资建议。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

# Windows 控制台默认 GBK，改为 UTF-8 避免中文/表情符乱码或 UnicodeEncodeError 崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 策略引擎（futures_multifactor_engine.py）**不随本仓库分发**，需用户手动放入本目录。
# 缺失时 doctor 会给出获取指引；signal/trade 等策略命令会明确报错，查询/风控类命令不受影响。
try:
    import futures_multifactor_engine as eng
    ENGINE_AVAILABLE = True
except ImportError:
    eng = None
    ENGINE_AVAILABLE = False


def _require_engine():
    """策略命令入口检查：引擎未就位则报错退出（给出放置路径）。"""
    if not ENGINE_AVAILABLE:
        print("[ERROR] 未找到策略引擎 futures_multifactor_engine.py（策略文件不随仓库分发）。")
        print(f"请将策略文件手动放入本目录后重试: {HERE}")
        print("获取方式见 README.md「策略引擎获取」一节，或运行 doctor 自检。")
        sys.exit(1)


def _load_env_from_shell_rc():
    """兜底加载 shell 配置里的环境变量（CFAPIKEY/TQSDK_USER/TQSDK_PASS/MX_APIKEY）。
    Windows 下 bash 会话不一定 source ~/.bashrc（cron/非交互 shell），导致依赖时有时无。
    只在环境变量缺失时，从 ~/.bashrc / ~/.bash_profile 的 export 行补齐，不覆盖已有值。"""
    _KEYS = ("CFAPIKEY", "TQSDK_USER", "TQSDK_PASS", "MX_APIKEY")
    if all(os.environ.get(k) for k in _KEYS):
        return
    home = os.path.expanduser("~")
    for rc in (os.path.join(home, ".bashrc"), os.path.join(home, ".bash_profile")):
        if not all(os.environ.get(k) for k in _KEYS) and os.path.exists(rc):
            try:
                with open(rc, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line.startswith("export "):
                            continue
                        m = re.match(r'export\s+([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"]*)"|\'([^\']*)\'|(\S+))', line)
                        if not m:
                            continue
                        name, val = m.group(1), (m.group(2) or m.group(3) or m.group(4) or "").strip()
                        if name in _KEYS and val and not os.environ.get(name):
                            os.environ[name] = val
            except Exception:
                pass


_load_env_from_shell_rc()

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNAL_PATH = os.path.join(HERE, "signal.json")
# 信号历史归档（反馈闭环）：每次 signal 追加一条快照，供 report 回放检验 alpha。
# 与 signal.json 不同，此文件长期保留、不被 _cleanup_temp 删除。
SIGNAL_LOG_PATH = os.path.join(HERE, "signal_log.jsonl")
# 成交归档：每次真实下单后追加一条记录，与 signal_log 配合复盘「信号→下单→盈亏」。
TRADE_LOG_PATH = os.path.join(HERE, "trade_log.jsonl")

# ==================== 消息面因子（可选，mx-search） ====================
# 可选的新闻情绪 overlay：按股债分组各查一个 broad query，用关键词规则算组级情绪，
# 以 W_NEWS 权重调整综合得分。未配置 MX_APIKEY 或未安装 mx-search 时自动降级为纯量化。
# W_NEWS / 关键词表 / 查询语句都是可调参数，使用者应自行验证后再信任该 overlay。
MX_SKILL_DIR = os.environ.get("CFFEX_MX_SKILL_DIR", os.path.expanduser("~/skills/mx-search"))
W_NEWS = 0.15                          # 消息面权重（建议 0~0.25，越高越激进）
EQUITY_QUERY = "A股 股指期货 市场情绪 政策面 今日"
BOND_QUERY = "国债期货 利率 央行货币政策 今日"
NEWS_CACHE_PATH = os.path.join(HERE, ".news_cache.json")  # 按日期缓存，避免同日重复调用
NEWS_MAX_AGE_DAYS = 3                  # 资讯时效过滤：只统计近 N 天的资讯（旧闻/科普文不计入情绪）

# 股指组：正面词加分、负面词减分（示例词表，可自行调整）
EQUITY_POSITIVE = ["上涨", "上行", "利好", "宽松", "反弹", "回升", "支持", "提振", "走强", "放量"]
EQUITY_NEGATIVE = ["下跌", "下行", "利空", "收紧", "回调", "承压", "走弱", "缩量", "风险", "疲软"]
# 国债组：降息/宽松/收益率下行 → 国债期货利好（多头加分）；加息/紧缩/收益率上行 → 利空
BOND_POSITIVE = ["降息", "宽松", "债牛", "收益率下行", "利率下行", "买入", "走强", "避险"]
BOND_NEGATIVE = ["加息", "紧缩", "债熊", "收益率上行", "利率上行", "抛售", "走弱", "收紧"]

# 上游依赖路径（均可用环境变量覆盖，见 doctor 自检）
FUTURES_TRADER_DIR = os.environ.get("CFFEX_FUTURES_TRADER_DIR",
                                    os.path.expanduser("~/.claude/skills/futures-trader"))
DFCFQH_SCRIPT = os.environ.get("CFFEX_DFCFQH_SCRIPT",
                               os.path.expanduser("~/.claude/skills/dfcfqh-zjsbsimrace/zjsbsimrace.py"))
DFCFQH_OUTPUT_DIR = os.environ.get("CFFEX_DFCFQH_OUTPUT_DIR",
                                   os.path.expanduser("~/.openclaw/workspace/qh_data/output"))
API_URL = os.environ.get("API_URL", "https://qhsim.eastmoney.com/simCustomCompeteApi")

# ==================== 保证金折算配置 ====================
# 中金所品种的合约乘数与交易所保证金比例（近似标准值，请按交易所最新公告核对调整）。
# 每手保证金 = 价格 × 乘数 × 保证金比例；手数 = floor(每腿预算 / 每手保证金)。
SPEC = {
    "IF0": {"multiplier": 300,   "margin_rate": 0.12},
    "IH0": {"multiplier": 300,   "margin_rate": 0.12},
    "IC0": {"multiplier": 200,   "margin_rate": 0.14},
    "IM0": {"multiplier": 200,   "margin_rate": 0.12},
    "TS0": {"multiplier": 20000, "margin_rate": 0.005},
    "TF0": {"multiplier": 10000, "margin_rate": 0.012},
    "T0":  {"multiplier": 10000, "margin_rate": 0.02},
    "TL0": {"multiplier": 10000, "margin_rate": 0.035},
}
TARGET_USE_RATE = 0.90   # 总保证金使用率目标（激进档；保守可调低到 0.5~0.7）
N_LEGS = 4               # 多空各 2 腿，策略等权
DEFAULT_EQUITY = 1_000_000.0   # 账户初始权益，signal 阶段预估用（orders 阶段用真实权益重算）

# ==================== 现货底仓 & 风险对冲（可选） ====================
# 若账户持有固定现货底仓（比赛账户为 沪深300ETF + 国债现券），现货是天然 β 多头：
#   沪深300ETF   → 权益 β（对应股指期货 IF）
#   22付息国债10 → 利率 β（对应国债期货 T）
# 对冲优先规则：期货买开与现货 β 同源的品种(IF/T) 会加倍该 β 风险 → 跳过；
#               期货卖开腿天然对冲现货多头 β → 保留，每腿 1 手最小对冲单位。
# 纯期货账户（无现货）可加 --no-hedge 直接跑 α 满仓轮动。
SPOT_BETA = {
    "IF0": {"spot": "沪深300ETF", "beta": "权益"},
    "T0":  {"spot": "22付息国债10", "beta": "利率"},
}

# ==================== 激进模式（可选的满仓轮动档） ====================
# 激进模式（--aggressive 或按日期自动触发）：关闭对冲、纯 α 全 4 腿满仓轮动。
#   1) 关闭对冲 —— 放弃 β 中性，追求纯 α 敞口（做多前2/做空后2 全 4 腿）。
#   2) 保证金使用率拉满到 AGGRESSIVE_USE_RATE —— 满仓博收益。
#      手数折算按 min(目标, AGGRESSIVE_USE_CAP) 实际执行：预留 ~8% 安全垫，
#      避免成交后使用率越过 STOP_LOSS_USE_RATE(95%) 立即触发自动止损全平。
# 比赛场景：把下面两个日期改成你的比赛窗口（初赛首日 / 末日），最后 3 个
# 调仓周一（窗口内）自动切激进模式。非比赛用户：PRELIM_START 置成很远的
# 未来日期即可等效永久关闭自动激进，需要时用 --aggressive 手动开。
PRELIM_START = datetime.date(2026, 9, 10)
PRELIM_END = datetime.date(2026, 11, 20)
AGGRESSIVE_USE_RATE = 1.0   # 激进模式保证金使用率目标（满仓，收益优先）
AGGRESSIVE_USE_CAP = 0.92   # 折算安全垫：实际按 min(目标, 92%) 计算手数，避开 95% 止损线


def _rebalance_mondays(start, end):
    """返回 [start, end] 区间内的所有周一（调仓日）。"""
    d = start
    out = []
    while d <= end:
        if d.weekday() == 0:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def _aggressive_from():
    """激进模式起始日 = 初赛窗口最后 3 个调仓周一的第一个。"""
    ms = _rebalance_mondays(PRELIM_START, PRELIM_END)
    return ms[-3] if len(ms) >= 3 else PRELIM_END


def _is_aggressive(today=None):
    """today >= 激进窗口第一天（赛程最后 3 个调仓周一的第一个）时进入激进模式。"""
    today = today or datetime.date.today()
    return today >= _aggressive_from()


# ==================== 软件侧止损（总亏损止损） ====================
# 模拟赛接口无交易所侧条件单/止损单，只能用软件盯盘实现：
# 周期性查询期货账户总浮亏，达到止损线时自动平掉全部期货持仓
# （空头买平/多头卖平），止损状态持久化到本地 JSON。启用前请确认你接受
# 「自动平仓绕过逐笔人工确认」这一行为，并保持盯盘任务在线。
STOP_STATE_PATH = os.path.join(HERE, "stop_state.json")
STOP_LOSS_FUTURE_PNL = -80_000.0   # 期货账户总浮亏 ≤ -8 万 触发止损（可调）
STOP_LOSS_USE_RATE = 0.95          # 或 资金使用率 ≥ 95% 触发（双条件任一即触发）


def _need_cfapikey():
    return os.environ.get("CFAPIKEY", "")


def _dfcfqh_api(payload):
    """调用模拟交易 API，返回解析后的 JSON dict（失败返回 None）。
    用 requests 直连（省去 subprocess 拉起 curl 的进程开销）。"""
    key = _need_cfapikey()
    if not key:
        return None
    try:
        import requests
        resp = requests.post(
            f"{API_URL}/api/skills/action",
            headers={"CFAPIKEY": key, "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        return json.loads(resp.content.decode("utf-8"))
    except Exception:
        return None


OPER_SIDE_TO_TYPE = {"买开": "0", "卖开": "1", "买平": "2", "卖平": "3"}


def _place_order(contract, side_or_oper, price, lots):
    """直接调用模拟交易下单接口（绕过自然语言脚本，省一次 subprocess 往返）。
    side_or_oper：中文方向（买开/卖开/买平/卖平）或 operType（"0"~"3"）。
    返回 (ok, msg)。"""
    oper = OPER_SIDE_TO_TYPE.get(side_or_oper, str(side_or_oper))
    payload = {
        "action": "order_submission",
        "market": "CFFEX",
        "operType": oper,
        "futcode": contract,
        "wtjg": str(price),
        "wtsl": str(int(lots)),
    }
    resp = _dfcfqh_api(payload)
    if not resp:
        return False, "请求失败（回执不明确，请查委托确认）"
    if resp.get("status") == 0:
        return True, resp.get("msg", "下单成功")
    return False, resp.get("msg", "下单失败")


# 执行记录文档路径（每次下单后自动同步；可按个人习惯改路径或清空禁用）
TRADE_RECORD_MD = os.environ.get(
    "CFFEX_TRADE_RECORD_MD",
    os.path.expanduser("~/Desktop/cffex-trader-records/交易执行记录.md"))


def _sync_trade_record():
    """调仓后自动同步交易记录到 交易执行记录.md：
    追加当日小节（当日委托/最新持仓/账户快照），同日重复调用幂等覆盖。失败只警告，不影响交易。"""
    try:
        acc = _dfcfqh_api({"action": "query_account_detail"})
        hold = _dfcfqh_api({"action": "query_account_holdings"})
    except Exception as e:
        print(f"[WARN] 交易记录同步查询失败（不影响下单）: {e}")
        return
    if not acc or not hold:
        print("[WARN] 交易记录同步查询失败（不影响下单）: 回执不明确")
        return

    today = datetime.date.today().strftime("%Y-%m-%d")
    now_hm = datetime.datetime.now().strftime("%H:%M")

    # 当日订单汇总（来自 trade_log.jsonl 今日条目）
    todays = []
    try:
        with open(TRADE_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get("ts", "").startswith(today):
                    todays.append(r)
    except Exception:
        pass

    lines = []
    try:
        # useRate/totalRate 可能是 '4.90%'（已除 100）或小数（直接用）
        def _rate(v):
            s = str(v).strip()
            return float(s.rstrip("%")) / 100 if s.endswith("%") else float(v or 0)
        result = acc.get("result", {})
        fa = result.get("futureAccountDetail", {})
        equity = _rate(fa.get("currentEquity") or 0)
        use_rate = _rate(fa.get("useRate") or 0)
        total_rate = _rate(fa.get("totalRate") or 0)
        ga = result.get("goodsAccountDetail", {})
        goods_if = float(ga.get("goodsIfAmount") or 0)
        goods_tm = float(ga.get("goodsTmAmount") or 0)

        pos_list = hold.get("result", {}).get("futureAccountHoldings", []) or []
        fut_rows = []
        for p in pos_list:
            fut_rows.append("| {} | {} | {} | {} → {} | {:+,.0f} |".format(
                p.get("futname", "?"),
                "空" if p.get("mmfx") == "空" else "多",
                p.get("ccsl"), p.get("kcjj"), p.get("price"),
                float(p.get("fdyk") or 0)))

        block = [f"## {today}（自动同步 · {now_hm}）", ""]
        block.append(f"**期货账户**：权益 {equity:,.0f}，总收益率 {total_rate * 100:+.2f}%，"
                     f"资金使用率 {use_rate * 100:.1f}%。")
        block.append(f"**现货**：沪深300ETF {goods_if:,.0f}、22付息国债10 {goods_tm:,.0f}。")
        block.append("")
        if todays:
            block.append("**今日委托**（trade_log 自动归档）：")
            block.append("")
            block.append("| 时间 | 合约 | 方向 | 委托价 | 手数 | 结果 |")
            block.append("|---|---|---|---|---|---|")
            for r in todays:
                block.append("| {} | {} | {} | {} | {} | {} |".format(
                    (r.get("ts", "") or "")[-8:-3] or "--:--",
                    r.get("contract"), r.get("side"), r.get("price"),
                    r.get("lots"), "回执OK" if r.get("ok") else "失败：" + str(r.get("msg"))))
            block.append("")
        if fut_rows:
            block.append("**最新持仓**：")
            block.append("")
            block.append("| 合约 | 方向 | 手数 | 成本→现价 | 浮盈亏 |")
            block.append("|---|---|---|---|---|")
            block.extend(fut_rows)
            block.append("")
        new_section = "\n".join(block)
    except Exception as e:
        print(f"[WARN] 交易记录内容组装失败（不影响下单）: {e}")
        return

    # 同日小节已存在则先移除（幂等），新小节插到「策略时间线备忘」标题前；没有锚点则追加末尾。
    # 小节终止符：下一个 "---" 分隔线 / 下一个 Markdown 标题 / 文件尾，任一先行即止。
    try:
        content = ""
        if os.path.exists(TRADE_RECORD_MD):
            with open(TRADE_RECORD_MD, encoding="utf-8") as f:
                content = f.read()

        sec_re = re.compile(
            r"\n(?:-{3,}[^\S\n]*\n*)?"          # 可选的前置 --- 分隔线一并移除
            r"## " + re.escape(today) + r"（自动同步"
            r"[\s\S]*?(?=\n-{3,}|\n#{1,3} |\Z)"
        )
        content = sec_re.sub("", content)

        section = new_section.rstrip()
        anchor = content.find("## 策略时间线备忘")
        if anchor >= 0:
            head, tail = content[:anchor].rstrip(), content[anchor:].lstrip("\n")
            sep = "\n\n" if head.endswith("---") else "\n\n---\n\n"
            new_content = head + sep + section + "\n\n" + tail
        else:
            new_content = content.rstrip() + "\n\n---\n\n" + section + "\n"
        with open(TRADE_RECORD_MD, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"[归档] 交易记录已同步到 {TRADE_RECORD_MD}")
    except Exception as e:
        print(f"[WARN] 交易记录写入失败（不影响下单）: {e}")


def _append_trade_log(contract, side, price, lots, ok, msg):
    """真实下单后追加成交记录到 trade_log.jsonl（长期保留，供复盘）。"""
    try:
        rec = {
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "contract": contract,
            "side": side,
            "price": str(price),
            "lots": int(lots),
            "ok": bool(ok),
            "msg": msg,
        }
        with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[WARN] 成交归档失败（不影响下单）: {e}")


def _query_equity():
    """查询期货账户当前权益，失败返回 None（回退默认值）。"""
    resp = _dfcfqh_api({"action": "query_account_detail"})
    if not resp or resp.get("status") != 0:
        return None
    try:
        return float(resp["result"]["futureAccountDetail"]["currentEquity"])
    except (KeyError, TypeError, ValueError):
        return None


def _compute_lots(orders, equity, use_rate=TARGET_USE_RATE):
    """按保证金折算每腿手数，保证全部下单可完成：
      1) 策略等权：每腿目标预算 = 权益×使用率÷腿数，手数 = floor(预算÷每手保证金)，
         各腿预算相等、手数随每手保证金高低自然分配（符合"比例以策略为准"）；
      2) 每腿至少 1 手（floor 成 0 的腿强制 1 手，杜绝漏单导致多空失衡）；
      3) 兜底校验：若 4 腿各 1 手的最低总保证金仍超过预算，打印醒目警告，但不丢腿。
    返回 {contract: (lots, margin_per_lot)}。"""
    budget = equity * use_rate
    per_leg = budget / N_LEGS
    out = {}
    for o in orders:
        spec = SPEC.get(o["symbol"])
        margin = (
            float(o["price_ref"]) * spec["multiplier"] * spec["margin_rate"] if spec else 0.0
        )
        lots = max(1, int(per_leg // margin)) if margin > 0 else 1
        out[o["contract"]] = (lots, margin)
    total = sum(m * l for l, m in out.values())
    if total > budget:
        print(f"\n[警告] 4 腿各 1 手的最低保证金 {total:,.0f} 已超过预算 {budget:,.0f}，"
              f"账户可能无法同时开满 4 腿，下单或遭拒（资金不足），请核对权益。")
    return out


def _margin_per_lot(o):
    """单腿每手保证金 = 价格 × 乘数 × 保证金比例（未知品种返回 0）。"""
    spec = SPEC.get(o["symbol"])
    return float(o["price_ref"]) * spec["multiplier"] * spec["margin_rate"] if spec else 0.0


def _apply_hedge(orders):
    """对冲优先：剔除与现货底仓 β 同源的做多腿。
    现货 = 沪深300ETF(权益多头) + 22付息国债10(利率多头)，固定不可交易。
    期货买开同源品种(IF/T) 会加倍现货 β 风险 → 跳过；
    期货卖开腿 天然对冲现货多头 β → 全部保留。
    返回 (kept_orders, skipped_orders)。"""
    kept, skipped = [], []
    for o in orders:
        if o.get("operType") == "0" and o["symbol"] in SPOT_BETA:
            skipped.append(o)
        else:
            kept.append(o)
    return kept, skipped


_SIDE_TO_MMFX = {"买开": "多", "卖开": "空"}


def _diff_against_positions(orders, lots_map):
    """把「目标委托清单」转成「相对当前持仓的增量动作」。

    返回 (to_place, to_close, skipped_existing, positions_known)。

    to_place：开仓/加仓单 [(o, lots, tag)]，o 为 signal 委托（买开/卖开）。
    to_close：平仓单 [(c, tag)]，c 为平仓委托（买平/卖平，含 contract/order_side/operType/lots/price_ref/name）。
      两类平仓：① 持仓里但不在目标清单的品种（旧腿，每日调仓要平掉多余敞口）；
                ② 持仓方向与目标相反的品种（先平旧仓，再反向开新仓）。
    skipped_existing：已同向到位、无需动的腿。
    positions_known=False 表示持仓查询失败，退化为全量目标（平仓单为空）。"""
    hold = _dfcfqh_api({"action": "query_account_holdings"})
    positions_known = bool(hold and hold.get("status") == 0)
    positions = {}   # sym -> (mmfx, lots, futcode, name, price)
    if positions_known:
        for p in (hold.get("result") or {}).get("futureAccountHoldings") or []:
            name = p.get("futname") or "?"
            sym = _symbol_from_futname(name)
            futcode = eng.MAIN_CONTRACT.get(sym)   # 中文 futname 无 ASCII 字母，需经连续代码映射带月份合约
            if not sym or not futcode:
                continue
            # 关键：用持仓 futname 里的实际月份还原合约（换月过渡期持仓可能在旧月）
            futcode = _restore_held_contract(sym, name, futcode)
            mmfx = p.get("mmfx") or ""
            try:
                lots = int(float(p.get("ccsl") or 0))
            except (TypeError, ValueError):
                lots = 0
            positions[sym] = (mmfx, lots, futcode, name, p.get("price") or "0")

    def _close_order(sym, mmfx, lots, futcode, name, price, tag):
        close_side = "卖平" if mmfx == "多" else "买平"
        oper = "3" if mmfx == "多" else "2"
        return ({"symbol": sym, "name": name, "contract": futcode,
                 "order_side": close_side, "operType": oper,
                 "lots": lots, "price_ref": price}, tag)

    to_place, to_close, skipped_existing = [], [], []
    target_syms = {o["symbol"] for o in orders}

    # 反向：目标清单外的持仓 → 平掉旧腿（每日调仓的关键：不平会越积越多）
    for sym, (mmfx, lots, futcode, name, price) in positions.items():
        if sym in target_syms:
            continue
        to_close.append(_close_order(sym, mmfx, lots, futcode, name, price, "平仓（旧腿）"))

    # 正向：目标清单内 → 开仓/加仓/跳过/反方向（先平后开）
    for o in orders:
        sym = o["symbol"]
        target_lots = lots_map[o["contract"]][0]
        target_mmfx = _SIDE_TO_MMFX.get(o["order_side"])
        if target_lots <= 0:
            continue
        cur = positions.get(sym)
        if not cur:
            to_place.append((o, target_lots, "开仓"))
            continue
        cur_mmfx, cur_lots, futcode, name, price = cur
        if cur_mmfx == target_mmfx:
            if cur_lots >= target_lots:
                skipped_existing.append((o, cur_lots))
            else:
                delta = target_lots - cur_lots
                to_place.append((o, delta, f"加仓 +{delta}"))
        else:
            to_close.append(_close_order(sym, cur_mmfx, cur_lots, futcode, name, price, "平仓（方向反转）"))
            to_place.append((o, target_lots, "开仓"))
    return to_place, to_close, skipped_existing, positions_known


def _cleanup_temp():
    """删除决策过程产生的临时数据：dfcfqh 输出文件 + signal.json。"""
    removed = 0
    if os.path.isdir(DFCFQH_OUTPUT_DIR):
        for fn in os.listdir(DFCFQH_OUTPUT_DIR):
            if fn.startswith("qh_moni_"):
                try:
                    os.remove(os.path.join(DFCFQH_OUTPUT_DIR, fn))
                    removed += 1
                except OSError:
                    pass
    if os.path.exists(SIGNAL_PATH):
        try:
            os.remove(SIGNAL_PATH)
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"\n[清理] 已删除 {removed} 个临时数据文件（dfcfqh 输出 + signal.json）。")


# ---------- ① update ----------
def cmd_update():
    """更新期货数据：调 futures-trader 的 FuturesUpdater.update_all_ohlc。"""
    _require_engine()
    if not os.path.isdir(FUTURES_TRADER_DIR):
        print(f"[ERROR] 未找到 futures-trader: {FUTURES_TRADER_DIR}")
        sys.exit(1)
    sys.path.insert(0, FUTURES_TRADER_DIR)
    try:
        from backend.updater import FuturesUpdater
    except ImportError as e:
        print(f"[ERROR] 无法导入 futures-trader（akshare 依赖？）: {e}")
        print(f"请确认 {FUTURES_TRADER_DIR} 内的 backend 包可用，且已安装 akshare。")
        sys.exit(1)

    u = FuturesUpdater()
    try:
        print(f"更新 {len(eng.SYMBOLS)} 个品种: {', '.join(eng.SYMBOLS)}")
        results = u.update_all_ohlc(symbols=eng.SYMBOLS)
        print("\n更新结果（symbol -> 新增/更新条数，<0 表示失败）:")
        for s, c in results.items():
            flag = "失败" if c < 0 else f"{c} 条"
            print(f"  {s}: {flag}")
    finally:
        u.close()


# ---------- ② signal ----------
def _load_news_cache():
    """读消息面缓存（{date: {group: sentiment}}），无/损坏返回 {}。"""
    if not os.path.exists(NEWS_CACHE_PATH):
        return {}
    try:
        with open(NEWS_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_news_cache(cache):
    try:
        with open(NEWS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] 消息面缓存写入失败（不影响下单）: {e}")


def _mx_search_raw(query):
    """调 mx-search 返回文本，失败返回 None。"""
    try:
        sys.path.insert(0, MX_SKILL_DIR)
        from mx_search import MXSearch
        mx = MXSearch()
        result = mx.search(query)
        return mx.format_pretty(result)
    except Exception as e:
        print(f"[WARN] 消息面查询失败（{query[:12]}...）: {e}")
        return None


def _filter_stale_news(text):
    """时效过滤：format_pretty 输出按「--- N. 标题 ---」分条，每条有「日期: YYYY-MM-DD」。
    只保留近 NEWS_MAX_AGE_DAYS 天的资讯；无日期或解析失败的条目一并剔除（多为科普文/旧闻）。"""
    if not text:
        return text
    today = datetime.date.today()
    cutoff = datetime.timedelta(days=NEWS_MAX_AGE_DAYS)
    keep, total = [], 0
    blocks = re.split(r"\n(?=--- )", text)
    header = blocks[0] if blocks and not blocks[0].startswith("--- ") else ""
    for block in blocks[1:] if header else blocks:
        if not block.startswith("--- "):
            continue
        total += 1
        m = re.search(r"日期:\s*(\d{4})-(\d{2})-(\d{2})", block)
        if not m:
            continue
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if (today - d) <= cutoff:
            keep.append(block)
    if total == 0:  # 格式意外（非 format_pretty 输出），不误杀，原样返回
        return text
    kept = "\n".join(keep)
    dropped = total - len(keep)
    if dropped > 0 or len(keep) < total:
        print(f"  [消息面] 时效过滤：{total} 条资讯，保留近 {NEWS_MAX_AGE_DAYS} 天 {len(keep)} 条，剔除过期/无日期 {dropped}+{total - len(keep) - dropped} 条")
    return header + ("\n" + kept if kept else "")


def _sentiment_from_text(text, positive, negative):
    """规则关键词情感：统计正/负面词命中，映射到 [-1, 1]。无命中返回 0。"""
    if not text:
        return 0.0
    pos = sum(1 for w in positive if w in text)
    neg = sum(1 for w in negative if w in text)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 4)


def news_overlay(sig, use_news=True):
    """消息面 overlay：给 sig 增加 news_sentiment / news_rank 列，并重算 score。
    sig：引擎返回的 DataFrame（已含 vol_rank/mom_rank/carry_rank/score）。
    返回调整后的 sig（含 raw_score 保留原值）。"""
    # 记原始得分
    sig["raw_score"] = sig["score"]

    # 组映射
    equity_syms = set(eng.INDEX_SYMBOLS) if hasattr(eng, "INDEX_SYMBOLS") else \
        {"IF0", "IH0", "IC0", "IM0"}
    bond_syms = set(eng.BOND_SYMBOLS) if hasattr(eng, "BOND_SYMBOLS") else \
        {"TS0", "TF0", "T0", "TL0"}

    if not use_news:
        sig["news_sentiment"] = 0.0
        sig["news_rank"] = (1 + len(sig)) / 2.0
        return sig

    cache = _load_news_cache()
    today = str(datetime.date.today())
    if today not in cache:
        cache[today] = {}
    eq_text = _filter_stale_news(_mx_search_raw(EQUITY_QUERY))
    bond_text = _filter_stale_news(_mx_search_raw(BOND_QUERY))
    eq_avail = eq_text is not None and eq_text.strip()      # 查询失败/全部过期则整组退化为纯量化，不稀释
    bond_avail = bond_text is not None and bond_text.strip()
    eq_sent = _sentiment_from_text(eq_text, EQUITY_POSITIVE, EQUITY_NEGATIVE)
    bond_sent = _sentiment_from_text(bond_text, BOND_POSITIVE, BOND_NEGATIVE)
    cache[today]["equity"] = eq_sent if eq_avail else None
    cache[today]["bond"] = bond_sent if bond_avail else None
    _save_news_cache(cache)

    # 打印消息面概要
    print("=" * 64)
    print("消息面情绪（mx-search）")
    print("=" * 64)
    if eq_avail:
        print(f"  股指组:  {eq_sent:+.2f}  {'多头偏好' if eq_sent > 0 else ('空头偏好' if eq_sent < 0 else '中性')}")
    else:
        print(f"  股指组:  查询失败，退化为纯量化")
    if bond_avail:
        print(f"  国债组:  {bond_sent:+.2f}  {'多头偏好' if bond_sent > 0 else ('空头偏好' if bond_sent < 0 else '中性')}")
    else:
        print(f"  国债组:  查询失败，退化为纯量化")

    # 组内映射：同组品种用同一 sentiment，作为 news_rank 基准。
    # 中性(0) 映射到中间值；正映射到高值，负映射到低值。
    n = len(sig)
    def sent_to_rank(s):
        return 1 + (s * 0.5 + 0.5) * (n - 1)  # [-1,1] -> [1,n]
    sent_map = {}
    for i, r in sig.iterrows():
        if r["symbol"] in equity_syms:
            sent_map[r["symbol"]] = eq_sent if eq_avail else 0.0
        elif r["symbol"] in bond_syms:
            sent_map[r["symbol"]] = bond_sent if bond_avail else 0.0
        else:
            sent_map[r["symbol"]] = 0.0
    sig["news_sentiment"] = sig["symbol"].map(sent_map)
    sig["news_rank"] = sig["news_sentiment"].map(sent_to_rank)

    # 重算综合得分：原量化因子权重不变，新增消息面项。
    # 查询失败的组完全退化为 raw_score（不被中性 news_rank 稀释）。
    q = 1.0 - W_NEWS
    sig["score"] = sig["raw_score"].astype(float)
    if eq_avail:
        m = sig["symbol"].isin(equity_syms)
        sig.loc[m, "score"] = q * sig.loc[m, "raw_score"] + W_NEWS * sig.loc[m, "news_rank"]
    if bond_avail:
        m = sig["symbol"].isin(bond_syms)
        sig.loc[m, "score"] = q * sig.loc[m, "raw_score"] + W_NEWS * sig.loc[m, "news_rank"]
    return sig


def cmd_signal(use_news=True):
    """跑策略引擎，输出多空信号并写 signal.json。"""
    _require_engine()
    try:
        sig, longs, shorts = eng.generate_signals()
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    # 消息面 overlay：加入 news_sentiment/news_rank，重算 score 并重新排序
    sig = news_overlay(sig, use_news)
    sig = sig.sort_values("score", ascending=False).reset_index(drop=True)
    n_long = len(longs)
    n_short = len(shorts)
    longs = list(sig.head(n_long)["symbol"])
    shorts = list(sig.tail(n_short)["symbol"])

    # 换月检测（信号阶段软提示）：映射失效/临近到期/持仓跨月 都在这里提前报警
    _, roll_warnings = check_contract_rollover(orders=None, block=False)
    if roll_warnings:
        print("=" * 64)
        print("换月检测（提示，不阻断）")
        print("=" * 64)
        for w in roll_warnings:
            print(f"  {w}")
        print()

    sig["rank"] = range(1, len(sig) + 1)
    sig["side"] = sig["symbol"].apply(
        lambda s: "long" if s in longs else ("short" if s in shorts else ""))
    sig["contract"] = sig["symbol"].map(eng.MAIN_CONTRACT)
    sig["order_side"] = sig["side"].map({"long": "买开", "short": "卖开"})

    cols = ["rank", "symbol", "name", "close", "momentum", "volatility",
            "carry", "carry_status", "raw_score", "news_sentiment", "score",
            "side", "contract"]
    pd = __import__("pandas")
    pd.set_option("display.float_format", lambda v: f"{v:.4f}")
    print("=" * 64)
    print("多因子截面轮动信号")
    print("=" * 64)
    print(sig[cols].to_string(index=False))
    print()
    print("做多: " + ", ".join(f"{s}({eng.NAMES[s]})" for s in longs))
    print("做空: " + ", ".join(f"{s}({eng.NAMES[s]})" for s in shorts))

    # 写 signal.json（含下单需要的全部字段 + 预估手数 + 消息面字段）
    orders = [
        {
            "symbol": r["symbol"],
            "name": r["name"],
            "contract": r["contract"],
            "order_side": r["order_side"],
            "operType": "0" if r["side"] == "long" else "1",
            "price_ref": round(float(r["close"]), 2),
            "news_sentiment": round(float(r["news_sentiment"]), 4),
        }
        for _, r in sig.iterrows() if r["side"] in ("long", "short")
    ]
    lots_map = _compute_lots(orders, DEFAULT_EQUITY)
    for o in orders:
        o["lots"] = lots_map[o["contract"]][0]
        o["margin_per_lot"] = round(lots_map[o["contract"]][1], 2)
    payload = {
        "generated_note": "多因子截面轮动信号 + 消息面（mx-search），下单前请人工确认",
        "longs": longs,
        "shorts": shorts,
        "use_news": bool(use_news),
        "orders": orders,
    }
    with open(SIGNAL_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n已写信号文件: {SIGNAL_PATH}")

    _append_signal_log(sig, longs, shorts)
    return payload


def _append_signal_log(sig, longs, shorts):
    """把本次信号快照追加到 signal_log.jsonl，供 report 回放检验 alpha。
    每条记录：日期 + 8 品种的 close/score/side，用于算「信号日 → 最新日」的实际涨跌。"""
    try:
        asof = str(sig["last_date"].max())
        rows = []
        for _, r in sig.iterrows():
            rows.append({
                "symbol": r["symbol"],
                "name": r["name"],
                "close": round(float(r["close"]), 4),
                "score": round(float(r["score"]), 4),
                "news_sentiment": round(float(r.get("news_sentiment", 0.0)), 4),
                "side": "long" if r["symbol"] in longs else ("short" if r["symbol"] in shorts else "flat"),
            })
        rec = {"asof": asof, "longs": longs, "shorts": shorts, "rows": rows}

        # 去重：同一天反复 signal 只保留最后一条（覆盖最后一行，避免 0 收益无效区间）
        if os.path.exists(SIGNAL_LOG_PATH):
            with open(SIGNAL_LOG_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            if lines:
                try:
                    last = json.loads(lines[-1])
                    if last.get("asof") == asof:
                        lines[-1] = json.dumps(rec, ensure_ascii=False) + "\n"
                        with open(SIGNAL_LOG_PATH, "w", encoding="utf-8") as f:
                            f.writelines(lines)
                        print(f"[归档] 信号快照已更新（覆盖同日 {asof}）。")
                        return
                except ValueError:
                    pass

        with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[归档] 信号快照已追加到 signal_log.jsonl（当前 {asof}）。")
    except Exception as e:
        print(f"[WARN] 信号归档失败（不影响下单）: {e}")


def _load_signal_log():
    """读信号归档，返回按日期升序的记录列表。"""
    recs = []
    if not os.path.exists(SIGNAL_LOG_PATH):
        return recs
    with open(SIGNAL_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
    recs.sort(key=lambda r: r.get("asof", ""))
    return recs


def _latest_close_map():
    """从数据库读每个 symbol 的最新收盘价，返回 {symbol: close}。"""
    import sqlite3
    if not os.path.exists(eng.DB_PATH):
        return {}
    conn = sqlite3.connect(eng.DB_PATH)
    out = {}
    try:
        for s in eng.SYMBOLS:
            df = __import__("pandas").read_sql_query(
                "SELECT close FROM futures_ohlc WHERE symbol=? ORDER BY date DESC LIMIT 1",
                conn, params=(s,))
            if not df.empty:
                out[s] = float(df["close"].iloc[0])
    finally:
        conn.close()
    return out


# ---------- 部署自检（doctor） ----------
def cmd_doctor():
    """首次部署自检：逐项检查 Python 依赖 / 环境变量 / 上游 skill / 数据库 /
    可交易接口 / 主力合约映射，输出修复指引，并生成个性化部署教程 GETTING_STARTED.md。
    只读检查，不下单、不改数据。"""
    import importlib
    import datetime as _dt

    print("=" * 64)
    print("cffex-trader 部署自检（doctor）")
    print("=" * 64)
    issues, optional_missing = [], []

    def check(name, ok, fix, optional=False):
        mark = "✅" if ok else ("🟡" if optional else "❌")
        print(f"  {mark} {name}")
        if not ok:
            (optional_missing if optional else issues).append((name, fix))

    # ⓪ 策略引擎（不随仓库分发，用户手动放入）
    print("\n[0/6] 策略引擎（用户自备）")
    check("futures_multifactor_engine.py 已放入本目录", ENGINE_AVAILABLE,
          "策略文件不随本仓库分发。获取后放入 orchestrator.py 同目录（"
          + HERE + "），详见 README.md「策略引擎获取」")
    if not ENGINE_AVAILABLE:
        print("      ⚠️  以下数据库/合约映射检查需要引擎文件，已跳过。")

    # ① Python 依赖
    print("\n[1/6] Python 依赖")
    for mod, pip_name, required in [
        ("pandas", "pandas", True),
        ("requests", "requests", True),
        ("akshare", "akshare", True),          # update 更新数据 + TS/TF YTM 回退
        ("tqsdk", "tqsdk", False),             # 可选：实时盘口对手价
        ("mx_search", None, False),            # 可选：消息面（独立 skill，非 pip 包）
    ]:
        try:
            importlib.import_module(mod)
            check(f"{mod}", True, "")
        except ImportError:
            if mod == "mx_search":
                check(f"{mod}（mx-search 消息面 skill）", False,
                      f"安装 mx-search skill 到 {MX_SKILL_DIR}（或设 CFFEX_MX_SKILL_DIR）；"
                      f"未安装则消息面自动降级为纯量化", optional=True)
            elif pip_name:
                check(mod, False, f"pip install {pip_name}", optional=not required)

    # ② 环境变量
    print("\n[2/6] 环境变量")
    check("CFAPIKEY（模拟交易下单必需）", bool(_need_cfapikey()),
          "export CFAPIKEY=<模拟赛/模拟账户的用户ID>；建议写入 ~/.bashrc")
    tq_ok = bool(os.environ.get("TQSDK_USER")) and bool(os.environ.get("TQSDK_PASS"))
    check("TQSDK_USER / TQSDK_PASS（实时盘口，可选）", tq_ok,
          "在 https://account.shinnytech.com/ 免费注册后 export；缺省则用收盘参考价下单", optional=True)
    check("MX_APIKEY（消息面，可选）", bool(os.environ.get("MX_APIKEY")),
          "在 mx-search skill 页面获取后 export；缺省则消息面降级纯量化", optional=True)

    # ③ 上游 skill
    print("\n[3/6] 上游 skill")
    check(f"futures-trader（{FUTURES_TRADER_DIR}）", os.path.isdir(FUTURES_TRADER_DIR),
          "安装 futures-trader skill（提供 init_db / OHLC 更新）；"
          "或设 CFFEX_FUTURES_TRADER_DIR 指向已有安装")
    check(f"dfcfqh 下单脚本（{DFCFQH_SCRIPT}）", os.path.exists(DFCFQH_SCRIPT),
          "安装 dfcfqh-zjsbsimrace skill（模拟赛账户查询/下单）；"
          "或设 CFFEX_DFCFQH_SCRIPT 指向脚本位置")
    check(f"mx-search（{MX_SKILL_DIR}）", os.path.isdir(MX_SKILL_DIR),
          f"安装 mx-search skill 到 {MX_SKILL_DIR}；缺省则消息面降级", optional=True)

    # ④ 本地数据库
    print("\n[4/6] 期货数据库")
    db_ok = ENGINE_AVAILABLE and os.path.exists(eng.DB_PATH)
    n_rows = 0
    last_dates = {}
    if not ENGINE_AVAILABLE:
        check("数据库检查（跳过：策略引擎未就位）", False,
              "先放入策略引擎文件，再重跑 doctor", optional=True)
    elif db_ok:
        try:
            import sqlite3 as _sq
            import pandas as _pd
            conn = _sq.connect(eng.DB_PATH)
            df = _pd.read_sql_query(
                "SELECT symbol, MAX(date) AS d, COUNT(*) AS n FROM futures_ohlc GROUP BY symbol",
                conn)
            conn.close()
            n_rows = int(df["n"].sum()) if not df.empty else 0
            last_dates = dict(zip(df["symbol"], df["d"]))
            enough = all(last_dates.get(s) for s in eng.SYMBOLS)
            check(f"数据库存在且 8 品种有数据（共 {n_rows} 行）", enough,
                  "运行 `python orchestrator.py update` 拉取 OHLC（需 ≥21 个交易日）")
        except Exception as e:
            check(f"数据库可读（异常: {type(e).__name__}）", False,
                  "检查 CFFEX_DB_PATH 或重建数据库（futures-trader init_db）")
    else:
        check(f"数据库存在（{eng.DB_PATH}）", False,
              "安装并初始化 futures-trader（init_db.py），然后运行 `python orchestrator.py update`")

    # ⑤ 模拟交易接口连通性
    print("\n[5/6] 模拟交易接口")
    if _need_cfapikey():
        r = _dfcfqh_api({"action": "query_futures_contracts"})
        check("账户接口连通（可交易合约查询）", bool(r and r.get("status") == 0),
              "检查 CFAPIKEY 是否有效、网络是否可达 " + API_URL)
    else:
        check("接口连通（跳过：未配置 CFAPIKEY）", False, "配置 CFAPIKEY 后重跑 doctor", optional=True)

    # ⑥ 主力合约映射 + 换月检测
    print("\n[6/6] 主力合约映射（换月检测）")
    if not ENGINE_AVAILABLE:
        check("换月检测（跳过：策略引擎未就位）", False,
              "放入引擎文件后重跑 doctor（MAIN_CONTRACT 定义在引擎内）", optional=True)
    elif _need_cfapikey():
        ok, warns = check_contract_rollover(orders=None, block=False)
        check("MAIN_CONTRACT 全部有效", ok,
              "按提示更新 futures_multifactor_engine.py 的 MAIN_CONTRACT")
        for w in warns:
            print(f"      {w}")
    else:
        exp = _contract_expiry(eng.MAIN_CONTRACT["IF0"])
        check(f"到期估算可用（如 IF 现映射 {eng.MAIN_CONTRACT['IF0']} 约 {exp} 交割）",
              exp is not None, "—", optional=True)
        print("      （配置 CFAPIKEY 后可做完整换月检测）")

    # 汇总
    print("\n" + "=" * 64)
    if not issues and not optional_missing:
        print("✅ 全部通过！可以直接开始使用：")
        print("   python orchestrator.py update && python orchestrator.py signal")
    elif not issues:
        print(f"✅ 核心功能可用；有 {len(optional_missing)} 项可选增强未配置（见上）。")
    else:
        print(f"❌ 有 {len(issues)} 项核心问题需修复：")
        for name, fix in issues:
            print(f"   - {name}\n     → {fix}")
    print("=" * 64)

    # 生成个性化部署教程（含当前缺失项与修复命令）
    _write_getting_started(issues, optional_missing, last_dates)


def _write_getting_started(issues, optional_missing, last_dates):
    """首次运行 doctor 时生成本地化部署教程 GETTING_STARTED.md（已存在则跳过）。"""
    import datetime as _dt
    path = os.path.join(HERE, "GETTING_STARTED.md")
    if os.path.exists(path):
        print(f"\n[教程] {path} 已存在，跳过生成。")
        return

    def _fix_lines(items):
        return "\n".join(f"- **{n}** → {f}" for n, f in items) or "- （无）"

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    content = f"""# cffex-trader 快速上手（自动生成于 {now}）

> 本教程由 `python orchestrator.py doctor` 根据你机器的**当前状态**生成。
> 全部修复后重跑 doctor 应显示「核心功能可用」或「全部通过」。

## 0. 放入策略引擎（第一步，必须）

**策略文件 `futures_multifactor_engine.py` 不随本仓库分发**，需手动放入
orchestrator.py 同一目录。没有它，update / signal / orders / report 均不可用
（查询与风控命令不受影响）。获取方式见 README.md「策略引擎获取」。

放入后可用 `python -c "import futures_multifactor_engine"` 验证可导入。

## 0.5 前置 skill（重要）

本工具是**编排层**，依赖以下上游 skill（Claude Code skills）：

| skill | 作用 | 必需? |
|---|---|---|
| `futures-trader` | 期货 OHLC 本地 SQLite（数据层） | ✅ 必需 |
| `dfcfqh-zjsbsimrace` | 模拟交易账户查询 + 下单（执行层） | ✅ 必需（真实下单时） |
| `mx-search` | 东方财富妙想资讯 → 消息面情绪因子（可选增强） | ⭕ 可选 |

把它们安装到 `~/.claude/skills/`（或用环境变量 `CFFEX_*` 指向自定义路径）。

## 1. 安装 Python 依赖

```bash
pip install pandas requests akshare
pip install tqsdk        # 可选：实时盘口对手价（推荐）
```

## 2. 配置环境变量（写入 ~/.bashrc 持久化）

```bash
export CFAPIKEY="<模拟赛/模拟账户 用户ID>"     # 必需：下单与查询
export TQSDK_USER="<快期账号>"                  # 可选：实时盘口
export TQSDK_PASS="<快期密码>"
export MX_APIKEY="<mx-search 的 key>"           # 可选：消息面因子
```

> 快期账户在 https://account.shinnytech.com/ 免费注册（仅用于行情）。

## 3. 当前待修复项（按 doctor 结果）

### 核心问题（修复前无法正常交易）

{_fix_lines(issues)}

### 可选增强（不影响核心流程）

{_fix_lines(optional_missing)}

## 4. 首次运行（三步）

```bash
python orchestrator.py update    # ① 拉取 8 品种 OHLC 到本地 SQLite
python orchestrator.py signal    # ② 生成多空信号（写 signal.json）
python orchestrator.py orders    # ③ 预览下单清单（dry-run，不下单）
python orchestrator.py orders --yes   # 确认后真实下单（还需输入 y 二次确认）
```

## 5. 日常使用与风控

- 每次下单后自动挂载软件侧止损（`stop_state.json`），`python orchestrator.py stop` 手动检查。
- `python orchestrator.py risk` 看爆仓/强平边界；`holdings` / `account` 查持仓与资金。
- `python orchestrator.py report` 回放信号命中率（积累 ≥2 个信号日后有意义）。
- **换月**：`signal`/`orders` 会自动检测主力合约到期与映射失效并报警，按提示更新
  `futures_multifactor_engine.py` 的 `MAIN_CONTRACT` 即可。
- 安全铁律：`orders` 默认只预览；交易失败后**不自动重试/改价/补单**，只查询状态。

## 6. 下一步建议

1. 先跑 `signal` + `orders`（dry-run）观察几天，熟悉输出。
2. 用 `report` 验证信号方向在你关注的品种上是否有效（因子方向样本期敏感）。
3. 按需调整 `W_NEWS`、`TARGET_USE_RATE`、`STOP_LOSS_*`、`SPOT_BETA` 等参数。
"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\n[教程] 已生成个性化部署教程: {path}")
    except OSError as e:
        print(f"\n[WARN] 教程写入失败: {e}")


def cmd_report():
    """回放信号归档，检验多空方向的实际表现（反馈闭环）。
    对每条信号：算「信号日 → 下一信号日（或最新）」的区间涨跌，
    做多腿涨=对、做空腿跌=对，汇总命中率与组合净收益。"""
    _require_engine()
    recs = _load_signal_log()
    if not recs:
        print("[ERROR] 暂无信号归档。先运行 `python orchestrator.py signal` 生成。")
        sys.exit(1)
    latest = _latest_close_map()

    print("=" * 64)
    print("信号反馈回放（多空方向命中检验）")
    print("=" * 64)

    total_long_hits = 0
    total_short_hits = 0
    total_long_n = 0
    total_short_n = 0
    total_ret = 0.0

    for i, rec in enumerate(recs):
        asof = rec.get("asof", "?")
        longs = rec.get("longs", [])
        shorts = rec.get("shorts", [])
        close_map = {r["symbol"]: r["close"] for r in rec.get("rows", [])}

        # 期末价：优先用下一信号日的 close（模拟持有到下次调仓），否则用最新价
        if i + 1 < len(recs):
            next_close_map = {r["symbol"]: r["close"] for r in recs[i + 1].get("rows", [])}
            horizon = f"→ {recs[i+1].get('asof','?')}"
        else:
            next_close_map = latest
            horizon = "→ 最新"

        print(f"\n[{asof}] {horizon}")
        leg_ret = []
        for sym in longs:
            c0, c1 = close_map.get(sym), next_close_map.get(sym)
            if not c0 or not c1:
                continue
            r = c1 / c0 - 1.0
            hit = r > 0
            total_long_n += 1
            total_long_hits += 1 if hit else 0
            leg_ret.append(r)
            print(f"  做多 {sym:4s}({eng.NAMES[sym]})  {r*100:+.2f}%  {'✓' if hit else '✗'}")
        for sym in shorts:
            c0, c1 = close_map.get(sym), next_close_map.get(sym)
            if not c0 or not c1:
                continue
            r = c1 / c0 - 1.0
            hit = r < 0   # 做空腿：价格跌=对
            total_short_n += 1
            total_short_hits += 1 if hit else 0
            leg_ret.append(-r)   # 做空盈亏 = 负的涨跌
            print(f"  做空 {sym:4s}({eng.NAMES[sym]})  {r*100:+.2f}%  {'✓' if hit else '✗'}")
        if leg_ret:
            total_ret += sum(leg_ret) / len(leg_ret)

    print("\n" + "=" * 64)
    print("汇总（等权多空腿，未扣手续费/滑点）")
    print("=" * 64)
    if total_long_n:
        print(f"  做多腿命中: {total_long_hits}/{total_long_n} = {total_long_hits/total_long_n:.0%}")
    if total_short_n:
        print(f"  做空腿命中: {total_short_hits}/{total_short_n} = {total_short_hits/total_short_n:.0%}")
    n_iv = len(recs) - 1
    if n_iv > 0:
        print(f"  平均每区间组合收益: {total_ret/n_iv*100:+.2f}%")
    print("\n说明：命中率>50% 表示方向有效；做多腿跌/做空腿涨 = 信号反向，需警惕。")


# ---------- ③ orders ----------
def _fmt_price(p):
    """价格转字符串，去尾零；无有效值返回 None。"""
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return f"{v:g}"


def _fetch_realtime_prices(contracts):
    """用 TqSdk 拉实时盘口（免费行情，需快期账户）。返回 {contract: {last, ask1, bid1}}。
    任一环节失败返回 {}，由调用方回退 price_ref（收盘参考价）。"""
    user = os.environ.get("TQSDK_USER", "").strip()
    pwd = os.environ.get("TQSDK_PASS", "").strip()
    if not user or not pwd:
        return {}
    try:
        from tqsdk import TqApi, TqAuth
        import time
    except ImportError:
        return {}
    out = {}
    api = None
    try:
        api = TqApi(auth=TqAuth(user, pwd))
        for c in contracts:
            try:
                q = api.get_quote("CFFEX." + c)
                api.wait_update(deadline=time.time() + 3)
                out[c] = {
                    "last": _fmt_price(q.last_price),
                    "ask1": _fmt_price(q.ask_price1),
                    "bid1": _fmt_price(q.bid_price1),
                }
            except Exception:
                out[c] = None
        api.close()
    except Exception:
        pass
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass
    return out


def _resolve_order_price(o, live):
    """按动作方向取对手价：买入（买开/买平）→卖一(ask1)，卖出（卖开/卖平）→买一(bid1)。
    无实时价回退 price_ref。返回 (委托价字符串, 实时价dict或None)。"""
    d = live.get(o["contract"]) or {}
    ask = d.get("ask1")
    bid = d.get("bid1")
    if ask and bid:
        side = o.get("order_side")
        if side in ("买开", "买平"):
            return ask, d
        if side in ("卖开", "卖平"):
            return bid, d
    return str(o["price_ref"]), d or None


def cmd_orders(execute=False, lots=None, force=False, hedge=True, aggressive=None):
    """读 signal.json，生成/执行 dfcfqh 下单命令。
    execute=True 才真正下单；force=True 跳过人工确认（供定时任务用）。
    hedge=True（默认）：对冲优先——剔除与现货 β 同源的做多腿，做空腿每腿 1 手对冲现货。
    lots：非对冲模式每腿手数（None=按保证金折算；传正数则统一指定）。
    aggressive：None=按日期自动判定；True/False 强制开/关激进模式（供演练）。
    激进模式（自动判定 today>=_aggressive_from()，或 --aggressive 强制开）：
      自动关闭对冲、满仓(使用率 1.0)走纯 α 全 4 腿，收益优先。"""
    _require_engine()
    if aggressive is None:
        aggressive = _is_aggressive()
    if aggressive:
        hedge = False   # 激进·收益优先：强制满仓 α，放弃对冲

    if not os.path.exists(SIGNAL_PATH):
        print("[ERROR] 未找到 signal.json，请先运行: python orchestrator.py signal")
        sys.exit(1)
    with open(SIGNAL_PATH, encoding="utf-8") as f:
        payload = json.load(f)

    orders = payload["orders"]

    # 换月检测（下单前强制）：映射失效 → 拒绝下单，提示先更新 MAIN_CONTRACT
    rollover_ok, roll_warnings = check_contract_rollover(orders=orders, block=True)
    for w in roll_warnings:
        print(w)
    if not rollover_ok:
        print("\n[ERROR] MAIN_CONTRACT 映射含已失效合约，拒绝下单。")
        print("请更新 futures_multifactor_engine.py 的 MAIN_CONTRACT 后重跑 signal → orders。")
        sys.exit(1)
    if roll_warnings:
        print()

    # 对冲优先：剔除与现货底仓 β 同源的做多腿（现货已是该 β 多头，期货再买开会加倍风险）
    use_est = False
    if hedge:
        orders, skipped = _apply_hedge(orders)
        if skipped:
            print("=" * 64)
            print("对冲调整：跳过与现货底仓 β 同源的做多腿（现货已是多头，期货再买开会加倍该 β 风险）")
            print("=" * 64)
            for o in skipped:
                spot = SPOT_BETA.get(o["symbol"], {})
                print(f"  - {o['order_side']} {o['contract']} ({o['name']})"
                      f"  【现货已有 {spot.get('spot', '')} {spot.get('beta', '')}多头】")
            print()
        if not orders:
            print("[ERROR] 对冲后无剩余可下单腿（信号做多腿全部与现货 β 同源）。")
            print("如需表达 α 而非纯对冲，可加 --no-hedge 走满仓轮动模式。")
            sys.exit(1)

    # 手数
    if hedge:
        # 对冲优先：每腿 1 手最小对冲单位（1 手名义已远超现货，避免过度对冲/过度杠杆）
        lots_map = {o["contract"]: (1, _margin_per_lot(o)) for o in orders}
    elif lots is None or lots <= 0:
        equity = _query_equity() or DEFAULT_EQUITY
        use_rate = min(AGGRESSIVE_USE_RATE, AGGRESSIVE_USE_CAP) if aggressive else TARGET_USE_RATE
        lots_map = _compute_lots(orders, equity, use_rate)
        use_est = equity is None
    else:
        lots_map = {o["contract"]: (lots, 0.0) for o in orders}

    # 增量对齐：目标清单 vs 当前持仓，只下「差异」腿，平掉「目标外」旧腿
    to_place, to_close, skipped_existing, positions_known = \
        _diff_against_positions(orders, lots_map)

    # 实时盘口报价（TqSdk 免费行情）：买入→卖一、卖出→买一；失败回退收盘参考价
    all_actions = [o for o, _, _ in to_place] + [c for c, _ in to_close]
    live_prices = _fetch_realtime_prices([o["contract"] for o in all_actions])
    if live_prices:
        print(f"[报价] 已获取实时盘口（TqSdk），对手价下单；未覆盖的腿回退收盘参考价。")

    print("=" * 64)
    if aggressive:
        mode = "激进·收益优先（满仓 α）"
    else:
        mode = "对冲优先" if hedge else "α 满仓轮动"
    print(f"委托清单（{mode} · {'执行' if execute else 'DRY-RUN，仅预览，未下单'}）")
    print("=" * 64)
    if aggressive:
        print(f"  [激进] 激进窗口（{_aggressive_from()} 起）已开启：关闭对冲、"
              f"目标使用率 {AGGRESSIVE_USE_RATE:.0%}（折算按 {min(AGGRESSIVE_USE_RATE, AGGRESSIVE_USE_CAP):.0%}，"
              f"预留安全垫避开 {STOP_LOSS_USE_RATE:.0%} 止损线），收益优先。")
    if not positions_known:
        print("  [注意] 持仓查询失败，按全量目标列出（可能重复下单，请先查 holdings 确认）。")

    for o, l, tag in to_place:
        margin = lots_map[o["contract"]][1]
        note = f"保证金≈{margin*l:.0f}" if margin else ""
        price, d = _resolve_order_price(o, live_prices)
        src = "参考价" if not d else ("卖一" if o["order_side"] in ("买开", "买平") else "买一")
        tail = f"（{tag}）"
        if d:
            print(f"  {o['order_side']} {o['contract']} ({o['name']})  "
                  f"{src} {price}（参考 {o['price_ref']}）  手数 {l}  {note} {tail}")
        else:
            print(f"  {o['order_side']} {o['contract']} ({o['name']})  "
                  f"{src} {price}  手数 {l}  {note} {tail}")

    for c, tag in to_close:
        price, d = _resolve_order_price(c, live_prices)
        src = "参考价" if not d else ("卖一" if c["order_side"] == "买平" else "买一")
        print(f"  {c['order_side']} {c['contract']} ({c['name']})  "
              f"{src} {price}  手数 {c['lots']}  （{tag}）")

    for o, cur_lots in skipped_existing:
        print(f"  [跳过] {o['order_side']} {o['contract']} ({o['name']})  "
              f"已有同向持仓 {cur_lots} 手，无需重复下单。")

    if not to_place and not to_close:
        print("\n[完成] 当前持仓已与目标一致，无增量下单。")
        if not execute:
            return
        _cleanup_temp()
        return

    if use_est:
        print("\n[注意] 实时权益查询失败，手数用默认权益 100 万预估。")
    if hedge:
        total_margin = sum(lots_map[o["contract"]][1] * l for o, l, _ in to_place)
        print(f"\n[对冲] 期货做空腿对冲现货底仓 β，增量总保证金 ≈ {total_margin:,.0f}。")
        print("  说明：做空股指对冲沪深300ETF 权益 β；做空国债对冲 22付息国债10 利率 β。")

    if not execute:
        print("\n以上为预览，未执行任何下单。")
        print("确认无误后运行: python orchestrator.py orders --yes")
        return

    if not _need_cfapikey():
        print("\n[ERROR] CFAPIKEY 环境变量未配置，无法下单。")
        print("请先: export CFAPIKEY=<你的用户ID>")
        sys.exit(1)

    # 执行前人工确认（安全铁律）；--force 时跳过（仅限用户预先授权的定时任务）
    if not force:
        print("\n[!] 即将真实下单到模拟交易账户，请确认（输入 y 继续，其他取消）: ", end="")
        ans = input().strip().lower()
        if ans != "y":
            print("已取消，未下单。")
            return

    # 先平后开：先释放旧仓位（尤其反方向腿，平掉才腾保证金），再开新仓
    for c, tag in to_close:
        price, _ = _resolve_order_price(c, live_prices)
        print(f"\n>> {c['order_side']} {c['contract']} @ {price} × {c['lots']} 手（{tag}）")
        ok, msg = _place_order(c["contract"], c["order_side"], price, c["lots"])
        _append_trade_log(c["contract"], c["order_side"], price, c["lots"], ok, msg)
        if ok:
            print(f"[OK] {msg}")
        else:
            print(f"[WARN] {msg}，请用 holdings / 今天委托 查状态，勿自动重试。")

    for o, l, tag in to_place:
        if l <= 0:
            print(f"\n[跳过] {o['order_side']} {o['contract']}：预算不足一手，未下单。")
            continue
        price, _ = _resolve_order_price(o, live_prices)
        print(f"\n>> {o['order_side']} {o['contract']} @ {price} × {l} 手（{tag}）")
        ok, msg = _place_order(o["contract"], o["order_side"], price, l)
        _append_trade_log(o["contract"], o["order_side"], price, l, ok, msg)
        if ok:
            print(f"[OK] {msg}")
        else:
            print(f"[WARN] {msg}，请用 holdings / 今天委托 查状态，勿自动重试。")

    print("\n下单流程结束。请运行 `python orchestrator.py holdings` 核对持仓，或查挂单确认。")
    # 每次成功下单后，自动挂载软件侧止损（总亏损止损）
    if any(l > 0 for _, l, _ in to_place) or to_close:
        arm_stop()
    # 调仓完成 → 自动同步交易记录（失败只警告）
    _sync_trade_record()
    # 决策完成后清理临时数据（dfcfqh 输出 + signal.json）
    _cleanup_temp()


def cmd_trade(lots=None, force=False, hedge=True, aggressive=None, use_news=True):
    """一键流程：更新数据(尽力而为) → 生成信号 → 下单。force=True 跳过确认。"""
    _require_engine()
    # ① 更新期货数据（失败不阻断，继续用现有 DB 生成信号）
    try:
        cmd_update()
    except SystemExit:
        print("[WARN] update 失败/退出，继续用现有数据生成信号。")
    except Exception as e:
        print(f"[WARN] update 异常（继续）: {e}")

    # ② 生成信号（失败则终止，不带着旧信号下单）
    cmd_signal(use_news=use_news)

    # ③ 直接下单（execute=True）
    cmd_orders(execute=True, lots=lots, force=force, hedge=hedge, aggressive=aggressive)


# ---------- 辅助查询 ----------
def _run_dfcfqh(query):
    """执行 dfcfqh 脚本的查询类指令，打印其结果。"""
    if not _need_cfapikey():
        print("[ERROR] CFAPIKEY 环境变量未配置。")
        sys.exit(1)
    subprocess.run(f"python {DFCFQH_SCRIPT} \"{query}\"", shell=True)


def cmd_contracts():
    _run_dfcfqh("有哪些合约")


def cmd_holdings():
    _run_dfcfqh("我的持仓")


def cmd_account():
    _run_dfcfqh("我的账户")


# ---------- 风险监控 ----------
# 持仓 futname（中文名）→ 连续代码，用于查合约乘数（SPEC）。顺序敏感：三十债须在十债前。
FUTNAME_TO_SYMBOL = [
    ("沪深", "IF0"), ("上证", "IH0"), ("中证500", "IC0"), ("中证1000", "IM0"),
    ("三十债", "TL0"), ("五债", "TF0"), ("二债", "TS0"), ("十债", "T0"),
]


def _symbol_from_futname(futname):
    for kw, sym in FUTNAME_TO_SYMBOL:
        if kw in (futname or ""):
            return sym
    return None


# ---------- 主力合约换月检测 ----------
# CFFEX 合约挂牌规则（近似）：股指 IF/IH/IC/IM 当月+下月+随后两个季月，当月合约
# 第三周周五交割；国债 TS/TF/T/TL 最近三个季月，最近季月第二周周五交割。
# 这里按「品种 → 当月到期日」近似估算，仅用于到期风险提示，不做精确交割日历。
def _contract_expiry(contract):
    """估算合约到期日（date）。股指=当月第三周周五，国债=当月第二周周五。"""
    m = re.match(r"^[A-Z]+(\d{4})$", contract or "")
    if not m:
        return None
    ym = m.group(1)
    yy, mm = int(ym[:2]) + 2000, int(ym[2:])
    try:
        first = datetime.date(yy, mm, 1)
    except ValueError:
        return None
    first_fri = first + datetime.timedelta(days=(4 - first.weekday()) % 7)
    is_bond = contract[0] in ("T",)   # TS/TF/T/TL 均以 T 开头
    week_n = 2 if is_bond else 3
    return first_fri + datetime.timedelta(weeks=week_n - 1)


def _query_tradable_contracts():
    """拉当前可交易期货合约代码集合（失败返回 None，调用方降级不阻断）。"""
    r = _dfcfqh_api({"action": "query_futures_contracts"})
    codes = set()
    for c in (r.get("result") or []) if r and r.get("status") == 0 else []:
        # isOption 是字符串 "0"/"1"（"0" 也是真值，不能 if not c.get(...)）
        if str(c.get("isOption")) != "1":
            code = c.get("futcode") or ""
            if re.match(r"^[A-Z]+\d{4}$", code):
                codes.add(code)
    return codes or None


def check_contract_rollover(orders=None, block=False):
    """换月检测：① MAIN_CONTRACT 映射的合约是否已不在可交易列表（失效→报警，block 时拒绝下单）；
    ② 主力合约距估算到期日 ≤5 天→到期预警（独立于可交易列表，接口失败也能算）；
    ③ 当前持仓月份与 MAIN_CONTRACT 不一致→提示跨月滚动。
    返回 (ok, warnings)；ok=False 表示映射失效且 block=True。"""
    warnings = []
    tradable = _query_tradable_contracts()
    today = datetime.date.today()
    ok = True

    # ① 映射失效 + ② 到期预警
    for sym, contract in eng.MAIN_CONTRACT.items():
        if tradable is not None and contract not in tradable:
            prefix = re.match(r"^[A-Z]+", contract).group(0)
            cands = sorted(c for c in tradable
                           if c.startswith(prefix) and re.match(r"^[A-Z]+\d{4}$", c))
            nxt = cands[0] if cands else None
            warnings.append(f"[换月] {sym} 映射的 {contract} 已不可交易，应换为 {nxt}（请更新 MAIN_CONTRACT）")
            ok = False
        exp = _contract_expiry(contract)
        if exp and 0 <= (exp - today).days <= 5:
            warnings.append(f"[到期预警] {sym} {contract} 约 {exp} 交割（≤5 天），"
                            f"请尽快核对下季月主力并滚动")

    # ② 持仓月份检测：持仓的品种若已不在 MAIN_CONTRACT 指向的月份，提示滚动
    if orders is not None and tradable is not None:
        for p in _query_positions_raw():
            name = p.get("futname") or "?"
            sym = _symbol_from_futname(name)
            target = eng.MAIN_CONTRACT.get(sym)
            if not sym or not target:
                continue
            # 从 futname 还原实际持仓月份（如「二债2612」→ 2612）
            m = re.search(r"(2[0-9]{3})\s*$", name)
            held_mm = m.group(1)[2:] if m else None
            tgt_mm = target[2:]
            if held_mm and held_mm != tgt_mm:
                warnings.append(f"[滚动提示] 持仓 {name}（{held_mm} 月）不在当前主力月份 {tgt_mm}，"
                                f"调仓将先平旧月、再开新月（跨月滚动）")

    return ok, warnings


def _restore_held_contract(sym, futname, fallback):
    """从持仓 futname 还原实际带月份合约（如「二债2612」→ TS2612）。
    futname 无月份或解析失败时回退 MAIN_CONTRACT（旧行为）。"""
    m = re.search(r"(2[0-9]{3})\s*$", futname or "")
    if not m or not fallback:
        return fallback
    month = m.group(1)[2:]
    prefix = re.match(r"^[A-Z]+", fallback).group(0)
    return f"{prefix}{month}"


def cmd_risk():
    """风险监控：读账户权益/使用率 + 期货持仓，输出爆仓/穿仓边界。
    强平线近似：使用率 = 占用保证金/权益；权益下降(浮亏) → 使用率上升。
    预警 80%、强平 100%。多空方向决定不利行情方向（多怕跌、空怕涨）。"""
    if not _need_cfapikey():
        print("[ERROR] CFAPIKEY 环境变量未配置。")
        sys.exit(1)

    acc = _dfcfqh_api({"action": "query_account_detail"})
    hold = _dfcfqh_api({"action": "query_account_holdings"})
    if not acc or not hold:
        print("[ERROR] 账户/持仓查询失败，请稍后重试。")
        sys.exit(1)

    # 账户
    fut = (acc.get("result") or {}).get("futureAccountDetail") or {}
    equity = float(fut.get("currentEquity") or 0)
    use_rate = float(fut.get("useRate") or 0)

    # 持仓
    result = hold.get("result") or {}
    positions = result.get("futureAccountHoldings") or []
    goods = result.get("goodsAccountHoldings") or {}

    used_margin = sum(float(p.get("ccbzj") or 0) for p in positions)
    available = equity - used_margin

    print("=" * 64)
    print("风险监控")
    print("=" * 64)
    print(f"  期货权益:      {equity:,.0f}")
    print(f"  占用保证金:    {used_margin:,.0f}")
    print(f"  可用缓冲:      {available:,.0f}")
    print(f"  资金使用率:    {use_rate:.1%}")
    if goods:
        print(f"  现货总市值:    {float(goods.get('goodsAmount') or 0):,.0f}（固定不可交易）")

    # 分级判定
    if use_rate < 0.50:
        level, act = "🟢 正常", "不动作，周度调仓即可。"
    elif use_rate < 0.80:
        level, act = "🟡 警戒", "每天查一次账户，关注浮亏方向。"
    elif use_rate < 0.95:
        level, act = "🟠 高危", "主动减仓：优先平亏损最大的腿，把使用率压回 60% 以下。"
    else:
        level, act = "🔴 强平逼近", "立即平掉全部期货持仓，保本退出。"
    print(f"\n  风险等级:      {level}")
    print(f"  处置建议:      {act}")

    # 强平/预警距离（占用保证金随价格小幅变动，此处按固定近似，实际会略快触线）
    loss_to_warn = equity - used_margin / 0.80
    loss_to_force = equity - used_margin / 1.00
    print("\n" + "-" * 64)
    print("爆仓/穿仓边界（累计浮亏达下列值即触发）")
    print("-" * 64)
    print(f"  距预警(使用率80%):  再亏 {max(0, loss_to_warn):,.0f} 元")
    print(f"  距强平(使用率100%): 再亏 {max(0, loss_to_force):,.0f} 元")
    print(f"  穿仓(权益归零):     再亏 {equity:,.0f} 元")

    # 逐腿
    print("\n" + "-" * 64)
    print("逐腿风险（不利方向 + 每点盈亏）")
    print("-" * 64)
    if not positions:
        print("  暂无期货持仓，无爆仓风险。")
    for p in positions:
        name = p.get("futname") or "?"
        mmfx = p.get("mmfx") or ""
        lots = float(p.get("ccsl") or 0)
        price = float(p.get("price") or 0)
        sym = _symbol_from_futname(name)
        mult = SPEC.get(sym, {}).get("multiplier", 0) if sym else 0
        pnl_per_pt = mult * lots
        if mmfx == "空":
            dir_desc = "怕涨（价格每涨1点亏 ¥{0:,.0f}）".format(pnl_per_pt)
        elif mmfx == "多":
            dir_desc = "怕跌（价格每跌1点亏 ¥{0:,.0f}）".format(pnl_per_pt)
        else:
            dir_desc = "方向未知"
        print(f"  {name}  {mmfx}{int(lots)}手  现价 {price:.2f}  {dir_desc}")
    print()
    print("备注：强平/穿仓边界为近似值，占用保证金随行情变动，实际触线会略早。")
    print("     激进模式使用率接近100%，务必每日盯一次本命令。")


def cmd_risk_alias():
    cmd_risk()


# ---------- 软件侧止损 ----------
def _query_positions_raw():
    """查询期货持仓原始列表（无持仓/失败返回 []）。"""
    hold = _dfcfqh_api({"action": "query_account_holdings"})
    if not hold or hold.get("status") != 0:
        return []
    return (hold.get("result") or {}).get("futureAccountHoldings") or []


def _query_future_pnl():
    """返回 (期货总浮亏, 资金使用率)；失败返回 (None, None)。"""
    acc = _dfcfqh_api({"action": "query_account_detail"})
    if not acc or acc.get("status") != 0:
        return None, None
    fut = (acc.get("result") or {}).get("futureAccountDetail") or {}
    try:
        return float(fut.get("marketBuoyancy") or 0), float(fut.get("useRate") or 0)
    except (TypeError, ValueError):
        return None, None


def _close_all_orders():
    """按持仓方向生成平仓命令清单。空头→买平(operType=2)、多头→卖平(operType=3)。
    返回 (平仓命令列表, 持仓列表)。"""
    positions = _query_positions_raw()
    cmds = []
    for p in positions:
        name = p.get("futname") or "?"
        sym = _symbol_from_futname(name)
        futcode = eng.MAIN_CONTRACT.get(sym)   # 中文 futname 无 ASCII 字母，经连续代码映射带月份合约
        if not futcode:
            continue
        futcode = _restore_held_contract(sym, name, futcode)   # 用持仓实际月份（换月过渡期可能在旧月）
        mmfx = p.get("mmfx") or ""
        oper = "2" if mmfx == "空" else ("3" if mmfx == "多" else None)
        if oper is None:
            continue
        close_side = "买平" if oper == "2" else "卖平"
        price = p.get("price") or "0"
        lots = int(float(p.get("ccsl") or 0))
        cmds.append({"contract": futcode, "side": close_side, "price": price,
                     "lots": lots, "operType": oper})
    return cmds, positions


def _check_stop(reason="monitor"):
    """检查止损条件，触发则自动平掉全部期货持仓。
    返回字符串状态：'triggered' | 'clear' | 'empty' | 'noop'。"""
    pnl, use_rate = _query_future_pnl()
    if pnl is None:
        return "noop"   # 查询失败，不动作
    if pnl <= STOP_LOSS_FUTURE_PNL or use_rate >= STOP_LOSS_USE_RATE:
        cmds, positions = _close_all_orders()
        if not cmds:
            print(f"[止损] 触发（浮亏 {pnl:,.0f} / 使用率 {use_rate:.1%}）但无可平持仓。")
            return "empty"
        print("=" * 64)
        print(f"[止损] 触发！期货总浮亏 {pnl:,.0f} ≤ {STOP_LOSS_FUTURE_PNL:,.0f}"
              f" 或 使用率 {use_rate:.1%} ≥ {STOP_LOSS_USE_RATE:.0%}")
        print("      自动平掉全部期货持仓（arm_stop 挂载时已确认自动止损）：")
        print("=" * 64)
        for c in cmds:
            print(f"  >> {c['side']} {c['contract']} @ {c['price']} × {c['lots']} 手")
            ok, msg = _place_order(c["contract"], c["operType"], c["price"], c["lots"])
            _append_trade_log(c["contract"], c["side"], c["price"], c["lots"], ok, msg)
            if ok:
                print(f"  [OK] {msg}")
            else:
                print(f"  [WARN] {msg}，请用 holdings 核对，勿自动重试。")
        _mark_stop(armed=True)
        _sync_trade_record()
        _cleanup_temp()
        return "triggered"
    return "clear"


def _load_stop_state():
    try:
        with open(STOP_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_stop_state(state):
    with open(STOP_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _mark_stop(armed=None):
    """更新止损状态文件。armed=True/False 或省略（只更新时间）。"""
    state = _load_stop_state()
    if armed is not None:
        state["armed"] = armed
    state["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["stop_loss_future_pnl"] = STOP_LOSS_FUTURE_PNL
    state["stop_loss_use_rate"] = STOP_LOSS_USE_RATE
    _save_stop_state(state)


def arm_stop():
    """下单成功后调用：把止损阈值写入本地状态，供盯盘任务读取。"""
    _mark_stop(armed=True)
    print(f"[止损] 已挂载软件侧止损：期货总浮亏 ≤ {STOP_LOSS_FUTURE_PNL:,.0f} 元"
          f" 或 使用率 ≥ {STOP_LOSS_USE_RATE:.0%} 时自动平仓。")
    print("      注意：需盯盘任务在线（App 打开）才能触发；App 关闭期间不生效。")


def cmd_stop():
    """一次性止损检查 + 查看状态。"""
    pnl, use_rate = _query_future_pnl()
    print("=" * 64)
    print("软件侧止损状态")
    print("=" * 64)
    print(f"  当前期货总浮亏:  {pnl if pnl is None else format(pnl, ',.0f')}")
    print(f"  当前资金使用率:  {use_rate if use_rate is None else format(use_rate, '.1%')}")
    print(f"  止损线(浮亏):    {STOP_LOSS_FUTURE_PNL:,.0f} 元")
    print(f"  止损线(使用率):  {STOP_LOSS_USE_RATE:.0%}")
    state = _load_stop_state()
    print(f"  止损状态:        {'已挂载' if state.get('armed') else '未挂载'}"
          f"（更新于 {state.get('updated_at', '-')}）")
    print("-" * 64)
    r = _check_stop(reason="manual")
    if r == "clear":
        print("  未触发，无需平仓。")
    elif r == "empty":
        print("  当前无期货持仓。")
    elif r == "triggered":
        print("  已触发并自动平仓。")
    elif r == "noop":
        print("  [WARN] 账户查询失败，无法判定，请稍后重试。")


def cmd_stop_watch():
    """供定时任务调用的无交互检查（避免 input 阻塞）。触发则自动平仓。"""
    r = _check_stop(reason="watch")
    if r == "clear":
        print("[止损] 正常，未触发。")
    return r


# ---------- 入口 ----------
def main():
    p = argparse.ArgumentParser(description="cffex-multifactor-trader 编排器")
    p.add_argument("command", choices=["update", "signal", "trade", "contracts",
                                       "orders", "holdings", "account", "risk",
                                       "stop", "stop-watch", "report", "doctor"])
    p.add_argument("--yes", action="store_true", help="orders：真正执行下单")
    p.add_argument("--force", action="store_true", help="trade/orders：跳过人工确认（仅限预先授权的定时任务）")
    p.add_argument("--lots", type=int, default=0, help="orders/trade：非对冲模式每腿手数（0=按保证金比例折算）")
    p.add_argument("--no-hedge", action="store_true", help="orders/trade：关闭对冲，走 α 满仓轮动模式（默认开启对冲）")
    p.add_argument("--aggressive", dest="aggressive", action="store_true", default=None,
                   help="orders/trade：强制激进·收益优先模式（供演练；默认按日期自动判定）")
    p.add_argument("--no-aggressive", dest="aggressive", action="store_false",
                   help="orders/trade：强制关闭激进模式（供演练；默认按日期自动判定）")
    p.add_argument("--no-news", action="store_true", help="signal/trade：跳过消息面查询（回退纯量化）")
    args = p.parse_args()

    lots = args.lots if args.lots > 0 else None
    hedge = not args.no_hedge
    use_news = not args.no_news

    if args.command == "update":
        cmd_update()
    elif args.command == "signal":
        cmd_signal(use_news=use_news)
    elif args.command == "trade":
        cmd_trade(lots=lots, force=args.force, hedge=hedge, aggressive=args.aggressive,
                  use_news=use_news)
    elif args.command == "orders":
        cmd_orders(execute=args.yes, lots=lots, force=args.force, hedge=hedge,
                   aggressive=args.aggressive)
    elif args.command == "contracts":
        cmd_contracts()
    elif args.command == "holdings":
        cmd_holdings()
    elif args.command == "account":
        cmd_account()
    elif args.command == "risk":
        cmd_risk()
    elif args.command == "stop":
        cmd_stop()
    elif args.command == "stop-watch":
        cmd_stop_watch()
    elif args.command == "report":
        cmd_report()
    elif args.command == "doctor":
        cmd_doctor()


if __name__ == "__main__":
    main()
