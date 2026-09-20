# 敏感信息说明（发布前请阅读）

本仓库为**开源分享版**，已剔除以下内容。若你 fork 后部署，请继续保持仓库清洁：

## 已剔除

1. **账户凭据**：`CFAPIKEY`、`TQSDK_USER/PASS`、`MX_APIKEY` 一律走环境变量，
   代码与文档中不含任何真实值。
2. **个人路径**：桌面/比赛工作目录路径已改为环境变量配置
   （`CFFEX_*` 系列），默认值指向通用的 `~/.claude/skills/` 布局。
3. **策略引擎本体**：`futures_multifactor_engine.py` **不随本仓库分发**（用户手动放入）。
   请勿把你的策略实现提交到公开仓库或 fork 中。
4. **私有策略校准**：回测结论（夏普数值/最优方向）、实盘归因数据、
   比赛专属排期说明已移除；参数保留为**未校准默认值**，注释明确提示需自行回测。
5. **交易数据**：`signal_log.jsonl`、`trade_log.jsonl`、`.news_cache.json`、
   `stop_state.json` 等本地运行产物不入库（见 .gitignore）。

## 部署时请勿提交

- `futures_multifactor_engine.py`（策略引擎，用户自备文件）
- 任何含 key 的 shell 配置片段（`~/.bashrc` 导出行）
- `signal.json` / `signal_log.jsonl` / `trade_log.jsonl` / `.news_cache.json` / `stop_state.json`
- `GETTING_STARTED.md`（doctor 按机器状态生成，属个人产物）

## 提交前自查

```bash
git grep -nE "CFAPIKEY\s*=\s*['\"][^<]"   # 确认没有硬编码 key
git grep -n " Desktop/"                    # 确认没有个人桌面路径
git ls-files | grep futures_multifactor    # 确认策略引擎未被误提交
```
