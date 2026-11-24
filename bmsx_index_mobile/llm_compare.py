"""LLM评分实验：对比 Kimi 输出与词典分数，生成 llm对比报告.md。

纯新增文件。输入：llm_package_with_dict.json（基准）+ 用户保存的 llm_scores_N.json。
校验不过就拒绝出报告，绝不把脏数据当结论。
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sentiment import signal_from_rate  # noqa: E402

PANIC_DECAY = 0.12
PERSONA_SET = {"baoma", "novice", "retail", "none", ""}


def load_package() -> dict:
    return json.loads((ROOT / "llm_package_with_dict.json").read_text(encoding="utf-8"))


def load_llm_scores() -> tuple:
    """收集 llm_scores*.json，严格校验。返回 (rows, errors)。"""
    rows = []
    errors = []
    seen = set()
    files = sorted(ROOT.glob("llm_scores*.json"))
    if not files:
        return [], ["找不到 llm_scores*.json —— 先把 Kimi 的回复逐个保存成这些文件"]
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path.name}: 不是合法JSON（{exc}）")
            continue
        if not isinstance(payload, list):
            errors.append(f"{path.name}: 顶层必须是JSON数组")
            continue
        for i, item in enumerate(payload):
            where = f"{path.name} 第{i+1}条"
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                errors.append(f"{where}: 缺字符串id")
                continue
            pid = item["id"]
            if pid in seen:
                errors.append(f"{where}: id重复 {pid}")
                continue
            seen.add(pid)
            direction = item.get("direction")
            if not isinstance(direction, (int, float)) or not -3 <= direction <= 3:
                errors.append(f"{where}: direction 必须是-3~3的数字")
                continue
            row = {"id": pid, "direction": float(direction), "reason": str(item.get("reason") or "")}
            for flag in ("fomo", "panic", "newcomer", "sarcasm", "ai_marketing"):
                row[flag] = 1 if item.get(flag) in (1, "1", True) else 0
            row["persona"] = str(item.get("persona") or "none")
            if row["persona"] not in PERSONA_SET:
                row["persona"] = "none"
            rows.append(row)
    return rows, errors


def dict_signals(posts: list) -> dict:
    """与主流程一致的词典口径聚合（按来源×日）。"""
    wsum = sum(p["weight"] for p in posts) or 1.0
    net = sum(p["weight"] * math.tanh(p["dict"]["direction"] / 2.2) for p in posts) / wsum
    rates = {k: sum(p["weight"] for p in posts if p["dict"][k]) / wsum for k in ("fomo", "panic", "newcomer", "intense")}
    return {
        "optimism": 50.0 + 45.0 * net,
        "fomo": signal_from_rate(rates["fomo"], "fomo"),
        "panic": 100.0 * (1.0 - math.exp(-rates["panic"] / PANIC_DECAY)),
        "newcomer": signal_from_rate(rates["newcomer"], "newcomer"),
        "intensity": signal_from_rate(rates["intense"], "intensity"),
    }


def llm_signals(posts: list) -> dict:
    wsum = sum(p["weight"] for p in posts) or 1.0
    net = sum(p["weight"] * (p["llm"]["direction"] / 3.0) for p in posts) / wsum
    rates = {k: sum(p["weight"] for p in posts if p["llm"][k]) / wsum for k in ("fomo", "panic", "newcomer")}
    intense_rate = sum(p["weight"] for p in posts if abs(p["llm"]["direction"]) >= 2) / wsum
    return {
        "optimism": 50.0 + 45.0 * net,
        "fomo": 100.0 * rates["fomo"],
        "panic": 100.0 * (1.0 - math.exp(-rates["panic"] / PANIC_DECAY)),
        "newcomer": 100.0 * rates["newcomer"],
        "intensity": 100.0 * intense_rate,
    }


def sign(v: float) -> int:
    return 1 if v > 0 else (-1 if v < 0 else 0)


def main() -> int:
    package = load_package()
    base = {p["id"]: p for p in package["posts"]}
    rows, errors = load_llm_scores()
    if errors:
        print("校验失败，不出报告：")
        for e in errors[:20]:
            print(" -", e)
        return 1
    unknown = [r["id"] for r in rows if r["id"] not in base]
    if unknown:
        print(f"校验失败：{len(unknown)} 个id不在帖子包里（示例 {unknown[:3]}），文件可能存错")
        return 1

    scored = []
    for row in rows:
        post = dict(base[row["id"]])
        post["llm"] = row
        scored.append(post)
    missing = len(base) - len(scored)

    by_group: dict = defaultdict(list)
    for p in scored:
        by_group[(p["platform"], p["source"], p["day"])].append(p)

    agree = sum(1 for p in scored if sign(p["dict"]["direction"]) == sign(p["llm"]["direction"]))
    coverage = len(scored) / max(1, len(base))

    lines = []
    lines.append("# LLM vs 词典 对比报告")
    lines.append("")
    lines.append(f"- 帖子包：{len(base)} 帖（{', '.join(package['days'])} 三个交易日）")
    lines.append(f"- LLM 有效评分：{len(scored)} 帖（覆盖率 {coverage:.0%}，未评分 {missing} 帖）")
    lines.append(f"- 方向一致率（看多/看空/中性同号）：{agree}/{len(scored)} = {agree/max(1,len(scored)):.0%}")
    lines.append("")
    lines.append("## 信号级对比（按 来源×交易日 加权聚合）")
    lines.append("")
    lines.append("| 来源 | 交易日 | 信号 | 词典 | LLM | 差 |")
    lines.append("|---|---|---|---|---|---|")
    keys = ("optimism", "fomo", "panic", "newcomer", "intensity")
    names = {"optimism": "乐观", "fomo": "追涨", "panic": "恐慌", "newcomer": "新手", "intensity": "强度"}
    diff_sum: dict = defaultdict(float)
    diff_n: dict = defaultdict(int)
    for (platform, source, day), posts in sorted(by_group.items()):
        d = dict_signals(posts)
        l = llm_signals(posts)
        for k in keys:
            diff = l[k] - d[k]
            diff_sum[k] += diff
            diff_n[k] += 1
            if abs(diff) >= 8:
                lines.append(f"| {platform}/{source} | {day} | {names[k]} | {d[k]:.0f} | {l[k]:.0f} | {diff:+.0f} |")
    lines.append("")
    lines.append("## 平均信号差（LLM − 词典，全部来源×日）")
    lines.append("")
    for k in keys:
        if diff_n[k]:
            lines.append(f"- {names[k]}：{diff_sum[k]/diff_n[k]:+.1f}")
    lines.append("")
    sarcasm = [p for p in scored if p["llm"]["sarcasm"]]
    ai = [p for p in scored if p["llm"]["ai_marketing"]]
    lines.append(f"## 质量标记")
    lines.append("")
    lines.append(f"- LLM 判为讽刺/反语：{len(sarcasm)} 帖；判为AI/营销文：{len(ai)} 帖")
    flip = [p for p in scored if sign(p["dict"]["direction"]) != sign(p["llm"]["direction"]) and p["dict"]["direction"] != 0 and p["llm"]["direction"] != 0]
    lines.append(f"- 方向完全相反：{len(flip)} 帖")
    lines.append("")
    lines.append("## 分歧最大的帖子（词典 vs LLM）")
    lines.append("")
    flip.sort(key=lambda p: abs(p["llm"]["direction"] - p["dict"]["direction"]), reverse=True)
    for p in flip[:10]:
        lines.append(f"- `{p['id']}` 词典{p['dict']['direction']:+d}（{p['dict']['label']}） vs LLM{p['llm']['direction']:+.0f}，讽刺={p['llm']['sarcasm']}")
        lines.append(f"  > {p['title'][:60]}")
    lines.append("")
    lines.append("## 口径说明")
    lines.append("")
    lines.append("- 词典信号经 tanh 饱和曲线压缩，LLM 标记率为线性——两者绝对值不可直接比，只看差的方向和量级")
    lines.append("- 恐慌信号两边用同一衰减公式换算")
    lines.append("- 关注(attention)是活动量口径、与文本无关，不在对比范围")

    report = "\n".join(lines)
    (ROOT / "llm对比报告.md").write_text(report, encoding="utf-8")
    print(report[:1500])
    print("...")
    print("完整报告已写入 llm对比报告.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
