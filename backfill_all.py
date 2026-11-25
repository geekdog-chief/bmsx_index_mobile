# -*- coding: utf-8 -*-
"""羊驼全量补跑：循环调用 llm_overlay.py 直到队列清空，然后自动重建指数并推送。

用法：双击 补跑全量.cmd（即 py backfill_all.py），或命令行 py backfill_all.py
断点续跑：STORE 每批落盘，中断后重开本脚本自动从断点继续。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_ROUNDS = 150          # 正常约72轮，保护上限
IDLE_LIMIT = 3            # 连续无进度轮数，判定 Ollama 掉线

def one_round() -> str:
    r = subprocess.run([sys.executable, "llm_overlay.py"],
                       capture_output=True, text=True, cwd=ROOT, encoding="utf-8", errors="replace")
    return (r.stdout or "") + (r.stderr or "")

def main() -> int:
    print("=" * 50)
    print("羊驼全量补跑：循环打分直到无剩余，完成后自动重建并推送")
    print("每批7条实时存档，随时可关，重开自动续跑")
    print("=" * 50, flush=True)
    idle = 0
    for rnd in range(1, MAX_ROUNDS + 1):
        out = one_round()
        (ROOT / "data" / "backfill_last.log").write_text(out, encoding="utf-8")
        m = re.search(r"本轮待打 (\d+) 条.*?未排上 (\d+) 条", out)
        stamp = time.strftime("%H:%M:%S")
        if "BACKFILL_DONE" in out:
            print(f"[第{rnd}轮 {stamp}] 队列已清空", flush=True)
            break
        if m:
            idle = 0
            print(f"[第{rnd}轮 {stamp}] 本轮待打 {m.group(1)} 条，未排上 {m.group(2)} 条", flush=True)
            continue
        idle += 1
        print(f"[第{rnd}轮 {stamp}] 无进度输出（连续 {idle} 轮）", flush=True)
        if idle >= IDLE_LIMIT:
            print("Ollama 可能掉线：启动 Ollama 后重开本脚本即可续跑", flush=True)
            return 1
    else:
        print(f"超过 {MAX_ROUNDS} 轮保护上限，检查 data/backfill_last.log 后再跑", flush=True)
        return 1

    print("全部补跑完成，自动重建指数并推送...", flush=True)
    r = subprocess.run(["cmd", "/c", "推送数据含LLM.cmd"], cwd=ROOT)
    print("完成。手机杀进程重开，热度榜『情』徽章应全部点亮", flush=True)
    return r.returncode

if __name__ == "__main__":
    sys.exit(main())
