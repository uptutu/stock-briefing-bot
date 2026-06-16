---
name: stock-brief
description: Use when user asks for today's A-share stock briefing, market summary, watchlist analysis, or "跑今天简报/今日简报/早盘/收盘/看一下自选股" — collects structured market data and returns it as dict for in-session LLM summarization
---

# stock-brief

## Overview

Pure data-collection skill for today's A-share stock briefing. Picks mode from current Beijing time window (盘前/盘中 → market-only, 盘后 → full, 深夜 → skip), checks trading-day calendar, then collects structured data (Layer 1 全市场基础态势, optionally Layer 2 自选股深度) and **returns a dict**. **In-session LLM (you, Claude) renders the markdown summary. NO Feishu push, NO litellm call, NO external LLM.**

## When to Use

Trigger when user asks for:
- "给我跑今天简报" / "今日简报" / "跑一下 brief"
- "早盘态势" / "收盘完整简报" / "今天市场怎么样"
- "看一下我的自选股" / "watchlist 数据"

Do NOT use for:
- Historical backfill (only handles current day)
- Non-A-share markets (港股/美股 not auto-collected by this skill)
- Sector ranking / 自定义筛选 Layer 3 (out of scope for this self-contained version)
- User wants the result pushed to Feishu / Slack / webhook (this skill returns data only; user or another tool does the push)

## Mode Auto-Selection (Beijing time, TZ=Asia/Shanghai)

| Window | Mode | Data collected |
|---|---|---|
| 06:00-15:00 (盘前/盘中) | `market-only` | Layer 1 only |
| 15:01-22:00 (盘后) | `full` | Layer 1 only (same as market-only) |
| 22:01-05:59 (深夜) | `skip` | nothing |
| user `--mode=watchlist` | `watchlist` | Layer 1 + Layer 2 自选股深度 |

Note: `full` and `market-only` currently collect the same Layer 1 set (龙虎榜 / 大宗 / 融资融券). The distinction is reserved for future Layer 3 (全市场筛选) expansion.

User can force any mode with `--mode=...`. `--force` skips trading-day check.

## Hard Rules

**No Feishu**: skill must NOT import `lark-oapi` / `requests` webhook / push. Return value is dict only.

**No external LLM**: skill must NOT call `litellm` / `openai` / `anthropic` SDK. LLM summarization happens **in the current session** (you, Claude).

**No editing project files**: must NOT modify `brief.py` / `sector.py` / `bot.py` / project root `watchlist.json` etc. All config lives in `skills/stock-brief/config/`.

**Trading-day check mandatory**: always check via `baostock.query_trade_dates()` (called inside `collect()`) unless `--force` is explicitly set.

**TZ locked**: all time decisions use `TZ=Asia/Shanghai`. Never use naive `datetime.now()`.

## Usage

All paths below are relative to the **skill root directory** `skills/stock-brief/`. Replace `$SKILL_DIR` with the actual path (e.g., `/home/alex/codes/stock-briefing-bot/skills/stock-brief`).

### As Python function (preferred for skill invocation)

```python
import sys
from pathlib import Path

# 1) 把 skill 自己的 scripts/ 加到 sys.path
sys.path.insert(0, str(Path("$SKILL_DIR/scripts").resolve()))

# 2) import 位于 skills/stock-brief/scripts/run_brief.py 的模块
from run_brief import collect, determine_mode, is_quiet_hours

# 3) 调用
result = collect(mode="auto")  # auto | market-only | full | watchlist | skip
result = collect(mode="auto", force=True)  # 跳过交易日历
result = collect(mode="watchlist")  # 强制 Layer 1+2

# result dict 结构：
# {
#   "mode": str,                   # 实际使用的 mode
#   "skipped_reason": str | None,
#   "data": {
#     "as_of": "2026-06-16 15:30 +08:00",
#     "layer1": {                  # 全市场基础 dict
#       "as_of": "...",
#       "yesterday": "20260615",
#       "lhb": [ {...}, ... ],     # 龙虎榜 list of dict
#       "dzjy": [ ... ],           # 大宗交易
#       "margin_sse": [ ... ],     # 融资融券-沪
#       "margin_szse": [ ... ],    # 融资融券-深
#     },
#     "layer2": [ ... ] | None,    # 仅 watchlist 模式：每只自选股的 11 维 dict
#   } | None,
#   "duration_sec": float,
# }
```

### As CLI (for manual data collection)

```bash
# 任意目录都行（脚本自动解析自身位置）
python "$SKILL_DIR/scripts/run_brief.py" --mode=auto        # 按北京时间窗口
python "$SKILL_DIR/scripts/run_brief.py" --mode=full        # 强制 full
python "$SKILL_DIR/scripts/run_brief.py" --mode=watchlist   # Layer 1+2
python "$SKILL_DIR/scripts/run_brief.py" --mode=market-only
python "$SKILL_DIR/scripts/run_brief.py" --force           # 跳过交易日历
python "$SKILL_DIR/scripts/run_brief.py" --out brief.json  # 把 data dict 写文件
python "$SKILL_DIR/scripts/run_brief.py" --check           # 只跑环境自检
```

Output: prints mode + which layers collected + duration to **stderr**. JSON dict goes to **stdout** (or file via `--out`) for piping.

### In-session summarization (you, Claude)

After `collect()` returns the dict:

1. Read `data.layer1` — describe 龙虎榜温度 / 大宗 / 融资融券
2. If `data.layer2` exists — for each stock, output 📋 数据清点 → 📰 公告/新闻解读 → 🧠 推理链 → 🎯 建议 (4 sections)
3. Deliver: print to user / save to file / hand to user for manual sharing

**Do NOT push to Feishu yourself.** If user wants that, suggest: copy markdown → Feishu bot, or use a separate push skill.

## Returned Data Schema

```python
result.data.layer1 = {
    "as_of":      "2026-06-16T15:30:00+08:00",
    "yesterday":  "20260615",
    "lhb":        [ {...}, ... ],   # ak.stock_lhb_detail_daily_sina(date=yesterday)
    "dzjy":       [ {...}, ... ],   # ak.stock_dzjy_mrtj(start=yesterday, end=today)
    "margin_sse": [ {...}, ... ],   # ak.stock_margin_detail_sse(date=yesterday)
    "margin_szse":[ {...}, ... ],   # ak.stock_margin_detail_szse(date=yesterday)
}

result.data.layer2 = [
    {
        "code":  "600519",
        "name":  "贵州茅台",
        "market":"a",
        "行情快照":       { "最新价": ..., "涨跌幅": ..., "成交额": ..., ... } | {"_error": ...},
        "K线技术":        { "收盘": ..., "MA5": ..., "MACD金叉死叉": ..., ... } | {"_error": ...},
        "龙虎榜命中":     [ {...}, ... ],
        "大宗交易":       [ {...}, ... ],
        "融资融券":       [ {...}, ... ],   # sse + szse 合并
        "股东户数趋势":   [ {...}, ... ],
        "近 30 日公告":   [ {...}, ... ],
        "近期新闻":       [ {...}, ... ],
    },
    ...
]
```

## Implementation

All paths relative to skill root `skills/stock-brief/`:

- `scripts/run_brief.py` — public API:
  - `determine_mode(now=None)` → `str` (Beijing time window → mode)
  - `is_quiet_hours(now=None)` → `bool`
  - `collect(mode="auto", *, force=False)` → `dict` (main entry)
- Data collection is **self-contained**: directly calls `akshare` + `baostock` pip packages; **does NOT import any project module** (`brief.py` / `sector.py` / `bot.py`).
- `_safe_df(label, fn, retries=3)` wraps every akshare call with retry + backoff.
- `_check_environment()` runs at `collect()` entry; raises clear error listing missing deps.
- `_is_cn_locale()` + `_pip_mirror_hint()` auto-suggest Tsinghua PyPI mirror on install.

## Environment & Setup

**One-time setup** (3 steps):

```bash
cd "$SKILL_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # CN locale 自动追加清华镜像
cp config/watchlist.example.json config/watchlist.json
# 编辑 config/watchlist.json，填你的自选股
```

**Run dependency check** (no data fetched):

```bash
python "$SKILL_DIR/scripts/run_brief.py" --check
```

**Required pip packages** (`requirements.txt`): `akshare>=1.18`, `baostock>=0.8`, `pandas>=2.0`. All other deps (`json`, `socket`, `pathlib`, `zoneinfo`) are stdlib.

## Common Mistakes

- ❌ **import `litellm` / `openai`** in skill → forbidden; you are the LLM
- ❌ **import `lark-oapi` / push to Feishu** in skill → forbidden; user handles push
- ❌ **import `brief` / `sector` / `bot`** → forbidden; skill must be self-contained
- ❌ **modify project root `watchlist.json`** → use `skills/stock-brief/config/watchlist.json` instead
- ❌ **skip trading-day check** → `collect()` does it automatically unless `force=True`
- ❌ **trust naive `datetime.now()`** → use `ZoneInfo("Asia/Shanghai")`
- ❌ **forget to run `pip install -r requirements.txt`** → run `--check` first to verify env

## Files

```
skills/stock-brief/
├── SKILL.md                       # this file
├── requirements.txt               # akshare + baostock + pandas
├── config/
│   ├── watchlist.example.json     # 模板，复制为 watchlist.json 后填自选股
│   └── watchlist.json             # 当前生效配置（gitignored）
└── scripts/
    └── run_brief.py               # 自包含数据采集器（~500 行）
```
