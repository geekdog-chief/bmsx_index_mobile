# -*- coding: utf-8 -*-
"""Ollama 一体化脚本：打分 + 三向对比（唯一需要跑的脚本）

用途：用本地 Ollama（gemma4）对 llm_package_1..4.json 共 200 帖打分，
输出 llm_scores_ollama.json，与 Kimi 第 1 天结果做三向对比。

prompt 与「发给Kimi的指令.md」逐字一致，保证可比。

用法：
    python run_ollama_scoring.py                # 全量
    python run_ollama_scoring.py --model gemma4
中断后重跑会自动跳过已完成的帖（断点续跑）。

前置：ollama serve 已在跑（默认 localhost:11434），模型已 pull。
测速：Measure-Command { ollama run gemma4 "用一句话评价A股" | Out-Null }
"""

import json, os, sys, time, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = BASE if os.path.exists(os.path.join(BASE, "llm_package_1.json")) else os.path.join(BASE, "llm_experiment_day1")
API = "http://localhost:11434/api/chat"

MODEL = "gemma4:12b"
for i, a in enumerate(sys.argv):
    if a == "--model" and i + 1 < len(sys.argv):
        MODEL = sys.argv[i + 1]

OUT = os.path.join(BASE, f"llm_scores_ollama_{MODEL.replace(":", "_")}.json")
MISSING = os.path.join(BASE, f"llm_scores_ollama_{MODEL.replace(":", "_")}_missing.txt")

# 与发给Kimi的指令.md 完全相同的 prompt（一字不改）
SYSTEM_PROMPT = """你是一名A股社区情绪标注员。接下来我会逐个上传 JSON 文件，每个文件里有若干条股民帖子（字段：id、day、平台、source、title）。请对每条帖子输出评分，严格按以下规则：

**方向分 direction**：整数 -3 到 +3。
+3 强烈看多，+2 明确看多，+1 轻度偏多，0 中性/无关/说了一堆等于没说，-1 轻度偏空，-2 明确看空，-3 强烈看空/恐慌。
注意识别反语和讽刺："呵呵，又要涨了"、"太厉害了，又创新高（指暴跌后）"这类表面夸奖实际看空，按真实意图给负分。
注意识别 AI 生成或营销文：内容是车轱辘话、无真实观点、明显引流性质的，direction 给 0。

**标记（0 或 1）**：
- ai_marketing：AI生成或营销文。**最高优先级，先判断这条再判断其他标记。** 特征：①研报/资讯腔的编号列表、机械罗列利好（如"1.荣盛石化—增持金额居全场之首利好：控股股东拟增持10亿"）；②模板化看多口号、空洞无具体股票操作的排比句（如"当下就是底部最佳布局窗口期，紧跟资金切换节奏"）；③水军短句、纯夸品牌（如"海尔品牌，值得信赖"）；④诱导加群、看分析、点链接的引流话（如"看分析在这"）。以上任何一条命中就给1，宁可多判不可漏判。
- fomo：追涨式表达（梭哈、满仓、上车、怕踏空、最后机会）
- panic：恐慌表达（割肉、清仓、跑路、完了、跌停恐慌）
- newcomer：新手提问（能买吗、怎么办、求带、什么是、目标价）
- sarcasm：讽刺/反语（按上面定义）

**persona**（该帖作者的表达身份，四选一）：
- baoma：家庭/生活视角的稳健表达（提到家庭、孩子、房贷、闲钱、定投、长期持有）
- novice：新手求教表达
- retail：交易/博弈视角表达（追涨杀跌、技术分析、仓位）
- none：以上都不是

**reason**：一句话说明判断依据，不超过15个字。

**输出格式（极其重要）**：
- 只输出一个 JSON 数组，不要输出任何其他文字、解释、markdown代码块标记
- 每条元素格式：{"id": "原样复制", "direction": 整数, "fomo": 0, "panic": 0, "newcomer": 0, "sarcasm": 0, "ai_marketing": 0, "persona": "baoma", "reason": "不超过15字"}
- 每个 id 恰好出现一次，顺序不限，一个都不能少
- 拿不准的一律 direction 给 0，宁可保守不可编造"""

USER_PREFIX = "请评分这个文件里的帖子，只输出JSON数组。\n\n"


def chat(prompt_user, retries=3):
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_user},
        ],
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": 0, "num_predict": 16384},
    }
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=900) as r:
                resp = json.loads(r.read().decode("utf-8"))
            return resp["message"]["content"]
        except Exception as e:
            last = e
            wait = 5 * (attempt + 1)
            print(f"  请求失败({attempt+1}/{retries}): {e}，{wait}s 后重试", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Ollama 请求 {retries} 次失败: {last}")


def parse_scores(text):
    """兼容三种模型输出：JSON数组 / JSON对象(id当键) / 带代码围栏。"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        t = "\n".join(lines).strip()
    start = t.find("[")
    if start >= 0:
        data = json.loads(extract_json_array(t))
        if isinstance(data, list):
            return data
    dec = json.JSONDecoder()
    out = []
    pos = 0
    while pos < len(t):
        while pos < len(t) and t[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(t):
            break
        obj, end = dec.raw_decode(t, pos)
        if isinstance(obj, dict):
            if "id" in obj and "direction" in obj:
                out.append(obj)
            else:
                for k, v in obj.items():
                    if isinstance(v, dict):
                        v = dict(v)
                        v.setdefault("id", k)
                        out.append(v)
        elif isinstance(obj, list):
            out.extend(x for x in obj if isinstance(x, dict))
        pos = end
    if out:
        return out
    raise ValueError("不是帖子评分的JSON")


def extract_json_array(text):
    """从模型输出里抠出第一个完整 JSON 数组。"""
    start = text.find("[")
    if start < 0:
        raise ValueError("输出里没有 [")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("JSON 数组不完整（可能被截断）")


def normalize(item):
    return {
        "id": str(item["id"]),
        "direction": int(item["direction"]),
        "fomo": int(bool(item.get("fomo", 0))),
        "panic": int(bool(item.get("panic", 0))),
        "newcomer": int(bool(item.get("newcomer", 0))),
        "sarcasm": int(bool(item.get("sarcasm", 0))),
        "ai_marketing": int(bool(item.get("ai_marketing", 0))),
        "persona": str(item.get("persona", "none")),
        "reason": str(item.get("reason", ""))[:30],
    }


def main():
    # 断点续跑：读已有结果
    done = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            for it in json.load(f):
                done[it["id"]] = it
        print(f"断点续跑：已完成 {len(done)} 条")

    results = dict(done)
    failed = []
    t0 = time.time()

    def score_posts(posts):
        """对一批帖子打分；失败自动对半拆小重试。返回仍失败的 id 列表。"""
        if not posts:
            return []
        user = USER_PREFIX + json.dumps(
            {"posts": [{"id": p["id"], "day": p["day"], "platform": p["platform"],
                        "source": p["source"], "title": p["title"]} for p in posts]},
            ensure_ascii=False,
        )
        for attempt in range(3):
            try:
                text = chat(user)
                arr = parse_scores(text)
                got = {str(x["id"]): normalize(x) for x in arr}
                missing = [p["id"] for p in posts if p["id"] not in got]
                if missing:
                    raise ValueError(f"id 缺失 {len(missing)} 条: {missing[:5]}...")
                for pid, it in got.items():
                    results[pid] = it
                return []
            except Exception as e:
                snippet = ""
                try:
                    snippet = " 模型原话: " + text[:150].replace("\n", " ")
                except UnboundLocalError:
                    pass
                print(f"  解析失败({attempt+1}/3): {e}{snippet}", flush=True)
                time.sleep(5)
        if len(posts) == 1:
            return [posts[0]["id"]]
        half = len(posts) // 2
        print(f"  拆包：{len(posts)} 条 → {half}+{len(posts)-half}", flush=True)
        return score_posts(posts[:half]) + score_posts(posts[half:])

    for n in range(1, 5):
        path = os.path.join(PKG_DIR, f"llm_package_{n}.json")
        with open(path, encoding="utf-8") as f:
            posts = json.load(f)["posts"]
        todo = [p for p in posts if p["id"] not in results]
        if not todo:
            print(f"llm_package_{n}.json: 全部已完成，跳过")
            continue
        print(f"llm_package_{n}.json: {len(todo)}/{len(posts)} 条待打分", flush=True)
        B = 10  # 12B 输出预算紧，10条/包
        for s in range(0, len(todo), B):
            failed.extend(score_posts(todo[s:s + B]))

        # 每包落盘一次，防断电
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(list(results.values()), f, ensure_ascii=False, indent=1)
        el = time.time() - t0
        print(f"  累计 {len(results)}/200，用时 {el:.0f}s", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(list(results.values()), f, ensure_ascii=False, indent=1)

    if failed:
        with open(MISSING, "w", encoding="utf-8") as f:
            f.write("\n".join(failed))
        print(f"打分完成 {len(results)}/200，失败 {len(failed)} 条，见 {MISSING}")
    else:
        if os.path.exists(MISSING):
            os.remove(MISSING)
        print(f"打分完成 {len(results)}/200，全部成功。")

    run_compare(results)


def run_compare(results):
    """三向对比：词典 vs Kimi vs Ollama，直接写 llm三向对比报告.md。"""
    def load(path):
        for cand in (os.path.join(BASE, path), os.path.join(BASE, "llm_experiment_day1", path)):
            if os.path.exists(cand):
                with open(cand, encoding="utf-8") as f:
                    return json.load(f)
        raise FileNotFoundError(path)

    pkg = load("llm_package_with_dict.json")["posts"]
    kimi = {x["id"]: x for x in load("llm_scores_all.json")}
    oll = results

    ids = [p["id"] for p in pkg if p["id"] in kimi and p["id"] in oll]
    d_sign = [1 if p["dict"]["direction"] > 0 else (-1 if p["dict"]["direction"] < 0 else 0) for p in pkg if p["id"] in ids]
    k_sign = [1 if kimi[i]["direction"] > 0 else (-1 if kimi[i]["direction"] < 0 else 0) for i in ids]
    o_sign = [1 if oll[i]["direction"] > 0 else (-1 if oll[i]["direction"] < 0 else 0) for i in ids]

    def agree(a, b):
        return sum(1 for x, y in zip(a, b) if x == y) / max(len(a), 1) * 100

    def dist(s):
        n = len(s)
        return f"看多 {sum(1 for x in s if x>0)} ({100*sum(1 for x in s if x>0)/n:.0f}%) / 中性 {sum(1 for x in s if x==0)} ({100*sum(1 for x in s if x==0)/n:.0f}%) / 看空 {sum(1 for x in s if x<0)} ({100*sum(1 for x in s if x<0)/n:.0f}%)"

    L = []
    L.append("# 三向对比报告：词典 vs Kimi vs Ollama")
    L.append("")
    L.append(f"- 帖子包：{len(ids)} 帖（三向均有分的交集）")
    L.append("")
    L.append("## 一、方向一致率（看多/看空/中性同号）")
    L.append("")
    L.append(f"| Kimi vs Ollama | {sum(1 for x,y in zip(k_sign,o_sign) if x==y)}/{len(ids)} | {agree(k_sign,o_sign):.1f}% |")
    L.append(f"| 词典 vs Kimi | {sum(1 for x,y in zip(d_sign,k_sign) if x==y)}/{len(ids)} | {agree(d_sign,k_sign):.1f}% |")
    L.append(f"| 词典 vs Ollama | {sum(1 for x,y in zip(d_sign,o_sign) if x==y)}/{len(ids)} | {agree(d_sign,o_sign):.1f}% |")
    L.append("")
    L.append("## 二、方向分布")
    L.append("")
    L.append(f"- 词典：{dist(d_sign)}，均值 {sum(d_sign)/len(d_sign):+.2f}")
    L.append(f"- Kimi：{dist(k_sign)}，均值 {sum(k_sign)/len(k_sign):+.2f}")
    L.append(f"- Ollama：{dist(o_sign)}，均值 {sum(o_sign)/len(o_sign):+.2f}")
    L.append("")
    L.append("## 三、标记检出（flags）")
    L.append("")
    L.append("| 标记 | Kimi检出 | Ollama检出 | 双方同检出 |")
    L.append("|---|---|---|---|")
    for fl in ["fomo", "panic", "newcomer", "sarcasm", "ai_marketing"]:
        kf = [int(bool(kimi[i].get(fl, 0))) for i in ids]
        of = [int(bool(oll[i].get(fl, 0))) for i in ids]
        both = sum(1 for x, y in zip(kf, of) if x == 1 and y == 1)
        L.append(f"| {fl} | {sum(kf)} | {sum(of)} | {both} |")
    L.append("")
    rep = os.path.join(BASE, "llm三向对比报告.md")
    with open(rep, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"对比报告已写出: {rep}")


if __name__ == "__main__":
    main()
