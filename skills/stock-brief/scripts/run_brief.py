"""
run_brief.py — stock-brief skill（自包含、零项目耦合）

硬约束：
  ❌ NO import brief / sector / bot —— 完全不依赖项目代码
  ❌ NO litellm / lark-oapi / 飞书推送 —— LLM 总结由调用会话完成
  ❌ NO 修改任何项目文件

数据采集直接调 akshare + baostock（pip 包，自包含）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ====================================================
# 路径常量：全部基于 skill 自己目录，不引用项目根
# ====================================================
SKILL_DIR = Path(__file__).resolve().parent.parent  # skills/stock-brief/
CONFIG_DIR = SKILL_DIR / "config"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.json"
WATCHLIST_EXAMPLE = CONFIG_DIR / "watchlist.example.json"

CN_TZ = ZoneInfo("Asia/Shanghai")

# Mode 常量
MODE_AUTO = "auto"
MODE_MARKET_ONLY = "market-only"
MODE_FULL = "full"
MODE_WATCHLIST = "watchlist"
MODE_SKIP = "skip"
ALL_MODES = {MODE_MARKET_ONLY, MODE_FULL, MODE_WATCHLIST, MODE_SKIP}


def _log(msg: str) -> None:
    print(f"[stock-brief] {msg}", file=sys.stderr, flush=True)


# ====================================================
# 环境自检（启动时跑，缺啥报啥）
# ====================================================
def _is_cn_locale() -> bool:
    """检测当前是否在中国时区（用 TZ 环境变量或 UTC 偏移判断）。

    用于在缺依赖时自动推荐清华 PyPI 镜像，提升国内用户安装速度。
    """
    # 1) 优先看 TZ 环境变量
    tz_env = os.environ.get("TZ", "")
    if tz_env and ("Asia/Shanghai" in tz_env or "Asia/Chongqing" in tz_env or "Asia/Hong_Kong" in tz_env or "CST" in tz_env):
        return True
    # 2) fallback：用当前 UTC 偏移判断（中国是 +08:00）
    try:
        offset = _now_cn().utcoffset()
        return offset is not None and offset.total_seconds() == 8 * 3600
    except Exception:
        return False


def _pip_mirror_hint() -> str:
    """根据 locale 返回 pip install 镜像提示。"""
    if _is_cn_locale():
        return " -i https://pypi.tuna.tsinghua.edu.cn/simple"
    return ""


def _check_environment() -> list[str]:
    """返回缺失项清单（空 list 表示全部就绪）。"""
    missing = []
    pip_hint = _pip_mirror_hint()  # 一次性算出，整个会话内 pip install 都建议带这个
    try:
        import akshare  # noqa: F401
    except ImportError:
        missing.append(f"akshare (pip install akshare>=1.18{pip_hint})")
    try:
        import baostock  # noqa: F401
    except ImportError:
        missing.append(f"baostock (pip install baostock>=0.8{pip_hint})")
    try:
        import pandas  # noqa: F401
    except ImportError:
        missing.append(f"pandas (pip install pandas>=2.0{pip_hint})")
    if not WATCHLIST_PATH.exists():
        missing.append(
            f"watchlist 配置 ({WATCHLIST_PATH}) — 复制 {WATCHLIST_EXAMPLE} 后填你的自选股"
        )
    return missing


# ====================================================
# 时间窗口
# ====================================================
def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def determine_mode(now: datetime | None = None) -> str:
    """根据北京时间窗口返回 mode。

    06:00-15:00 (盘前/盘中) → market-only
    15:01-22:00 (盘后)        → full
    22:01-05:59 (深夜)        → skip
    """
    now = now or _now_cn()
    m = now.hour * 60 + now.minute
    if 22 * 60 + 1 <= m or m < 6 * 60:
        return MODE_SKIP
    if 6 * 60 <= m < 15 * 60 + 1:
        return MODE_MARKET_ONLY
    return MODE_FULL


def is_quiet_hours(now: datetime | None = None) -> bool:
    return determine_mode(now) == MODE_SKIP


# ====================================================
# 交易日检查（直接调 baostock，无项目耦合）
# ====================================================
def _is_trading_day() -> bool:
    import baostock as bs
    try:
        today = _now_cn().strftime("%Y-%m-%d")
        rs = bs.query_trade_dates(start_date=today, end_date=today)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return False
        return rows[0][1] == "1"  # is_trading_day
    except Exception as e:
        _log(f"⚠️  交易日检查失败 ({type(e).__name__})，默认按交易日处理")
        return True
    finally:
        try:
            bs.logout()
        except Exception:
            pass


# ====================================================
# akshare 数据采集（自包含，零项目耦合）
# ====================================================
def _safe_df(label: str, fn, retries: int = 3, backoff: float = 2.0):
    """带重试的 akshare DataFrame 包装。失败返回 None。"""
    import pandas as pd
    last_err = None
    for i in range(retries):
        try:
            df = fn()
            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                _log(f"⚠️  {label}: 返回空")
                return pd.DataFrame()
            return df
        except Exception as e:
            last_err = e
            _log(f"⚠️  {label} 第 {i+1}/{retries} 次失败: {type(e).__name__}: {e}")
            time.sleep(backoff)
    _log(f"❌ {label}: 重试 {retries} 次后仍失败: {last_err}")
    return None


def _df_to_records(obj):
    """DataFrame → records（dict-friendly）；其他对象原样返回。"""
    import pandas as pd
    if isinstance(obj, pd.DataFrame):
        return obj.fillna("").to_dict(orient="records")
    return obj


def _yesterday_str() -> str:
    return (_now_cn() - timedelta(days=1)).strftime("%Y%m%d")


def _collect_layer1() -> dict:
    """Layer 1 全市场基础数据：龙虎榜 + 大宗 + 融资融券 + 北向 + 公告 + 新闻。
    注：不包含板块（curl_cffi 太重）和游资聚合（依赖 seats.json 配置）。
    """
    import akshare as ak
    socket.setdefaulttimeout(15)  # akshare 兜底超时

    yesterday = _yesterday_str()
    last_week = (_now_cn() - timedelta(days=7)).strftime("%Y%m%d")
    today = _now_cn().strftime("%Y%m%d")

    return {
        "as_of":       _now_cn().isoformat(timespec="seconds"),
        "yesterday":   yesterday,
        "lhb":         _df_to_records(_safe_df("龙虎榜", lambda: ak.stock_lhb_detail_daily_sina(date=yesterday))),
        "dzjy":        _df_to_records(_safe_df("大宗交易", lambda: ak.stock_dzjy_mrtj(start_date=yesterday, end_date=today))),
        "margin_sse":  _df_to_records(_safe_df("融资融券-沪", lambda: ak.stock_margin_detail_sse(date=yesterday))),
        "margin_szse": _df_to_records(_safe_df("融资融券-深", lambda: ak.stock_margin_detail_szse(date=yesterday))),
    }


def _safe_stock_basic(code: str, name: str, fn) -> dict:
    """单股采集的容错包装。失败返回 {_error: ...}。"""
    try:
        return fn()
    except Exception as e:
        _log(f"⚠️  {name}({code}) 采集失败: {type(e).__name__}: {e}")
        return {"_error": f"{type(e).__name__}: {e}"}


# ====================================================
# 单股 11 维数据（A 股，直接调 akshare + baostock）
# ====================================================
def _bs_code(code: str) -> str:
    """6 位 code → baostock 格式（沪 sh. / 深 sz. / 北 bj.）"""
    if code.startswith(("60", "68", "90")):
        return f"sh.{code}"
    if code.startswith(("00", "30", "20")):
        return f"sz.{code}"
    if code.startswith(("43", "83", "87")):
        return f"bj.{code}"
    return f"sh.{code}"


def _a_gather_one(code: str, name: str, prefer_realtime: bool) -> dict:
    """A 股单股 11 维数据采集。"""
    import akshare as ak
    import baostock as bs
    import pandas as pd

    yesterday = _yesterday_str()
    last_week = (_now_cn() - timedelta(days=7)).strftime("%Y%m%d")
    last_month = (_now_cn() - timedelta(days=30)).strftime("%Y%m%d")
    payload: dict = {"code": code, "name": name, "market": "a"}

    # ---- 1. 行情快照 ----
    try:
        spot = ak.stock_zh_a_spot_em()
        row = spot[spot["代码"] == code]
        if not row.empty:
            payload["行情快照"] = {
                "最新价":   row.iloc[0].get("最新价"),
                "涨跌幅":   row.iloc[0].get("涨跌幅"),
                "成交额":   row.iloc[0].get("成交额"),
                "市盈率-动态": row.iloc[0].get("市盈率-动态"),
                "总市值":   row.iloc[0].get("总市值"),
            }
    except Exception as e:
        payload["行情快照"] = {"_error": f"{type(e).__name__}: {e}"}

    # ---- 2. K 线 + MA + MACD（baostock 取日 K）----
    try:
        bs.login()
        rs = bs.query_history_k_data_plus(
            _bs_code(code),
            "date,open,high,low,close,volume",
            start_date=(_now_cn() - timedelta(days=120)).strftime("%Y-%m-%d"),
            end_date=_now_cn().strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=["date","open","high","low","close","volume"])
        for col in ("open","high","low","close","volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna()
        if not df.empty:
            df["MA5"]  = df["close"].rolling(5).mean()
            df["MA10"] = df["close"].rolling(10).mean()
            df["MA20"] = df["close"].rolling(20).mean()
            # MACD
            ema12 = df["close"].ewm(span=12, adjust=False).mean()
            ema26 = df["close"].ewm(span=26, adjust=False).mean()
            df["DIF"] = ema12 - ema26
            df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
            df["MACD"] = (df["DIF"] - df["DEA"]) * 2
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else last
            ma_arr = "多头排列" if last["MA5"] > last["MA10"] > last["MA20"] else (
                     "空头排列" if last["MA5"] < last["MA10"] < last["MA20"] else "震荡")
            macd_cross = "金叉" if last["DIF"] > last["DEA"] and prev["DIF"] <= prev["DEA"] else (
                         "死叉" if last["DIF"] < last["DEA"] and prev["DIF"] >= prev["DEA"] else "—")
            close_5d_ago = df["close"].iloc[-6] if len(df) >= 6 else df["close"].iloc[0]
            payload["K线技术"] = {
                "收盘":         float(last["close"]),
                "当日涨跌幅%":  round((last["close"] / prev["close"] - 1) * 100, 2) if prev["close"] else 0,
                "5日累计涨跌幅%": round((last["close"] / close_5d_ago - 1) * 100, 2) if close_5d_ago else 0,
                "MA5":  round(float(last["MA5"]), 2),
                "MA10": round(float(last["MA10"]), 2),
                "MA20": round(float(last["MA20"]), 2),
                "均线排列": ma_arr,
                "MACD金叉死叉": macd_cross,
                "MACD柱":      round(float(last["MACD"]), 4),
            }
    except Exception as e:
        payload["K线技术"] = {"_error": f"{type(e).__name__}: {e}"}
    finally:
        try: bs.logout()
        except Exception: pass

    # ---- 3. 龙虎榜命中 ----
    try:
        lhb_df = ak.stock_lhb_detail_daily_sina(date=yesterday)
        if lhb_df is not None and not lhb_df.empty:
            hit = lhb_df[lhb_df["股票代码"] == code]
            payload["龙虎榜命中"] = _df_to_records(hit) if not hit.empty else []
    except Exception as e:
        payload["龙虎榜命中"] = {"_error": f"{type(e).__name__}: {e}"}

    # ---- 4. 大宗交易 ----
    payload["大宗交易"] = _df_to_records(_safe_df(
        f"{name}-大宗交易",
        lambda: ak.stock_dzjy_mrtj(start_date=last_week, end_date=_now_cn().strftime("%Y%m%d")).pipe(
            lambda d: d[d["证券代码"] == code] if not d.empty else d
        ),
    ))

    # ---- 5. 融资融券（合并 sse+szse，命中当前 code）----
    margin_hits = []
    for label, fn in [
        ("融资融券-沪", lambda: ak.stock_margin_detail_sse(date=yesterday)),
        ("融资融券-深", lambda: ak.stock_margin_detail_szse(date=yesterday)),
    ]:
        df = _safe_df(label, fn)
        if df is not None and not df.empty and "标的证券代码" in df.columns:
            hit = df[df["标的证券代码"] == code]
            if not hit.empty:
                margin_hits.extend(_df_to_records(hit))
    payload["融资融券"] = margin_hits

    # ---- 6. 股东户数 ----
    payload["股东户数趋势"] = _df_to_records(_safe_df(
        f"{name}-股东户数",
        lambda: ak.stock_zh_a_gdhs(symbol=code),
    ))

    # ---- 7. 公告（近 30 日）----
    payload["近 30 日公告"] = _df_to_records(_safe_df(
        f"{name}-公告",
        lambda: ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京", category="",
            start_date=last_month, end_date=_now_cn().strftime("%Y%m%d"),
        ),
    ))

    # ---- 8. 新闻 ----
    payload["近期新闻"] = _df_to_records(_safe_df(
        f"{name}-新闻",
        lambda: ak.stock_news_em(symbol=code),
    ))

    return payload


def _collect_layer2(prefer_realtime: bool = False) -> list:
    """Layer 2 自选股深度。"""
    wl = json.loads(WATCHLIST_PATH.read_text())["watchlist"]
    a_stocks = [s for s in wl if s.get("market", "a") == "a"]
    return [_a_gather_one(s["code"], s["name"], prefer_realtime) for s in a_stocks]


# ====================================================
# 一站式入口
# ====================================================
def collect(mode: str = MODE_AUTO, *, force: bool = False) -> dict:
    """采集今天简报所需的结构化数据。

    Args:
        mode:  "auto" | "market-only" | "full" | "watchlist" | "skip"
        force: True 跳过交易日历检查

    Returns:
        {
          "mode": str,
          "skipped_reason": str | None,
          "data": {"as_of": ..., "layer1": {...}, "layer2": [...]} | None,
          "duration_sec": float,
        }
    """
    start = time.monotonic()
    now = _now_cn()

    # ---- Step 0: 环境自检 ----
    missing = _check_environment()
    if missing:
        raise RuntimeError(
            "skill 环境未就绪，缺以下项：\n  - " + "\n  - ".join(missing)
        )

    # ---- Step 1: mode 决议 ----
    if mode == MODE_AUTO:
        effective = determine_mode(now)
    elif mode in ALL_MODES:
        effective = mode
    else:
        raise ValueError(f"invalid mode: {mode!r}")

    # ---- Step 2: 深夜窗口 → skip ----
    if effective == MODE_SKIP:
        return {
            "mode": MODE_SKIP,
            "skipped_reason": f"深夜窗口（{now.strftime('%H:%M')}）数据源不稳定，跳过",
            "data": None,
            "duration_sec": time.monotonic() - start,
        }

    # ---- Step 3: 交易日检查（非强制）----
    if not force and not _is_trading_day():
        wd = now.strftime("%A")
        return {
            "mode": effective,
            "skipped_reason": f"今天非 A 股交易日（{wd}），跳过（用 force=True 强制跑）",
            "data": None,
            "duration_sec": time.monotonic() - start,
        }

    _log(f"开始采集 mode={effective} 北京时间 {now.strftime('%H:%M')}")

    # ---- Step 4: Layer 1 全市场 ----
    layer1 = _collect_layer1()

    # ---- Step 5: Layer 2（仅 watchlist 模式）----
    layer2 = None
    if effective == MODE_WATCHLIST:
        layer2 = _collect_layer2(prefer_realtime=(15 > now.hour >= 9))

    return {
        "mode": effective,
        "skipped_reason": None,
        "data": {
            "as_of":  now.isoformat(timespec="seconds"),
            "layer1": layer1,
            "layer2": layer2,
        },
        "duration_sec": time.monotonic() - start,
    }


# ====================================================
# CLI
# ====================================================
def main() -> int:
    p = argparse.ArgumentParser(
        description="stock-brief skill — 自包含、零项目耦合的 A 股数据采集器"
    )
    p.add_argument("--mode", choices=[MODE_AUTO, MODE_MARKET_ONLY, MODE_FULL, MODE_WATCHLIST, MODE_SKIP],
                   default=MODE_AUTO)
    p.add_argument("--force", action="store_true", help="跳过交易日历检查")
    p.add_argument("--out", type=str, default=None, help="把 data dict JSON 序列化到文件")
    p.add_argument("--check", action="store_true", help="只跑环境自检，不采集")
    args = p.parse_args()

    print("🤖 stock-brief skill（自包含 / 零项目耦合）", file=sys.stderr)
    print(f"   现在（北京时间）：{_now_cn().strftime('%Y-%m-%d %H:%M:%S %Z')}", file=sys.stderr)
    print(f"   skill 目录：{SKILL_DIR}", file=sys.stderr)
    print("-" * 60, file=sys.stderr)

    # 启动时自检
    missing = _check_environment()
    if missing:
        print("❌ 环境未就绪，缺：", file=sys.stderr)
        for m in missing:
            print(f"   - {m}", file=sys.stderr)
        pip_hint = _pip_mirror_hint()
        install_cmd = f"pip install -r requirements.txt{pip_hint}"
        print(f"\n💡 修复：cd {SKILL_DIR} && {install_cmd}", file=sys.stderr)
        if _is_cn_locale():
            print("   （已自动添加清华 PyPI 镜像，国内安装更快）", file=sys.stderr)
        if WATCHLIST_PATH.exists() is False and WATCHLIST_EXAMPLE.exists():
            print(f"   cp {WATCHLIST_EXAMPLE.name} {WATCHLIST_PATH.name}", file=sys.stderr)
        return 1

    if args.check:
        print("✅ 环境自检通过", file=sys.stderr)
        return 0

    result = collect(mode=args.mode, force=args.force)

    print("-" * 60, file=sys.stderr)
    print(f"✅ mode          : {result['mode']}", file=sys.stderr)
    if result["skipped_reason"]:
        print(f"⏭  skipped      : {result['skipped_reason']}", file=sys.stderr)
    if result["data"]:
        layers = [k for k in ("layer1", "layer2") if result["data"].get(k) is not None]
        print(f"📦 layers        : {layers}", file=sys.stderr)
    print(f"⏱  duration_sec  : {result['duration_sec']:.1f}s", file=sys.stderr)

    payload = json.dumps(result, ensure_ascii=False, default=str)
    if args.out:
        Path(args.out).write_text(payload)
        print(f"💾 写入 {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
