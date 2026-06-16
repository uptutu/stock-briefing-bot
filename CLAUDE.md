# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目一句话

每日 A 股自选股 + 全市场态势 + LLM 因果推理 → Markdown 简报 → 飞书推送。设计原则：给数据 + 讲因果 + 不预测。

## 命令

```bash
# 装依赖（Python 3.11+）
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 配 env
cp .env.example .env  # 编辑填 LLM_API_KEY 和 FEISHU_* 凭证

# 完整 brief（约 5-7 分钟，自动推飞书；非交易日自动跳过）
.venv/bin/python brief.py

# 只跑 Layer 1 全市场态势（早盘场景，~1-2 分钟）
.venv/bin/python brief.py --market-only

# 只跑指定股票
.venv/bin/python brief.py --stocks 600519,300750

# 不调 LLM 不推送，验证数据流（最省 token）
.venv/bin/python brief.py --dry-run

# 跳过交易日检查（强制跑）
.venv/bin/python brief.py --skip-trading-check

# 启飞书 @ 机器人（长连接，常驻进程；收到 @ 后路由命令）
.venv/bin/python bot.py

# 探针：验证 akshare 6 个核心接口
.venv/bin/python probe.py
```

`scripts/run_brief_cn.sh` 由 launchd 触发（`scripts/launchd.plist.example` 是模板），按 `TZ=Asia/Shanghai` 北京时间自动选 `--market-only` 或 full 模式。

## 核心架构：三层流水线

`brief.py` 是单进程流水线，分 3 层（详见 `brief.py` docstring）：

- **Layer 1 全市场态势**（`run_market_review()`）：一次预拉 7 类数据（龙虎榜/大宗/融资融券 sse+szse/北向总览/行业板块），渲染为 Markdown。`sector.py` 提供板块 Top N + 板块内 LLM 挑 Top 3 优质股的子模块（混合方案 C：规则粗筛 → baostock 补 ROE/净利 → LLM 精选）。
- **Layer 2 自选股深度**（`run_watchlist_brief()`）：每只股拉行情/K 线/MACD/北向/龙虎榜命中/大宗/融资融券/股东户数/板块同业/公告/新闻 → `llm_summarize()` 输出「📋 数据清点 → 📰 关键消息解读 → 🧠 推理链 → 🎯 建议」。
- **Layer 3 自定义筛选**（`run_custom_screening()`）：只在 full brief 跑，3 条件同时满足（涨幅 > 5% / 收盘 > MA5 且 > MA10 / 量比 > 2），全 A 股扫描后用 markdown 表格渲染。

CLI 用 argparse 路由（`main()` in `brief.py:1506`）：`--market-only` → 1，`--stocks X,Y` → 2，无参 → 1 + 2 + 3。

## 数据源与 fallback

| 数据 | 主源 | 备用 |
|---|---|---|
| A 股行情/K 线/财报 | `baostock` | 腾讯 `qt.gtimg.cn` + 新浪 |
| 板块涨幅/成分股 | 同花顺 `q.10jqka.com.cn`（`curl_cffi` 模拟 chrome120 TLS） | 新浪申万 |
| 主力/新闻/公告 | `akshare`（巨潮/新浪/东财） | — |
| 港股 | `akshare` | — |
| 美股 | `yfinance` | — |

关键：`socket.setdefaulttimeout(15)` 兜底 akshare 偶发 hang；`safe_df()` / `safe()` 包装所有外部调用（3 次重试 + 2s 退避）。

## LLM 调用

`brief.py:llm_summarize()` 走 `litellm`（OpenAI 兼容协议），通过 `.env` 配：`LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。`sector.py:llm_pick_top3()` 单独调板块 Top 3。Prompt 要求 LLM 显式因果（"因为 A 所以 B，叠加 C 说明 D"）+ 数据可溯源。

并发：`sector.py` 板块精选用 `concurrent.futures.ThreadPoolExecutor`（pool_size=6 默认），主流程用 `subprocess.Popen` 后台跑（防 LLM 超时阻塞）。

## 飞书集成

两套通道，按场景选：
- **Webhook 自定义机器人**（`FEISHU_WEBHOOK_URL`）：`push_feishu()` 用，只在 brief.py 推送。
- **应用机器人长连接**（`FEISHU_APP_ID/SECRET`）：`bot.py` 用，`lark.ws.Client` WebSocket 监听 `@股民简报`。

Markdown 推送前必须过 `render_brief()` → 卡片正文超 4800 字节要 chunk（`_split_markdown()` 拆 header/section 边界）。`bot.py` 的 `reply_card()` 自带 4800 截断。

## 关键文件

- `brief.py` (1783 行) — 简报生成 + 推送，单文件分层
- `sector.py` (728 行) — 板块强势子模块，被 `brief.py` 在 Layer 1 调用
- `bot.py` — 飞书 @ 机器人长连接 + 命令路由（watchlist/板块/龙虎榜/brief/单股）
- `probe.py` — akshare 6 个核心接口的探针（输出 `data/probe_<timestamp>.json`）
- `watchlist.json` — 自选股清单（`code`/`name`/`market`）
- `stock_meta.json` — 每只股的市值/行业（每季度刷新）
- `data/seats.json` — 席位识别字典（hot_money/generic，含待命名席位发现阈值），改完 mtime 自动重载
- `docs/quote_source_eval_*.md` / `docs/sector_strength_eval_*.md` — 数据源选型评估历史
- `scripts/run_brief_cn.sh` + `scripts/launchd.plist.example` — launchd 定时任务

## 命令路由（bot.py）

收到 `@股民简报 X` 时按文本匹配路由：

- `watchlist` / `自选股` / `列表` → 列表
- `板块` / `强势板块` → 后台跑 `sector.py`，回复 interactive card
- `龙虎榜` / `游资` / `lhb` → 拉昨日龙虎榜聚合 Top 买卖席位
- `brief` / `完整简报` → 后台 `subprocess.Popen(brief.py)` 跑完整流程
- 6 位代码 / 股票名 → 单股分析（盘中走腾讯 `prefer_realtime=True`，非盘中走 baostock）
- 其他 → `HELP_TEXT`

`resolve_stock()` 三层匹配：嵌入 6 位代码 → 自选股名/简称模糊 → 全 A 股完整名（磁盘缓存 `data/cache/a_share_universe.json`，EM 主源 + 新浪 spot 备源）。

## 交易日检查

`is_trading_day_today()` 用 baostock 查询当日是否 A 股交易日；非交易日 `brief.py` 自动跳过。`scripts/run_brief_cn.sh` 强制 `TZ=Asia/Shanghai` 避免 PDT 时区错位。

## 调试常见问题

- **akshare hang**：全局 `socket.setdefaulttimeout(15)`，单接口重试用 `safe_df()`。
- **同花顺 401/403**：必须用 `curl_cffi` 模拟 chrome120 指纹，普通 requests 会被拒。
- **LLM 超时/截断**：用 `subprocess.Popen` 后台跑 brief 不阻塞；板块精选并发 6 路。
- **飞书卡片 4800 截断**：`render_brief()` 输出时已 chunk；bot 端 `reply_card()` 自带硬截断。
- **北向数据缺失**：2024-08-19 起政策性停发，代码里所有 `*hsgt*` 行已注释"已删除"标记。
