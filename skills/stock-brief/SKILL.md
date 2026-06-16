---
name: stock-brief
description: Use when user asks for today's A-share stock briefing, market summary, watchlist analysis, or "跑今天简报/今日简报/早盘/收盘/看一下自选股" — collects structured market data and returns it as dict for in-session LLM summarization
---

# stock-brief

## Overview

Pure data-collection skill for today's A-share stock briefing. Picks mode from current Beijing time window (盘前/盘中 → market-only, 盘后 → full, 深夜 → skip), checks trading-day calendar, then collects structured data (Layer 1 全市场 + Layer 3 自定义筛选, optionally Layer 2 自选股深度) and **returns a dict**. **In-session LLM (you, Claude) renders the markdown summary. NO Feishu push, NO litellm call, NO external LLM.**

## When to Use

Trigger when user asks for:
- "给我跑今天简报" / "今日简报" / "跑一下 brief"
- "早盘态势" / "收盘完整简报" / "今天市场怎么样"
- "看一下我的自选股" / "watchlist 数据"

Do NOT use for:
- Historical backfill (only handles current day)
- Non-A-share markets (港股/美股 are not auto-collected by this skill)
- User wants the result pushed to Feishu / Slack / webhook (this skill returns data only; user or another tool does the push)

## Mode Auto-Selection (Beijing time, TZ=Asia/Shanghai)

| Window | Mode | Data collected |
|---|---|---|
| 06:00-15:00 (盘前/盘中) | `market-only` | Layer 1 only |
| 15:01-22:00 (盘后) | `full` | Layer 1 + Layer 3 (custom screening) |
| 22:01-05:59 (深夜) | `skip` | nothing |
| user `--mode=watchlist` | `watchlist` | Layer 1 + Layer 2 自选股深度 |

User can force any mode with `--mode=...`. `--force` skips trading-day check.

## Hard Rules

**No Feishu**: this skill must NOT import lark-oapi / requests webhook / push. The return value is dict only; user decides how to deliver.

**No external LLM**: this skill must NOT call litellm / openai / anthropic SDK. LLM summarization happens **in the current session** (you, Claude), reading the returned dict.

**No editing existing files**: must NOT modify `brief.py` / `sector.py` / `bot.py` / `watchlist.json` / `stock_meta.json` / `data/seats.json` / `.env`.

**Trading-day check mandatory**: always call `is_trading_day_today()` unless `--force` is explicitly set.

**TZ locked**: all time decisions use `TZ=Asia/Shanghai`. Never use naive `datetime.now()`.

## Usage

### As Python function

```python
import sys
from pathlib import Path
sys.path.insert(0, "/home/alex/codes/stock-briefing-bot/skills/stock-brief/scripts")

from run_brief import collect

# mode="auto" picks by Beijing time window
result = collect(mode="auto")
# mode can also be: "market-only" | "full" | "watchlist" | "skip"

# result = {
#   "mode": str,
#   "skipped_reason": str | None,
#   "data": {
#     "as_of": "2026-06-16 15:30 +08:00",
#     "layer1": {                       # 全市场 dict（来自 fetch_market_tables）
#       "lhb": DataFrame, "dzjy": DataFrame, "margin_sse": DataFrame, ...
#       "hot_money": {...},             # 游资聚合 dict
#       "sectors": [...],               # 板块 Top N
#     },
#     "layer2": [...],                  # 仅 watchlist 模式：每只自选股的 11 维 dict
#     "layer3": {...},                  # 仅 full 模式：自定义筛选 hits
#   } | None,
#   "duration_sec": float,
# }
```

### As CLI (for manual data collection)

```bash
cd /home/alex/codes/stock-briefing-bot
.venv/bin/python skills/stock-brief/scripts/run_brief.py --mode=auto       # by time window
.venv/bin/python skills/stock-brief/scripts/run_brief.py --mode=full       # force full
.venv/bin/python skills/stock-brief/scripts/run_brief.py --mode=watchlist  # Layer 1+2
.venv/bin/python skills/stock-brief/scripts/run_brief.py --force          # skip trading-day
```

Output: prints mode + which layers were collected + duration. Data dict is JSON-serialized to stdout for piping.

### In-session summarization (you, Claude)

After `collect()` returns the dict:

1. Read `data.layer1` (全市场 dict) — describe北向/龙虎榜温度/游资动向/板块
2. If `data.layer2` exists — for each stock, output 📋 数据清点 + 📰 关键消息解读 + 🧠 推理链 + 🎯 建议 (4 sections)
3. If `data.layer3` exists — render markdown table of hits
4. Deliver: print to user / save to file / hand to user for manual sharing

**Do NOT push to Feishu yourself.** If user wants that, tell them how (e.g., "copy this markdown to your Feishu bot") or suggest another tool.

## Returned Data Schema

```
result.data.layer1 = {
    "lhb":         DataFrame,         # 龙虎榜（akshare stock_lhb_detail_daily_sina）
    "dzjy":        DataFrame,         # 大宗交易
    "margin_sse":  DataFrame,         # 融资融券-沪
    "margin_szse": DataFrame,         # 融资融券-深
    "hsgt":        dict,              # 北向总览（_hsgt_summary 输出）
    "industry_list": DataFrame,       # 行业板块列表
    "hot_money":   dict,              # layer1_hot_money_summary 输出
    "sectors":     list[dict],        # 板块 Top N（layer1_top_sectors 输出）
}

result.data.layer2 = [
    {
        "code": "600519", "name": "贵州茅台", "market": "a",
        "payload": {
            "行情快照": dict, "K线技术": dict, "龙虎榜命中": list,
            "大宗交易": list, "融资融券(亿元)": dict, "股东户数趋势": dict,
            "板块同业": dict, "近 30 日公告": list, "近期新闻": list,
        }
    },
    ...
]

result.data.layer3 = {
    "candidates": int, "hits": list[dict], "error": str | None,
}
```

## Implementation

- `scripts/run_brief.py` — 3 public functions:
  - `determine_mode()` — pure local logic (Beijing time window → mode string)
  - `is_quiet_hours()` — bool convenience wrapper
  - `collect(mode, force)` — main entry: checks time/trading-day, calls brief.py pure data functions, returns dict
- All data functions delegated to `brief.py`:
  - `is_trading_day_today()`
  - `fetch_market_tables()` (Layer 1)
  - `gather_a(code, market, prefer_realtime)` (Layer 2 per stock)
  - `run_custom_screening()` (Layer 3)
  - `layer1_hot_money_summary(lhb_df, date, top_n)` (游资聚合)
  - Sector functions from `sector.py` (板块 Top N + 优质股预筛)

## Common Mistakes

- ❌ **import litellm / openai** in skill → forbidden; you are the LLM
- ❌ **import lark-oapi / requests webhook** in skill → forbidden; user handles push
- ❌ **call `brief.run_full_brief()` / `run_market_review()` / `run_watchlist_brief()`** → these include LLM + render; call pure data functions instead
- ❌ **modify `watchlist.json` / `stock_meta.json`** to "adjust" data → read-only
- ❌ **skip trading-day check** → always call `is_trading_day_today()` unless `--force`
- ❌ **trust `datetime.now()` without TZ** → wrap with `ZoneInfo("Asia/Shanghai")`

## Files

- `SKILL.md` — this file
- `scripts/run_brief.py` — pure data collector, returns dict, no LLM, no push
