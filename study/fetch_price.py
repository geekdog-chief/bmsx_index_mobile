# -*- coding: utf-8 -*-
"""研究用价格数据抓取：批量下载 ETF/指数日线，存 CSV 到 study/price/。

一次性准备 + 每周补增量均可。依赖：pip install akshare
运行：python study/fetch_price.py
"""
import os
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


def fetch_one(code: str, fname: str, note: str) -> None:
    path = OUT / f"{fname}.csv"
    if ak is None:
        print(f"[FAIL] {code} {note}: akshare 未安装（pip install akshare）", flush=True)
        return
    start = _last_date(path)
    for attempt in (1, 2):  # 限流时等 60 秒重试一次
        try:
            df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                     start_date=start, end_date="20500101", adjust="hfq")
            if path.exists() and start != "20180101":
                old_df = pd.read_csv(path)
                df = merge_frames(old_df, df)
            df.to_csv(path, index=False, encoding="utf-8-sig")
            print(f"[OK] {code} {note} -> 累计 {len(df)} 行（本次起点 {start}）", flush=True)
            return
        except Exception as e:
            msg = str(e)
            print(f"[{'重试' if attempt == 1 else 'FAIL'}] {code} {note}: {msg[:100]}", flush=True)
            if "限流" in msg or "断开" in msg or "push2" in msg:
                time.sleep(60)
            else:
                return  # 非限流错误不重试
    # 两次都失败：等久点让下个基金接力（整体节奏放慢）
    time.sleep(120)


def write_status(ok: int, fail: int) -> None:
    """心跳：写最后运行状态，断更检测用（超过2天没更新 = akshare 可能挂了）。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    (OUT / "_last_run.txt").write_text(
        f"last_run: {stamp}\nok: {ok}\nfail: {fail}\n", encoding="utf-8")


if __name__ == "__main__":
    ok = fail = 0
    for code, (fname, note) in FUNDS.items():
        path = OUT / f"{fname}.csv"
        before = path.exists()
        fetch_one(code, fname, note)
        if path.exists() and (not before or path.stat().st_mtime > time.time() - 300):
            ok += 1
        else:
            fail += 1
        time.sleep(6.0)  # 东财对高频请求限流（2026-11-25 实测1秒间隔4只后被封），放慢到6秒
    write_status(ok, fail)
    print(f"全部完成（成功 {ok}，失败 {fail}）。CSV 在 study/price/", flush=True)
