"""LLM评分实验：导出帖子包给用户拖给 Kimi，词典分数一并附上供对比。

纯新增文件，不改任何采集/评分/聚合逻辑。
- 数据源：data/community_archive/ 的增量档案（既有产物，只读）
- 词典分数：直接调用 sentiment.py / update_index.py 的纯函数，口径与主流程一致
- 输出：llm_package_N.json（喂 Kimi，每文件<=50帖）+ llm_package_with_dict.json（对比基准）
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from trading_calendar import previous_trading_days  # noqa: E402
from sentiment import (  # noqa: E402
    CN_TZ,
    valid_text,
    grassroots_style_weight,
    sentiment_score,
    classify_persona_evidence,
    newcomer_flag,
    contains_any,
    contains_nonnegated,
    parse_time,
    signal_from_rate,
    FOMO_WORDS,
    PANIC_WORDS,
    INTENSE_WORDS,
)
from update_index import (  # noqa: E402
    DATA_DIR,
    LOOKBACK_DAYS,
    engagement_weight,
    post_age_days,
    published_trading_day,
    apply_author_daily_limit,
)

ARCHIVE_DIR = DATA_DIR / "community_archive"
MAX_TOTAL = 200          # 实验总帖数上限（统计上够用，手工操作量可控）
CHUNK = 50               # 每个喂给 Kimi 的文件最多帖子数（防输出截断）
PER_SOURCE_DAY = 1       # 每个来源×交易日抽1帖，先保证覆盖面

PANIC_DECAY = 0.12       # 与 channel_fairness_model._signals_from_metrics 一致


def load_archive_posts() -> list:
    posts = []
    if not ARCHIVE_DIR.is_dir():
        return posts
    for path in sorted(ARCHIVE_DIR.glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            if isinstance(payload.get("posts"), list):
                posts.extend(payload["posts"])
            else:  # 按日期键存的字典
                for value in payload.values():
                    if isinstance(value, list):
                        posts.extend(value)
        elif isinstance(payload, list):
            posts.extend(payload)
    return posts


def adapt(post: dict) -> dict:
    adapted = dict(post)
    parsed = parse_time(post.get("publishTime"))
    if parsed is not None:
        adapted["published"] = parsed
    return adapted


def main() -> int:
    _raw_days = previous_trading_days(date.today(), 3)
    days = [x if isinstance(x, date) else date.fromisoformat(str(x)) for x in _raw_days]
    day_set = set(days)

    eligible = []
    for raw in load_archive_posts():
        title = str(raw.get("title") or "")
        if not valid_text(title):
            continue
        post = adapt(raw)
        if published_trading_day(post) is None:
            continue
        weight = grassroots_style_weight(post)
        if float(weight) <= 0.0:
            continue
        post["grassrootsStyleWeight"] = weight
        if not 0.0 <= post_age_days(post, datetime.now(CN_TZ)) <= LOOKBACK_DAYS:
            continue
        eligible.append(post)

    author_limited, _ = apply_author_daily_limit(eligible)

    visible = [p for p in author_limited if published_trading_day(p) in day_set]
    visible.sort(key=lambda p: str(p.get("publishTime") or ""), reverse=True)

    per_source_day: dict = defaultdict(list)
    for post in visible:
        day = published_trading_day(post)
        key = (str(post.get("platform") or "?"), str(post.get("sourceKey") or "?"), day)
        if len(per_source_day[key]) < PER_SOURCE_DAY:
            per_source_day[key].append(post)

    pool = [p for group in per_source_day.values() for p in group]
    pool.sort(key=lambda p: str(p.get("archiveKey") or p.get("id") or ""))
    sampled = pool[:MAX_TOTAL]
    if not sampled:
        print("没有可用档案帖子。先正常运行一次指数更新生成档案，再导出。")
        return 1

    annotated = []
    for post in sampled:
        text = str(post.get("title") or "")
        topic = post.get("topic") or post.get("sourceCode")
        source = post.get("source")
        raw = sentiment_score(text, topic=topic, source=source)
        persona, reason, masked_text = classify_persona_evidence(text)
        masked_raw = sentiment_score(masked_text, topic=topic, source=source) if masked_text else 0
        pid = str(post.get("archiveKey") or post.get("id"))
        day = published_trading_day(post)
        annotated.append({
            "id": pid,
            "day": day.isoformat() if day else "",
            "platform": str(post.get("platformName") or post.get("platform") or "?"),
            "source": str(source or post.get("sourceKey") or "?"),
            "title": text,
            "dict": {
                "direction": raw,
                "label": "看多" if raw > 0 else "看空" if raw < 0 else "中性",
                "persona": persona,
                "personaReason": reason,
                "maskedDirection": masked_raw,
                "fomo": bool(contains_nonnegated(text, FOMO_WORDS)),
                "panic": bool(contains_nonnegated(text, PANIC_WORDS)),
                "newcomer": newcomer_flag(text),
                "intense": abs(raw) >= 2 or contains_any(text, INTENSE_WORDS) or len(__import__("re").findall(r"[!！?？]", text)) >= 2,
            },
            "weight": engagement_weight(post, use_interactions=False) * float(post.get("grassrootsStyleWeight") or 0.0),
        })

    with open(ROOT / "llm_package_with_dict.json", "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "days": [d.isoformat() for d in days], "posts": annotated},
                  fh, ensure_ascii=False, indent=1)

    for index in range(0, len(annotated), CHUNK):
        part = annotated[index:index + CHUNK]
        payload = {
            "说明": "A股社区帖子情绪评分任务，评分要求见对话中的指令",
            "posts": [{"id": p["id"], "day": p["day"], "platform": p["platform"],
                       "source": p["source"], "title": p["title"]} for p in part],
        }
        name = f"llm_package_{index // CHUNK + 1}.json"
        with open(ROOT / name, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)

    chunks = (len(annotated) + CHUNK - 1) // CHUNK
    print(f"导出完成：{len(annotated)} 帖，分 {chunks} 个文件（llm_package_1.json ~ llm_package_{chunks}.json）")
    print("词典分数基准已存 llm_package_with_dict.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
