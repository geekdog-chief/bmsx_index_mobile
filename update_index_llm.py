# -*- coding: utf-8 -*-
"""update_index 的 LLM 覆盖层入口（叠加层新文件，原作者代码零改动）

用法：python update_index_llm.py   （替代 update_index.py，流程完全一样）

规则：羊驼接管，词典兜底——
1. sentiment_score：羊驼打过分的帖，方向分直接用羊驼的（量纲换算 -3..+3 → 词典 -3..+6）；
   羊驼判 ai_marketing → 方向分归 0（营销文不进情绪）；
   羊驼没打过的帖 → 词典分兜底（新帖首日词典先行，次日羊驼接管）。
2. contains_nonnegated：羊驼打过分的帖，fomo/panic 以羊驼判定为准；
   没打过的帖 → 词典词表兜底。

实验依据：Kimi vs 12B 方向一致率 76.5%，ai_marketing 召回 7/8。
前置：先跑 llm_overlay.py 生成 data/llm_scores_ollama.json。
"""

import json
import sys
from pathlib import Path

import update_index as ui

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "forum_cache.json"
STORE = ROOT / "data" / "llm_scores_ollama.json"

LLM_BY_TITLE = {}


def load_llm_scores():
    """帖子标题 → 羊驼评分。标题取自 forum_cache 原文，与 update_index 读的同一份。"""
    if not CACHE.exists() or not STORE.exists():
        return
    scores = {x["id"]: x for x in json.loads(STORE.read_text(encoding="utf-8"))}
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    for src in cache.values():
        if not isinstance(src, dict):
            continue
        for p in src.get("posts", []):
            if not isinstance(p, dict) or not p.get("title"):
                continue
            e = scores.get(str(p.get("id") or ""))
            if e:
                LLM_BY_TITLE[str(p["title"])] = e


_orig_sentiment = ui.sentiment_score
_orig_contains = ui.contains_nonnegated
_orig_load_cache = ui.load_forum_cache


def dedup_cache():
    """按帖子 id 去重（同一热帖连续多日采集会重复进缓存），保留最新一条。"""
    cache = _orig_load_cache()
    n_before = n_after = 0
    for key, src in cache.items():
        if not isinstance(src, dict):
            continue
        posts = src.get("posts") or []
        n_before += len(posts)
        seen, deduped = set(), []
        for p in posts:  # 保持原顺序，保首个丢重复
            pid = str(p.get("id") or "") if isinstance(p, dict) else ""
            if not pid or pid in seen:
                continue
            seen.add(pid)
            deduped.append(p)
        src["posts"] = deduped
        n_after += len(src["posts"])
    if n_before != n_after:
        print(f"LLM 覆盖层：缓存去重 {n_before} → {n_after} 条", flush=True)
    return cache


def llm_sentiment_score(text, topic=None, source=None):
    e = LLM_BY_TITLE.get(str(text or ""))
    if not e:
        return _orig_sentiment(text, topic=topic, source=source)  # 词典兜底
    if e.get("ai_marketing"):
        return 0
    d = int(e["direction"])
    return d * 2 if d > 0 else d  # -3..+3 → -3..+6，对齐词典量纲


def llm_contains(text, words):
    e = LLM_BY_TITLE.get(str(text or ""))
    if not e:
        return _orig_contains(text, words)  # 词典兜底
    if words is ui.FOMO_WORDS:
        return bool(e.get("fomo"))
    if words is ui.PANIC_WORDS:
        return bool(e.get("panic"))
    return _orig_contains(text, words)


def main():
    load_llm_scores()
    n = len(LLM_BY_TITLE)
    print(f"LLM 覆盖层：载入羊驼评分 {n} 条", flush=True)
    if n == 0:
        print("警告：无羊驼分数，本轮纯词典（先跑 llm_overlay.py）", flush=True)
    ui.sentiment_score = llm_sentiment_score
    ui.contains_nonnegated = llm_contains
    ui.load_forum_cache = dedup_cache
    return ui.main()


if __name__ == "__main__":
    sys.exit(main())
