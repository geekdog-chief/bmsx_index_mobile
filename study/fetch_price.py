# -*- coding: utf-8 -*-
"""研究用价格数据抓取：批量下载 ETF/指数日线，存 CSV 到 study/price/。

一次性准备 + 定时补增量均可。依赖：pip install akshare
运行：python study/fetch_price.py

防封设计（2026-11-27 K7 整改，用户批准）：
1. 每次请求随机轮换浏览器 UA + Referer，摘掉 python-requests 机器指纹（封禁主因是
   指纹+节奏，不是流量：一天 13 只基金仅几十个请求）；
2. 基金间隔 15-35 秒随机抖动，去固定节奏特征；
3. 单轮熔断：任一基金触发风控类报错 → 当轮立即整体退出。IP 被封期间撞墙会加重
   处罚、延长封禁，停手等解封才是正解（原 60 秒重试 + 120 秒接力 = 封禁期 15 分钟
   撞 26 次墙，自残）；
4. 定时任务每 3 日一次（安装价格任务.cmd /MO 3），暴露频率降 2/3，研究日线滞后无影响。
"""
import os
import random
import time
from pathlib import Path

try:
    import akshare as ak
except ImportError:
    ak = None  # 惰性失败：仅抓取时需要，测试环境可导入本模块
import pandas as pd

OUT = Path(__file__).resolve().parent / "price"
OUT.mkdir(parents=True, exist_ok=True)

# 代码: (文件名, 说明)  —— 映射以 RESEARCH_PLAN.md 终稿为准
FUNDS = {
    "510300": ("hs300_510300", "沪深300ETF（基准）"),
    "159995": ("chip_159995", "芯片ETF"),
    "515880": ("comm_515880", "通信ETF"),
    "512690": ("wine_512690", "酒ETF"),
    "512070": ("broker_512070", "证券保险ETF"),
    "512200": ("realestate_512200", "房地产ETF"),
    "512800": ("bank_512800", "银行ETF"),
    "516950": ("infra_516950", "基建ETF（流动性薄）"),
    "159611": ("power_159611", "电力ETF"),
    "159992": ("innovdrug_159992", "创新药ETF"),
    "159883": ("meddevice_159883", "医疗器械ETF"),
    "159562": ("goldstock_159562", "黄金股ETF"),
    "159770": ("robot_159770", "机器人ETF"),
}

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]
_REFERERS = [
    "https://quote.eastmoney.com/",
    "https://fund.eastmoney.com/",
    "https://data.eastmoney.com/",
]

_STEALTH_APPLIED = False


def _apply_stealth_headers():
    """monkeypatch requests：每次请求注入随机浏览器 UA + Referer。惰性、幂等。
    只在真正抓取时生效（测试环境 import 本模块不受影响）。"""
    global _STEALTH_APPLIED
    if _STEALTH_APPLIED:
        return
    try:
        import requests
    except ImportError:
        return
    orig = requests.sessions.Session.request

    def patched(self, method, url, **kw):
        h = dict(kw.get("headers") or {})
        h.setdefault("User-Agent", random.choice(_UA_POOL))
        h.setdefault("Referer", random.choice(_REFERERS))
        kw["headers"] = h
        return orig(self, method, url, **kw)

    requests.sessions.Session.request = patched
    _STEALTH_APPLIED = True


def _is_rate_limit(msg: str) -> bool:
    """东财风控类报错识别：触发即熔断，不撞墙。"""
    return any(k in msg for k in ("限流", "断开", "push2", "反爬", "风控", "频繁", "403"))


def _last_date(path) -> str:
    """已有 CSV 的最后日期 → 增量起点；无文件则从头抓。"""
    if not path.exists():
        return "20180101"
    try:
        import pandas as pd
        df = pd.read_csv(path)
        col = "日期" if "日期" in df.columns else df.columns[0]
        last = str(df[col].iloc[-1])[:10].replace("-", "")
        return last  # 从最后一条重拉一天，保证衔接
    except Exception:
        return "20180101"


def merge_frames(old_df, new_df):
    """合并新旧行情：按日期去重（新数据优先）、升序排序。纯函数，供测试。"""
    col = "日期"
    df = pd.concat([old_df, new_df], ignore_index=True)
    df = df.drop_duplicates(subset=[col], keep="last")
    return df.sort_values(col).reset_index(drop=True)


def fetch_one(code: str, fname: str, note: str) -> object:
    """返回 True=成功 / False=失败 / "banned"=触发东财风控（主循环熔断）。"""
    path = OUT / f"{fname}.csv"
    if ak is None:
        print(f"[FAIL] {code} {note}: akshare 未安装（pip install akshare）", flush=True)
        return False
    _apply_stealth_headers()
    start = _last_date(path)
    for attempt in (1, 2):
        try:
            df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                     start_date=start, end_date="20500101", adjust="hfq")
            if path.exists() and start != "20180101":
                old_df = pd.read_csv(path)
                df = merge_frames(old_df, df)
            df.to_csv(path, index=False, encoding="utf-8-sig")
            print(f"[OK] {code} {note} -> 累计 {len(df)} 行（本次起点 {start}）", flush=True)
            return True
        except Exception as e:
            msg = str(e)
            if _is_rate_limit(msg):
                # 熔断：封禁期任何重试都是撞墙，交给主循环整轮退出
                print(f"[BAN] {code} {note}: 东财风控类报错（{msg[:80]}）→ 当轮熔断", flush=True)
                return "banned"
            print(f"[{'重试' if attempt == 1 else 'FAIL'}] {code} {note}: {msg[:100]}", flush=True)
            time.sleep(random.uniform(15, 35))  # 瞬时错误抖动重试一次
    return False


def write_status(ok: int, fail: int, banned: bool = False) -> None:
    """心跳：写最后运行状态，断更检测用（超过阈值没更新 = akshare/东财挂了）。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    (OUT / "_last_run.txt").write_text(
        f"last_run: {stamp}\nok: {ok}\nfail: {fail}\nbanned: {1 if banned else 0}\n",
        encoding="utf-8")


if __name__ == "__main__":
    ok = fail = 0
    banned = False
    for code, (fname, note) in FUNDS.items():
        r = fetch_one(code, fname, note)
        if r == "banned":
            banned = True
            fail += 1
            break  # 熔断：整体退出，剩余基金下一轮再补（封禁期不撞墙）
        if r:
            ok += 1
        else:
            fail += 1
        time.sleep(random.uniform(15, 35))  # 东财反爬对固定节奏敏感（2026-11-27 整改）
    write_status(ok, fail, banned=banned)
    tag = "（触发熔断：疑似被封，停手等解封，勿手动连跑）" if banned else ""
    print(f"全部完成（成功 {ok}，失败 {fail}）{tag}。CSV 在 study/price/", flush=True)
