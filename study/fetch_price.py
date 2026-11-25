# -*- coding: utf-8 -*-
"""研究用价格数据抓取：批量下载 ETF/指数日线，存 CSV 到 study/price/。

一次性准备 + 每周补增量均可。依赖：pip install akshare
运行：python study/fetch_price.py
"""
import os
import time
from pathlib import Path

import akshare as ak
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


def fetch_one(code: str, fname: str, note: str) -> None:
    path = OUT / f"{fname}.csv"
    try:
        df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                 start_date="20180101", end_date="20500101", adjust="hfq")
        df.to_csv(path, index=False, encoding="utf-8-sig")
        n = len(df)
        print(f"[OK] {code} {note} -> {n} 行", flush=True)
    except Exception as e:  # 单个失败不拖垮整批
        print(f"[FAIL] {code} {note}: {e}", flush=True)


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
        if path.exists() and (not before or path.stat().st_mtime > time.time() - 120):
            ok += 1
        else:
            fail += 1
        time.sleep(1.0)  # 别太猛，给源站留面子
    write_status(ok, fail)
    print(f"全部完成（成功 {ok}，失败 {fail}）。CSV 在 study/price/", flush=True)
