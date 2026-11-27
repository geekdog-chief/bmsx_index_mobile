"""宝妈·小白·散户社区情绪指数 v6 重写版。

单入口调度：采集(collectors) → 评分(source_analysis) → 四渠道公平聚合
(channel_fairness_model) → 板块热度(sector_heat) → 历史(history_engine)。
Python 3 标准库即可运行，不联网时自动续用45分钟内的采集缓存。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import traceback
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Mapping, Sequence, Tuple

from channel_fairness_model import (
    CHANNELS as FAIR_CHANNELS,
    MODEL_VERSION as FAIRNESS_MODEL_VERSION,
    SIGNAL_KEYS as _CHANNEL_SIGNAL_KEYS,
    aggregate_channel_fairness,
    score_personas,
)
from collectors import (
    PLATFORM_NAMES,
    collection_metadata,
    fetch_eastmoney,
    fetch_sina,
    fetch_taoguba,
    fetch_ths,
    save_sina_bids,
    save_ths_config,
)
from community_archive import archive_results
from quality_gate import build_quality_report, format_console_lines
from history_engine import (
    ESTIMATED,
    MEASURED,
    build_history_meta,
    build_history_point,
    normalized_weighted_average,
    record_type_of,
    upsert_history,
)
from sector_catalog import (
    PARENT_SECTORS,
    SECTOR_CATALOG_ID,
    SECTOR_CATALOG_VERSION,
    SECTORS,
)
from sector_heat import calibrate_sector_heats, stabilize_sector_heats
from sentiment import (
    AUTHOR_DAILY_LIMIT,
    CN_TZ,
    ESTIMATED_SOURCE_DAY_LIMIT,
    PERSONA_EVIDENCE_KEYS,
    FOMO_WORDS,
    INTENSE_WORDS,
    PANIC_WORDS,
    contains_any,
    clean_text,
    clamp,
    classify_persona_evidence,
    contains_nonnegated,
    dual_directional_condition,
    grassroots_style_weight,
    has_negating_prefix,
    newcomer_flag,
    normalize_text,
    parse_time,
    round1,
    safe_int,
    sentiment_score,
    stable_estimated_post_key,
    trade_switch_adjustment,
    valid_text,
)
from trading_calendar import (
    calendar_mode,
    is_trading_day,
    latest_trading_day,
    previous_trading_days,
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"
HISTORY_META_PATH = DATA_DIR / "history_meta.json"
LEGACY_HISTORY_PATH = DATA_DIR / "history_market_v2.json"
V3_HISTORY_PATH = DATA_DIR / "history_community_v3.json"
DATA_JS_PATH = ROOT / "data.js"
LOG_PATH = DATA_DIR / "update.log"
FORUM_CACHE_PATH = DATA_DIR / "forum_cache.json"
COMMUNITY_ARCHIVE_DIR = DATA_DIR / "community_archive"
SQLITE_ARCHIVE_PATH = DATA_DIR / "archive.sqlite3"

CN_TZ = CN_TZ
NOW = datetime.now(CN_TZ)
VERSION = 6
LOOKBACK_DAYS = 30
TRADING_DAY_WEIGHTS: Tuple[float, float, float] = (0.50, 0.30, 0.20)
TRADING_WINDOW_SIZE = len(TRADING_DAY_WEIGHTS)
ACTIVITY_VELOCITY_WEIGHT = 0.85
ACTIVITY_BREADTH_WEIGHT = 1.0 - ACTIVITY_VELOCITY_WEIGHT

MARKET_EASTMONEY_SOURCES: List[Tuple[str, str]] = [
    ("zssh000001", "上证指数吧"),
    ("zssz399001", "深证成指吧"),
    ("zssz399006", "创业板指吧"),
]

ABSOLUTE_SIGNAL_WEIGHT = {
    "optimism": 0.75,
    "fomo": 0.75,
    "newcomer": 0.70,
    "intensity": 0.75,
    "attention": 0.50,
}

SIGNAL_KEYS = ("optimism", "fomo", "newcomer", "intensity", "attention")
FAIR_SIGNAL_KEYS = ("optimism", "fomo", "panic", "newcomer", "intensity", "attention")
FAIR_SIGNAL_BASELINES = {
    "optimism": 50.0,
    "fomo": 50.0,
    "panic": 0.0,
    "newcomer": 50.0,
    "intensity": 50.0,
    "attention": 50.0,
}

# 写入门槛：新鲜源占比 ≥30% 且覆盖 ≥3 平台，否则保留上一次结果
MIN_FRESH_SOURCE_RATIO = 0.30
MIN_FRESH_PLATFORMS = 3
CACHE_RESUME_MINUTES = 45


def write_log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload: object) -> None:
    """uuid临时名+os.replace原子提交。固定.tmp名是原实现并发写崩的根因：
    线程A replace移走tmp后线程B replace直接FileNotFoundError。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def build_source_specs() -> Tuple[Dict[str, Dict[str, object]], List[str], Dict[str, List[str]]]:
    """生成每日采集计划。

    东方财富、新浪、淘股吧分别读取三家代表公司的个股社区；同花顺读取
    明确配置的板块社区及其公开关联场景。入口数只增加证据覆盖，正式聚合
    仍由四个固定25%渠道席位控制。
    """

    def stock_pairs(value: object) -> List[Tuple[str, str]]:
        rows: List[Tuple[str, str]] = []
        if isinstance(value, Mapping):
            value = [value]
        elif (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and not isinstance(value[0], (list, tuple, Mapping))
        ):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return rows
        for item in value:
            if isinstance(item, Mapping):
                code = str(item.get("code") or "").strip()
                name = str(item.get("name") or code).strip()
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                code = str(item[0]).strip()
                name = str(item[1]).strip()
            else:
                continue
            if code:
                rows.append((code, name or code))
        return rows

    specs: Dict[str, Dict[str, object]] = {}
    market_keys: List[str] = []
    sector_keys: Dict[str, List[str]] = {}

    for code, name in MARKET_EASTMONEY_SOURCES:
        key = f"eastmoney:{code}"
        specs[key] = {"key": key, "platform": "eastmoney", "code": code, "name": name}
        market_keys.append(key)
    ths_market_key = "ths:market:1A0001"
    specs[ths_market_key] = {
        "key": ths_market_key,
        "platform": "ths",
        "code": "1A0001",
        "name": "上证指数讨论",
        "marketId": 16,
        "preferScene": False,
    }
    market_keys.append(ths_market_key)

    # 大盘层补齐新浪/淘股吧固定席位（2026-11-24）：此前两渠道在大盘层无采集源，
    # 证据恒为空并被中性化；现接入上证指数吧（新浪）与上证指数讨论（淘股吧）。
    sina_market_key = "sina:sh000001"
    specs[sina_market_key] = {
        "key": sina_market_key,
        "platform": "sina",
        "code": "000001",
        "symbol": "sh000001",
        "name": "上证指数",
    }
    market_keys.append(sina_market_key)
    taoguba_market_key = "taoguba:sh000001"
    specs[taoguba_market_key] = {
        "key": taoguba_market_key,
        "platform": "taoguba",
        "code": "000001",
        "symbol": "sh000001",
        "name": "上证指数",
    }
    market_keys.append(taoguba_market_key)

    for sector_key, definition in SECTORS.items():
        keys: List[str] = []
        source_stocks = stock_pairs(definition.get("sourceStocks") or definition.get("stocks"))
        if not source_stocks:
            source_stocks = stock_pairs(definition.get("representativeCompanies"))[:2]
        if not source_stocks:
            raise ValueError(f"板块 {sector_key} 没有可用的论坛采集公司")

        industry_rows = stock_pairs(definition.get("industry"))
        eastmoney_sources = [*industry_rows[:1], *source_stocks]
        for code, name in eastmoney_sources:
            key = f"eastmoney:{code}"
            specs[key] = {
                "key": key,
                "platform": "eastmoney",
                "code": code,
                "name": f"{name}吧" if not str(name).endswith("吧") else name,
            }
            if key not in keys:
                keys.append(key)

        sina_stocks = stock_pairs(definition.get("sinaStocks")) or source_stocks[:1]
        for sina_code, sina_name in sina_stocks:
            sina_key = f"sina:{sina_code}"
            specs[sina_key] = {
                "key": sina_key,
                "platform": "sina",
                "code": sina_code,
                "name": sina_name,
            }
            if sina_key not in keys:
                keys.append(sina_key)

        ths_rows = definition.get("thsCommunities")
        if not isinstance(ths_rows, list) or not ths_rows:
            raise ValueError(f"板块 {sector_key} 没有配置同花顺板块社区")
        for community in ths_rows:
            if not isinstance(community, Mapping):
                continue
            ths_code = str(community.get("code") or "").strip()
            if not ths_code:
                continue
            ths_key = f"ths:sector:{sector_key}:{ths_code}"
            specs[ths_key] = {
                "key": ths_key,
                "platform": "ths",
                "code": ths_code,
                "name": str(community.get("name") or definition["name"]),
                "marketId": safe_int(community.get("marketId"), 48),
                "preferScene": True,
                "sectorKey": sector_key,
            }
            keys.append(ths_key)

        for taoguba_code, taoguba_name in source_stocks:
            taoguba_key = f"taoguba:{taoguba_code}"
            specs[taoguba_key] = {
                "key": taoguba_key,
                "platform": "taoguba",
                "code": taoguba_code,
                "name": taoguba_name,
                "sectorKey": sector_key,
            }
            if taoguba_key not in keys:
                keys.append(taoguba_key)
        sector_keys[sector_key] = keys
    return specs, market_keys, sector_keys



def load_forum_cache() -> Dict[str, Dict[str, object]]:
    raw = load_json(FORUM_CACHE_PATH, {})
    if not isinstance(raw, dict):
        return {}
    restored: Dict[str, Dict[str, object]] = {}
    for old_key, source in raw.items():
        if not isinstance(source, dict):
            continue
        platform = str(source.get("platform") or "eastmoney")
        key = str(source.get("key") or old_key)
        if ":" not in key:
            key = f"{platform}:{key}"
        posts: List[Dict[str, object]] = []
        for item in source.get("posts", []):
            if not isinstance(item, dict):
                continue
            published = parse_time(item.get("publishTime"))
            if published is None:
                continue
            posts.append({
                **item,
                "published": published,
                "platform": str(item.get("platform") or platform),
                "platformName": str(item.get("platformName") or PLATFORM_NAMES.get(platform, platform)),
                "sourceKey": str(item.get("sourceKey") or key),
                "kind": str(item.get("kind") or "post"),
                # Legacy THS comments inherited the parent feed item's time.
                # Preserve that fact when an older cache predates the explicit
                # marker; other legacy posts used their own publication time.
                "timeInherited": bool(
                    item.get("timeInherited")
                    if "timeInherited" in item
                    else platform == "ths" and str(item.get("kind") or "post") == "comment"
                ),
            })
        metadata = collection_metadata(
            raw_times=[],
            posts=posts,
            raw_rows=safe_int(source.get("rawRows"), len(posts)),
            page_count=safe_int(source.get("pageCount"), 1 if posts else 0),
            cap_hit=source.get("capHit") if "capHit" in source else None,
            chronological=(
                bool(source.get("chronological"))
                if "chronological" in source
                else platform in {"eastmoney", "sina", "taoguba"}
            ),
            fresh_fetched_now=False,
            fetched_at=source.get("fetchedAt"),
            now=NOW,
        )
        # Raw-row coverage can include filtered posts. Retain saved evidence
        # when present, and only reconstruct fields for a legacy cache.
        for field in (
            "rawRows", "pageCount", "oldestSeen", "newestSeen", "capHit",
            "chronological", "coverageDays", "thirdTradingDayReached",
        ):
            if field in source:
                metadata[field] = source[field]
        metadata["freshFetchedNow"] = False
        metadata["rawRowsEstimated"] = bool(source.get("rawRowsEstimated", "rawRows" not in source))
        restored[key] = {
            **source,
            **metadata,
            "coverageStatus": str(source.get("coverageStatus") or "legacy_cache_unknown"),
            "key": key,
            "platform": platform,
            "posts": posts,
        }
    return restored



def save_forum_cache(results: Mapping[str, Dict[str, object]]) -> None:
    serializable: Dict[str, Dict[str, object]] = {}
    for key, source in results.items():
        posts = [{field: value for field, value in item.items() if field != "published"} for item in source.get("posts", [])]
        serializable[key] = {**source, "posts": posts}
    save_json(FORUM_CACHE_PATH, serializable)



def cache_is_usable(source: Mapping[str, object]) -> bool:
    fetched = parse_time(source.get("fetchedAt"))
    if fetched is None or NOW - fetched > timedelta(hours=72):
        return False
    return bool(source.get("posts"))



def cache_is_recent(source: Mapping[str, object], minutes: int = 45) -> bool:
    fetched = parse_time(source.get("fetchedAt"))
    return bool(source.get("posts")) and fetched is not None and NOW - fetched <= timedelta(minutes=minutes)



def accumulate_taoguba_snapshots(
    fresh: Dict[str, object],
    previous: Mapping[str, object] | None,
) -> Dict[str, object]:
    """Accumulate anonymous 20-row snapshots without claiming uncensored volume."""

    if str(fresh.get("platform") or "") != "taoguba" or not isinstance(previous, Mapping):
        return fresh
    combined: Dict[str, Dict[str, object]] = {}
    cutoff = NOW - timedelta(days=10)
    for source in (previous, fresh):
        for raw in source.get("posts", []):
            if not isinstance(raw, Mapping):
                continue
            post = dict(raw)
            published = post.get("published") if isinstance(post.get("published"), datetime) else parse_time(post.get("publishTime"))
            if published is None or published < cutoff:
                continue
            post["published"] = published
            identity = str(post.get("id") or hashlib.sha256(
                normalize_text(str(post.get("title") or "")).encode("utf-8")
            ).hexdigest())
            combined[identity] = post
    merged = sorted(combined.values(), key=lambda post: post["published"], reverse=True)
    return {
        **fresh,
        "posts": merged,
        "snapshotAccumulated": True,
        "snapshotAccumulatedPostCount": len(merged),
        "volumeIsLowerBound": True,
    }



def merge_posts(results: Mapping[str, Dict[str, object]], source_keys: Sequence[str]) -> List[Dict[str, object]]:
    merged: Dict[str, Dict[str, object]] = {}
    normalized_seen: set[str] = set()
    for source_key in source_keys:
        for original in results.get(source_key, {}).get("posts", []):
            post = dict(original)
            normalized = normalize_text(str(post.get("title") or ""))
            platform = str(post.get("platform") or "unknown")
            # 精确归一化正文跨平台、跨来源去重；平台只保留在 id 身份键中。
            content_key = normalized
            raw_id = str(post.get("id") or "")
            identity_key = raw_id if raw_id.startswith(f"{platform}:") else f"{platform}:{raw_id}"
            if identity_key in merged or (normalized and content_key in normalized_seen):
                continue
            merged[identity_key] = post
            if normalized:
                normalized_seen.add(content_key)
    return sorted(merged.values(), key=lambda item: item["published"], reverse=True)



def apply_author_daily_limit(
    visible: Sequence[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], int]:
    """按作者×自然日限额，防止单一高产账号制造虚假热度。"""

    ordered = sorted(
        visible,
        key=lambda item: item.get("published") or datetime.min.replace(tzinfo=CN_TZ),
        reverse=True,
    )
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    author_limited: List[Dict[str, object]] = []
    author_overflow = 0
    for post in ordered:
        published = post.get("published")
        day = published.astimezone(CN_TZ).strftime("%Y-%m-%d") if isinstance(published, datetime) else "unknown"
        author = str(post.get("authorKey") or post.get("authorHash") or post.get("id") or normalize_text(str(post.get("title") or "")))
        bucket = (author, day)
        if author_day_counts[bucket] >= AUTHOR_DAILY_LIMIT:
            author_overflow += 1
            continue
        author_day_counts[bucket] += 1
        author_limited.append(post)

    return author_limited, author_overflow



def select_analysis_sample(
    visible: Sequence[Dict[str, object]],
    record_type: str,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """执行作者限额；仅 estimated 的情绪比例再做来源×日期稳定限额。

    每来源每日 8 条只用于避免历史文本量不均匀地改变情绪词比例。讨论量、
    独立作者和发帖速度始终使用作者限额后的全部可见文本，不能与情绪抽样
    共用上限，否则真正热门的板块会被硬截成和普通板块一样热。
    """

    author_limited, author_overflow = apply_author_daily_limit(visible)

    if record_type == MEASURED:
        return author_limited, {
            "mode": MEASURED,
            "postsBefore": len(visible),
            "postsAfterAuthorLimit": len(author_limited),
            "postsSelected": len(author_limited),
            "authorOverflowRemoved": author_overflow,
            "sourceDayBucketsCapped": 0,
            "capPerSourcePublishedDay": None,
        }

    by_source_day: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    for post in author_limited:
        published = post.get("published")
        day = published.astimezone(CN_TZ).strftime("%Y-%m-%d") if isinstance(published, datetime) else "unknown"
        by_source_day[(str(post.get("sourceKey") or "unknown"), day)].append(post)
    selected: List[Dict[str, object]] = []
    capped_buckets = 0
    for bucket_posts in by_source_day.values():
        if len(bucket_posts) > ESTIMATED_SOURCE_DAY_LIMIT:
            capped_buckets += 1
        selected.extend(sorted(bucket_posts, key=stable_estimated_post_key)[:ESTIMATED_SOURCE_DAY_LIMIT])
    selected.sort(
        key=lambda item: item.get("published") or datetime.min.replace(tzinfo=CN_TZ),
        reverse=True,
    )
    return selected, {
        "mode": ESTIMATED,
        "postsBefore": len(visible),
        "postsAfterAuthorLimit": len(author_limited),
        "postsSelected": len(selected),
        "authorOverflowRemoved": author_overflow,
        "sourceDayBucketsCapped": capped_buckets,
        "capPerSourcePublishedDay": ESTIMATED_SOURCE_DAY_LIMIT,
        "selection": "sha256(sourceKey,id,publishTime,authorKey); title only when id missing",
    }



def source_activity_metrics(
    posts: Sequence[Mapping[str, object]],
    as_of: datetime,
    record_type: str,
) -> Dict[str, object]:
    """计算三交易日加权的社区发帖量与独立参与广度。

    最新、上一、上两个交易日的原始权重固定为 50%/30%/20%。若某个
    交易日没有真实可见样本，只在其余可用日之间归一化，不把缺日当成 0，
    更不会补入中性 50。周末和休市日文本不进入数量或速度口径。
    """

    window_days = scoring_trading_days(as_of)
    window_set = set(window_days)
    ordered = sorted(
        (
            post for post in posts
            if isinstance(post.get("published"), datetime)
            and post_age_days(post, as_of) >= 0.0
            and published_trading_day(post) in window_set
        ),
        key=lambda item: item["published"],
        reverse=True,
    )
    by_day: Dict[date, List[Mapping[str, object]]] = defaultdict(list)
    for post in ordered:
        day = published_trading_day(post)
        if day is not None:
            by_day[day].append(post)

    effective_weights = normalized_trading_day_weights(as_of, by_day)
    daily_rows: List[Dict[str, object]] = []
    weighted_daily_posts = 0.0
    weighted_author_ratio = 0.0
    all_authors: set[str] = set()
    for index, day in enumerate(window_days):
        day_posts = by_day.get(day, [])
        equivalent_posts = sum(
            (0.72 if post.get("kind") == "comment" else 1.0)
            * float(post.get("grassrootsStyleWeight") or grassroots_style_weight(post))
            for post in day_posts
        )
        authors = {
            str(post.get("authorKey") or post.get("authorHash") or post.get("id") or "unknown")
            for post in day_posts
        }
        all_authors.update(authors)
        author_ratio = len(authors) / len(day_posts) if day_posts else 0.0
        effective_weight = effective_weights.get(day, 0.0)
        weighted_daily_posts += effective_weight * equivalent_posts
        weighted_author_ratio += effective_weight * author_ratio
        daily_rows.append({
            "date": day.isoformat(),
            "rawWeight": TRADING_DAY_WEIGHTS[index],
            "effectiveWeight": round(effective_weight, 6),
            "postCount": len(day_posts),
            "equivalentPosts": round(equivalent_posts, 3),
            "uniqueAuthors": len(authors),
            "uniqueAuthorRatio": round(author_ratio, 4),
            "available": bool(day_posts),
        })

    anchor_posts = by_day.get(window_days[0], []) if window_days else []
    anchor_equivalent = sum(
        (0.72 if post.get("kind") == "comment" else 1.0)
        * float(post.get("grassrootsStyleWeight") or grassroots_style_weight(post))
        for post in anchor_posts
    )
    anchor_authors = {
        str(post.get("authorKey") or post.get("authorHash") or post.get("id") or "unknown")
        for post in anchor_posts
    }
    total_window_equivalent = sum(float(row["equivalentPosts"]) for row in daily_rows)
    posting_velocity = max(0.0, weighted_daily_posts)

    return {
        # 新口径字段。
        "method": "three_trading_day_weighted_daily_posts",
        "marketDate": window_days[0].isoformat() if window_days else None,
        "tradingDays": [day.isoformat() for day in window_days],
        "dayWeights": list(TRADING_DAY_WEIGHTS),
        "availableTradingDayCount": len(effective_weights),
        "missingTradingDaysExcluded": len(window_days) - len(effective_weights),
        "effectiveDayWeights": {
            day.isoformat(): round(weight, 6) for day, weight in effective_weights.items()
        },
        "dailyPostCounts": daily_rows,
        "weightedDailyPosts3d": round(weighted_daily_posts, 3),
        "totalEquivalentPosts3d": round(total_window_equivalent, 3),
        "uniqueAuthors3d": len(all_authors),
        "uniqueAuthorRatio3d": round(weighted_author_ratio, 4),
        "calendarMode": calendar_mode(window_days[0]) if window_days else calendar_mode(as_of),
        # 兼容既有页面和板块热度模型的旧字段；数值已切换到三交易日口径。
        "recent24Equivalent": round(anchor_equivalent, 3),
        "effective7Equivalent": round(total_window_equivalent, 3),
        "dailyEquivalent": round(weighted_daily_posts, 3),
        "uniqueAuthors24": len(anchor_authors),
        "uniqueAuthorRatio24": round(weighted_author_ratio, 4),
        "postingVelocity": round(posting_velocity, 3),
        "logPostingVelocity": round(math.log1p(posting_velocity), 6),
        "spanRate": None,
        "spanHours": None,
        "spanSamplePosts": 0,
        "lowerBound": False,
        "usesInteractions": False,
        "usesMarketPrice": False,
    }



def estimated_attention_score(
    posts: Sequence[Mapping[str, object]],
    as_of: datetime,
) -> Tuple[float, Dict[str, object]]:
    """Compatibility helper for three-trading-day retrospective attention."""

    metrics = source_activity_metrics(posts, as_of, ESTIMATED)
    daily_equivalent = float(metrics["weightedDailyPosts3d"])
    activity = round1(
        50.0 + 30.0 * math.tanh(math.log((daily_equivalent + 2.0) / 10.0) / 1.3)
    )
    return activity, {
        "method": "three_trading_day_weighted_post_volume",
        "status": "applied" if metrics["availableTradingDayCount"] else "unavailable",
        "reason": None if metrics["availableTradingDayCount"] else "no_available_trading_day_samples",
        "dailyEquivalent": round(daily_equivalent, 3),
        "weightedDailyPosts3d": round(daily_equivalent, 3),
        "tradingDays": metrics["tradingDays"],
        "effectiveDayWeights": metrics["effectiveDayWeights"],
        "missingDaysAssumedZero": False,
        "usesFutureDates": False,
    }



def engagement_weight(post: Mapping[str, object], *, use_interactions: bool = True) -> float:
    comments = max(0.0, float(post.get("comments") or 0)) if use_interactions else 0.0
    likes = max(0.0, float(post.get("likes") or 0)) if use_interactions else 0.0
    interaction = min(math.log1p(comments) * 0.13 + math.log1p(likes) * 0.08, 0.65)
    kind_weight = 0.72 if post.get("kind") == "comment" else 1.0
    return kind_weight * (1.0 + interaction)



def post_age_days(post: Mapping[str, object], as_of: datetime = NOW) -> float:
    published = post.get("published")
    if not isinstance(published, datetime):
        return float(LOOKBACK_DAYS)
    return (as_of - published).total_seconds() / 86400.0



def scoring_market_day(as_of: datetime = NOW) -> date:
    """Return the A-share session that owns a scoring snapshot.

    A weekend or exchange holiday update belongs to the latest completed
    trading date.  On a trading day, an update before 15:10 is also assigned
    to the previous completed session: otherwise a partial morning/afternoon
    post count would receive the same 50% weight as a full trading day.
    ``updatedAt`` still records the real collection time; this helper only
    controls the scoring window and the history-point date.
    """

    local = as_of.replace(tzinfo=CN_TZ) if as_of.tzinfo is None else as_of.astimezone(CN_TZ)
    if is_trading_day(local.date()) and (local.hour, local.minute) < (15, 10):
        return latest_trading_day(local.date() - timedelta(days=1))
    return latest_trading_day(local)



def scoring_trading_days(as_of: datetime = NOW) -> List[date]:
    """Return the three scoring sessions, newest first."""

    return previous_trading_days(scoring_market_day(as_of), TRADING_WINDOW_SIZE)



def published_trading_day(post: Mapping[str, object]) -> date | None:
    published = post.get("published")
    if not isinstance(published, datetime):
        return None
    local = published.replace(tzinfo=CN_TZ) if published.tzinfo is None else published.astimezone(CN_TZ)
    return local.date()



def normalized_trading_day_weights(
    as_of: datetime,
    available_days: Iterable[date],
) -> Dict[date, float]:
    """Normalise 50/30/20 only over trading days that contain real samples."""

    available = set(available_days)
    raw = {
        day: TRADING_DAY_WEIGHTS[index]
        for index, day in enumerate(scoring_trading_days(as_of))
        if day in available
    }
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {day: weight / total for day, weight in raw.items()}



def trading_day_gap(previous: date, current: date) -> int:
    """Count A-share sessions after ``previous`` through ``current``."""

    if current <= previous:
        return 1
    cursor = previous + timedelta(days=1)
    count = 0
    while cursor <= current:
        if is_trading_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return max(1, count)



def signal_from_rate(rate: float, kind: str) -> float:
    if kind == "fomo":
        return round1(50.0 + 30.0 * math.tanh((rate - 0.06) / 0.07))
    if kind == "newcomer":
        return round1(50.0 + 28.0 * math.tanh((rate - 0.02) / 0.035))
    if kind == "intensity":
        return round1(50.0 + 25.0 * math.tanh((rate - 0.18) / 0.16))
    raise ValueError(kind)



def source_analysis(
    posts: Sequence[Dict[str, object]],
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> Dict[str, object] | None:
    window_days = scoring_trading_days(as_of)
    window_set = set(window_days)
    eligible: List[Dict[str, object]] = []
    for original in posts:
        if (
            not valid_text(str(original.get("title") or ""))
            or not 0.0 <= post_age_days(original, as_of) <= LOOKBACK_DAYS
            or published_trading_day(original) is None
        ):
            continue
        post = dict(original)
        post["grassrootsStyleWeight"] = grassroots_style_weight(post)
        if float(post["grassrootsStyleWeight"]) <= 0.0:
            continue
        eligible.append(post)
    visible = [
        post for post in eligible
        if published_trading_day(post) in window_set
    ]
    non_trading_excluded = sum(
        1 for post in eligible
        if not is_trading_day(published_trading_day(post))
    )
    # 回填档案先限制单一作者，再在 estimated 中按来源×发布日期做稳定限额；
    # measured 保持原有选样路径不变。
    activity_sample, _ = apply_author_daily_limit(visible)
    sample, sampling_policy = select_analysis_sample(visible, record_type)
    if not sample:
        return None
    activity_metrics = source_activity_metrics(activity_sample, as_of, record_type)
    annotated: List[Dict[str, object]] = []
    for post in sample:
        text = str(post.get("title") or "")
        topic = post.get("topic") or post.get("sourceCode")
        source = post.get("source")
        raw = sentiment_score(text, topic=topic, source=source)
        persona, evidence_reason, masked_text = classify_persona_evidence(text)
        masked_raw = sentiment_score(masked_text, topic=topic, source=source) if masked_text else 0
        annotated.append({
            **post,
            "rawScore": raw,
            "personaEvidence": persona,
            "personaEvidenceReason": evidence_reason,
            "maskedEvidenceText": masked_text,
            "maskedRawScore": masked_raw,
            "label": "看多" if raw > 0 else "看空" if raw < 0 else "中性",
            "fomoFlag": contains_nonnegated(text, FOMO_WORDS),
            "panicFlag": contains_nonnegated(text, PANIC_WORDS),
            "newcomerFlag": newcomer_flag(text),
            "intenseFlag": abs(raw) >= 2 or contains_any(text, INTENSE_WORDS) or len(re.findall(r"[!！?？]", text)) >= 2,
            "tradingDay": published_trading_day(post).isoformat() if published_trading_day(post) else None,
        })
    annotated_by_day: Dict[date, List[Dict[str, object]]] = defaultdict(list)
    for post in annotated:
        day = published_trading_day(post)
        if day is not None:
            annotated_by_day[day].append(post)
    effective_day_weights = normalized_trading_day_weights(as_of, annotated_by_day)
    daily_values: Dict[date, Dict[str, float]] = {}
    for day, day_posts in annotated_by_day.items():
        totals = defaultdict(float)
        denominator = 0.0
        for post in day_posts:
            # 正式情绪只看文本本身。同一段话不能因为某个平台累计了更多点赞/
            # 评论就获得更大方向权重，否则 measured 与历史 estimated 会形成
            # 不可比较的两套口径，也会把平台推荐算法混进社区情绪。
            weight = (
                engagement_weight(post, use_interactions=False)
                * float(post.get("grassrootsStyleWeight") or 0.0)
            )
            denominator += weight
            raw = int(post["rawScore"])
            totals["net"] += weight * math.tanh(raw / 2.2)
            for key in ("fomo", "panic", "newcomer", "intense"):
                totals[key] += weight if post[f"{key}Flag"] else 0.0
        if denominator > 0:
            daily_values[day] = {
                key: value / denominator for key, value in totals.items()
            }
    if not daily_values or not effective_day_weights:
        return None
    raw_metrics = {
        key: sum(
            effective_day_weights.get(day, 0.0) * values.get(key, 0.0)
            for day, values in daily_values.items()
        )
        for key in ("net", "fomo", "panic", "newcomer", "intense")
    }

    sampling_policy = {
        **sampling_policy,
        "scoringWindow": "latest_three_a_share_trading_days",
        "marketDate": window_days[0].isoformat(),
        "tradingDays": [day.isoformat() for day in window_days],
        "rawDayWeights": {
            day.isoformat(): TRADING_DAY_WEIGHTS[index]
            for index, day in enumerate(window_days)
        },
        "effectiveDayWeights": {
            day.isoformat(): round(weight, 6)
            for day, weight in effective_day_weights.items()
        },
        "availableTradingDayCount": len(effective_day_weights),
        "missingTradingDaysExcluded": len(window_days) - len(effective_day_weights),
        "nonTradingPostsExcluded": non_trading_excluded,
        "missingDaysAssumedNeutral": False,
    }

    median_comments = statistics.median([safe_int(post.get("comments")) for post in annotated]) if record_type == MEASURED else 0.0
    daily_equivalent = float(activity_metrics["weightedDailyPosts3d"])
    activity = 50.0 + 30.0 * math.tanh(math.log((daily_equivalent + 2.0) / 10.0) / 1.3)
    interaction = (
        50.0 + 20.0 * math.tanh(math.log1p(median_comments) / 3.0)
        if record_type == MEASURED else None
    )
    # 互动只保留为解释字段，不进入正式 attention。这样当前实测与历史回溯
    # 使用完全相同的文本/发帖量定义，并避免不同平台的推荐机制改变指数。
    attention = round1(activity)
    attention_baseline = {
        "method": "three_trading_day_weighted_post_volume",
        "status": "applied",
        "dailyEquivalent": round(daily_equivalent, 3),
        "weightedDailyPosts3d": round(daily_equivalent, 3),
        "activityScore": round1(activity),
        "interactionScore": round1(interaction) if interaction is not None else None,
        "volumeWeight": 1.0,
        "interactionWeight": 0.0,
        "interactionUsedForScoring": False,
        "tradingDays": [day.isoformat() for day in window_days],
        "effectiveDayWeights": activity_metrics["effectiveDayWeights"],
        "missingDaysAssumedZero": False,
    }
    signals = {
        "optimism": round1(50.0 + 45.0 * raw_metrics["net"]),
        "fomo": signal_from_rate(raw_metrics["fomo"], "fomo"),
        "panic": round1(100.0 * (1.0 - math.exp(-raw_metrics["panic"] / 0.12))),
        "newcomer": signal_from_rate(raw_metrics["newcomer"], "newcomer"),
        "intensity": signal_from_rate(raw_metrics["intense"], "intensity"),
        "attention": attention,
    }
    signals["heat"] = round1(
        signals["fomo"] * 0.30 + signals["newcomer"] * 0.15
        + signals["intensity"] * 0.25 + signals["attention"] * 0.30
    )
    return {
        "signals": signals,
        "raw": raw_metrics,
        "dailyRaw": [
            {
                "date": day.isoformat(),
                "rawWeight": TRADING_DAY_WEIGHTS[index],
                "effectiveWeight": round(effective_day_weights.get(day, 0.0), 6),
                "sampleCount": max(0, int(round(sum(
                    float(post.get("grassrootsStyleWeight") or 0.0)
                    for post in annotated_by_day.get(day, [])
                )))),
                "rawSampleCount": len(annotated_by_day.get(day, [])),
                "available": day in daily_values,
                "metrics": {
                    key: round(float(daily_values.get(day, {}).get(key, 0.0)), 6)
                    for key in ("net", "fomo", "panic", "newcomer", "intense")
                } if day in daily_values else None,
            }
            for index, day in enumerate(window_days)
        ],
        "posts": annotated,
        "activity": activity_metrics,
        "sampleCount": max(0, int(round(sum(
            float(post.get("grassrootsStyleWeight") or 0.0) for post in annotated
        )))),
        "rawSampleCount": len(annotated),
        "grassrootsEffectiveSample": round(sum(
            float(post.get("grassrootsStyleWeight") or 0.0) for post in annotated
        ), 3),
        "grassrootsFullWeightShare": round(
            sum(float(post.get("grassrootsStyleWeight") or 0.0) >= 0.95 for post in annotated)
            / max(1, len(annotated)),
            4,
        ),
        "activityPostCount": len(activity_sample),
        # 旧字段继续供页面使用：recent24 表示归属交易日，recent7 表示三交易日窗口。
        "recent24": sum(1 for post in activity_sample if published_trading_day(post) == window_days[0]),
        "recent7": len(activity_sample),
        "tradingWindowPosts": len(activity_sample),
        "currentTradingDayPosts": sum(
            1 for post in activity_sample if published_trading_day(post) == window_days[0]
        ),
        "tradingWindow": {
            "marketDate": window_days[0].isoformat(),
            "tradingDays": [day.isoformat() for day in window_days],
            "rawDayWeights": list(TRADING_DAY_WEIGHTS),
            "effectiveDayWeights": {
                day.isoformat(): round(weight, 6)
                for day, weight in effective_day_weights.items()
            },
            "availableTradingDayCount": len(effective_day_weights),
            "missingTradingDaysExcluded": len(window_days) - len(effective_day_weights),
            "nonTradingPostsExcluded": non_trading_excluded,
        },
        "medianComments": float(median_comments),
        "bullish": sum(1 for post in annotated if post["label"] == "看多"),
        "bearish": sum(1 for post in annotated if post["label"] == "看空"),
        "neutral": sum(1 for post in annotated if post["label"] == "中性"),
        "samplingPolicy": sampling_policy,
        "attentionBaseline": attention_baseline,
    }



def persona_evidence_diagnostics(
    analyses: Sequence[Mapping[str, object]],
    baseline_direction: float,
    as_of: datetime,
) -> Dict[str, object]:
    """汇总互斥行为证据，产出不参与正式指数的遮蔽词候选方向分。"""

    pool: List[Mapping[str, object]] = []
    for analysis in analyses:
        pool.extend(
            post for post in analysis.get("posts", [])
            if isinstance(post, Mapping) and post.get("personaEvidence") in PERSONA_EVIDENCE_KEYS
        )
    counts = Counter(str(post.get("personaEvidence")) for post in pool)
    reason_counts: Dict[str, Counter[str]] = defaultdict(Counter)
    for post in pool:
        reason_counts[str(post.get("personaEvidence"))][str(post.get("personaEvidenceReason"))] += 1

    candidates: Dict[str, Dict[str, object]] = {}
    for persona in PERSONA_EVIDENCE_KEYS:
        cohort = [post for post in pool if post.get("personaEvidence") == persona]
        sources = {str(post.get("sourceKey") or "unknown") for post in cohort}
        platforms = {str(post.get("platform") or "unknown") for post in cohort}
        if not cohort:
            candidates[persona] = {
                "available": False,
                "evidencePosts": 0,
                "sourceCount": 0,
                "platformCount": 0,
                "maskedDirectionRaw": None,
                "evidenceWeight": 0.0,
                "candidateShrunkDirection": None,
            }
            continue
        weighted_net = 0.0
        denominator = 0.0
        for post in cohort:
            weight = (0.72 if post.get("kind") == "comment" else 1.0) * (
                0.5 ** (post_age_days(post, as_of) / 7.0)
            )
            denominator += weight
            weighted_net += weight * math.tanh(safe_int(post.get("maskedRawScore")) / 2.2)
        raw_direction = round1(50.0 + 45.0 * weighted_net / max(denominator, 1e-9))
        sample_ratio = min(1.0, len(cohort) / 20.0)
        source_ratio = min(1.0, len(sources) / 3.0)
        platform_ratio = min(1.0, len(platforms) / 2.0)
        evidence_weight = min(0.65, math.sqrt(sample_ratio * source_ratio * platform_ratio))
        candidate = baseline_direction + evidence_weight * (raw_direction - baseline_direction)
        candidates[persona] = {
            "available": True,
            "evidencePosts": len(cohort),
            "sourceCount": len(sources),
            "platformCount": len(platforms),
            "maskedDirectionRaw": raw_direction,
            "evidenceWeight": round(evidence_weight, 3),
            "candidateShrunkDirection": round1(candidate),
        }
    return {
        "status": "experimental_not_used_in_formal_scores",
        "formalScoresUnchanged": True,
        "interpretation": {
            "baoma": "家庭/生活资金或稳健长期型表达，不推断真实身份",
            "novice": "明确提问、求教或首次入市型表达",
            "retail": "明确买卖、仓位或主动交易型表达",
        },
        "exclusivePriority": list(PERSONA_EVIDENCE_KEYS),
        "maskedBeforeCandidateScoring": True,
        "evidenceCounts": {key: int(counts.get(key, 0)) for key in PERSONA_EVIDENCE_KEYS},
        "reasonCounts": {
            key: dict(reason_counts.get(key, Counter())) for key in PERSONA_EVIDENCE_KEYS
        },
        "candidates": candidates,
    }



def build_platform_calibration(
    results: Mapping[str, Dict[str, object]],
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> Dict[str, Dict[str, List[float]]]:
    """建立当日的平台内参照分布，修正页面容量和用户语言的结构差异。"""
    calibration: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for source in results.values():
        analysis = source_analysis(source.get("posts", []), as_of, record_type)
        if not analysis:
            continue
        platform = str(source.get("platform") or "unknown")
        for key in SIGNAL_KEYS:
            calibration[platform][key].append(float(analysis["signals"][key]))
        activity = analysis.get("activity") if isinstance(analysis.get("activity"), Mapping) else {}
        calibration[platform]["activityVelocity"].append(
            float(activity.get("logPostingVelocity") or 0.0)
        )
        calibration[platform]["activityBreadth"].append(
            float(activity.get("uniqueAuthorRatio3d") or activity.get("uniqueAuthorRatio24") or 0.0)
        )
    return {
        platform: {key: sorted(values) for key, values in signals.items()}
        for platform, signals in calibration.items()
    }



def percentile_score(
    values: Sequence[float],
    value: float,
    low: float = 20.0,
    high: float = 80.0,
) -> float:
    if len(values) < 8:
        return 50.0
    lower = sum(1 for candidate in values if candidate < value)
    equal = sum(1 for candidate in values if candidate == value)
    percentile = (lower + 0.5 * max(equal, 1)) / len(values)
    # 平台内最冷/最热不触碰 0/100，避免单日横截面制造虚假极值。
    return low + (high - low) * percentile



def calibrate_source_analysis(
    analysis: Dict[str, object],
    platform: str,
    calibration: Mapping[str, Mapping[str, Sequence[float]]],
) -> Dict[str, object]:
    signals = dict(analysis["signals"])
    platform_reference = calibration.get(platform, {})
    for key in SIGNAL_KEYS:
        absolute = float(signals[key])
        reference = platform_reference.get(key, [])
        if len(reference) < 8:
            continue
        relative = percentile_score(reference, absolute)
        absolute_weight = ABSOLUTE_SIGNAL_WEIGHT[key]
        signals[key] = round1(absolute * absolute_weight + relative * (1.0 - absolute_weight))
    activity = dict(analysis.get("activity") or {})
    activity_reference = platform_reference.get("activityVelocity", [])
    breadth_reference = platform_reference.get("activityBreadth", [])
    if len(activity_reference) >= 8:
        velocity_score = percentile_score(
            activity_reference,
            float(activity.get("logPostingVelocity") or 0.0),
            low=15.0,
            high=85.0,
        )
        breadth_score = (
            percentile_score(
                breadth_reference,
                float(activity.get("uniqueAuthorRatio3d") or activity.get("uniqueAuthorRatio24") or 0.0),
                low=25.0,
                high=75.0,
            )
            if len(breadth_reference) >= 8 else 50.0
        )
        activity["velocityScore"] = round1(velocity_score)
        activity["breadthScore"] = round1(breadth_score)
        activity["relativeScore"] = round1(
            ACTIVITY_VELOCITY_WEIGHT * velocity_score
            + ACTIVITY_BREADTH_WEIGHT * breadth_score
        )
    else:
        activity["relativeScore"] = round1(signals.get("attention", 50.0))
        activity["velocityScore"] = activity["relativeScore"]
        activity["breadthScore"] = 50.0
    activity["platformReferenceCount"] = len(activity_reference)
    signals["heat"] = round1(
        signals["fomo"] * 0.30 + signals["newcomer"] * 0.15
        + signals["intensity"] * 0.25 + signals["attention"] * 0.30
    )
    return {**analysis, "signals": signals, "activity": activity}



def _daily_absolute_signals(metrics: Mapping[str, object]) -> Dict[str, float]:
    """Convert one platform/day's text rates to the public six-signal scale."""

    net = float(metrics.get("net") or 0.0)
    fomo = max(0.0, float(metrics.get("fomo") or 0.0))
    panic = max(0.0, float(metrics.get("panic") or 0.0))
    newcomer = max(0.0, float(metrics.get("newcomer") or 0.0))
    intense = max(0.0, float(metrics.get("intense") or 0.0))
    return {
        "optimism": round1(50.0 + 45.0 * net),
        "fomo": signal_from_rate(fomo, "fomo"),
        "panic": round1(100.0 * (1.0 - math.exp(-panic / 0.12))),
        "newcomer": signal_from_rate(newcomer, "newcomer"),
        "intensity": signal_from_rate(intense, "intensity"),
    }




def verified_zero_days_for_source(
    source: Mapping[str, object],
    trading_days: Sequence[date],
    as_of: datetime,
) -> set[str]:
    """Return only days for which a chronological collector proves absence.

    A missing row is not a zero.  We require a head-to-boundary chronological
    read, a fetch made after the completed scoring session, and a raw timestamp
    older than the candidate day.  THS hot_feed is intentionally never eligible.
    """

    if str(source.get("platform") or "") == "ths" or not bool(source.get("chronological")):
        return set()
    if bool(source.get("rawRowsEstimated")):
        return set()
    coverage_status = str(source.get("coverageStatus") or "")
    coverage_proved = bool(source.get("thirdTradingDayReached")) or coverage_status in {
        "third_trading_day_reached",
        "source_exhausted_before_third_trading_day",
    }
    if not coverage_proved:
        return set()
    oldest = parse_time(source.get("oldestSeen"))
    fetched = parse_time(source.get("fetchedAt"))
    if oldest is None or fetched is None:
        return set()
    market_day = scoring_market_day(as_of)
    session_complete = datetime(
        market_day.year,
        market_day.month,
        market_day.day,
        15,
        10,
        tzinfo=CN_TZ,
    )
    if fetched < session_complete:
        return set()
    return {
        day.isoformat()
        for day in trading_days
        if oldest.date() < day <= market_day
    }



def build_daily_calibrated_source_rows(
    results: Mapping[str, Dict[str, object]],
    analyses: Mapping[str, Mapping[str, object] | None],
    trading_days: Sequence[date],
    as_of: datetime,
) -> Dict[str, Dict[str, Dict[str, object]]]:
    """Calibrate each source inside the same platform and the same trading day.

    Emotion keeps its absolute textual meaning (70%-75%) and uses the platform
    percentile only as a capacity/language correction.  Attention is purely a
    same-platform daily percentile, so a 100-row page cannot beat a 50-row page
    merely because the website exposes more rows.
    """

    base_rows: Dict[str, Dict[str, Dict[str, object]]] = {}
    references: Dict[str, Dict[str, Dict[str, List[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for source_key, source in results.items():
        platform = str(source.get("platform") or "unknown")
        analysis = analyses.get(source_key)
        raw_rows = analysis.get("dailyRaw", []) if isinstance(analysis, Mapping) else []
        raw_by_day = {
            str(row.get("date")): row
            for row in raw_rows
            if isinstance(row, Mapping) and row.get("date")
        }
        activity = analysis.get("activity", {}) if isinstance(analysis, Mapping) else {}
        activity_rows = activity.get("dailyPostCounts", []) if isinstance(activity, Mapping) else []
        activity_by_day = {
            str(row.get("date")): row
            for row in activity_rows
            if isinstance(row, Mapping) and row.get("date")
        }
        verified_zeros = verified_zero_days_for_source(source, trading_days, as_of)
        source_days: Dict[str, Dict[str, object]] = {}
        for day in trading_days:
            day_key = day.isoformat()
            raw_row = raw_by_day.get(day_key)
            activity_row = activity_by_day.get(day_key, {})
            if isinstance(raw_row, Mapping) and bool(raw_row.get("available")):
                raw_metrics = raw_row.get("metrics") if isinstance(raw_row.get("metrics"), Mapping) else {}
                signals = _daily_absolute_signals(raw_metrics)
                volume = max(
                    0.0,
                    float(activity_row.get("equivalentPosts") or activity_row.get("postCount") or 0.0),
                )
                post_count = max(0, safe_int(activity_row.get("postCount"), safe_int(raw_row.get("sampleCount"))))
                unique_authors = max(0, safe_int(activity_row.get("uniqueAuthors")))
                breadth = unique_authors / post_count if post_count else 0.0
                source_days[day_key] = {
                    "status": "observed",
                    "sampleCount": max(0, safe_int(raw_row.get("sampleCount"))),
                    "signals": signals,
                    "volume": volume,
                    "uniqueAuthorRatio": breadth,
                }
                for signal_key, value in signals.items():
                    references[platform][day_key][signal_key].append(float(value))
                references[platform][day_key]["volume"].append(volume)
                references[platform][day_key]["breadth"].append(breadth)
            elif day_key in verified_zeros:
                source_days[day_key] = {
                    "status": "verified_zero",
                    "sampleCount": 0,
                    "volume": 0.0,
                }
                references[platform][day_key]["volume"].append(0.0)
                references[platform][day_key]["breadth"].append(0.0)
            else:
                source_days[day_key] = {"status": "unobserved", "sampleCount": 0}
        base_rows[source_key] = source_days

    calibrated: Dict[str, Dict[str, Dict[str, object]]] = {}
    for source_key, source_days in base_rows.items():
        platform = str(results[source_key].get("platform") or "unknown")
        calibrated[source_key] = {}
        for day_key, row in source_days.items():
            if row.get("status") != "observed":
                calibrated[source_key][day_key] = dict(row)
                continue
            reference = references[platform][day_key]
            signals = dict(row.get("signals") or {})
            for signal_key in ("optimism", "fomo", "panic", "newcomer", "intensity"):
                absolute = float(signals.get(signal_key, FAIR_SIGNAL_BASELINES[signal_key]))
                values = reference.get(signal_key, [])
                if len(values) >= 8:
                    relative = percentile_score(values, absolute, low=20.0, high=80.0)
                    absolute_weight = ABSOLUTE_SIGNAL_WEIGHT.get(signal_key, 0.75)
                    signals[signal_key] = round1(
                        absolute_weight * absolute + (1.0 - absolute_weight) * relative
                    )
            volume_values = reference.get("volume", [])
            breadth_values = reference.get("breadth", [])
            if len(volume_values) >= 8:
                volume_score = percentile_score(
                    volume_values,
                    float(row.get("volume") or 0.0),
                    low=15.0,
                    high=85.0,
                )
                breadth_score = (
                    percentile_score(
                        breadth_values,
                        float(row.get("uniqueAuthorRatio") or 0.0),
                        low=25.0,
                        high=75.0,
                    )
                    if len(breadth_values) >= 8 else 50.0
                )
                signals["attention"] = round1(
                    ACTIVITY_VELOCITY_WEIGHT * volume_score
                    + ACTIVITY_BREADTH_WEIGHT * breadth_score
                )
            else:
                signals["attention"] = 50.0
            calibrated[source_key][day_key] = {
                **row,
                "signals": signals,
                "calibrationReferenceCount": len(volume_values),
            }
    return calibrated



def _fair_source_rows(
    results: Mapping[str, Dict[str, object]],
    source_keys: Sequence[str],
    analyses: Mapping[str, Mapping[str, object] | None],
    daily_rows: Mapping[str, Mapping[str, Mapping[str, object]]],
    unit_for_source: Mapping[str, str],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for source_key in source_keys:
        source = results.get(source_key)
        unit_key = str(unit_for_source.get(source_key) or "")
        if not isinstance(source, Mapping) or not unit_key:
            continue
        rows.append({
            "sourceKey": source_key,
            "unitKey": unit_key,
            "companyKey": unit_key,
            "channel": str(source.get("platform") or "unknown"),
            "platform": str(source.get("platform") or "unknown"),
            "analysis": analyses.get(source_key) or {},
            "dailySignals": daily_rows.get(source_key, {}),
        })
    return rows



def _summarize_fairness(
    fair: Dict[str, object],
    unit_names: Mapping[str, str],
    source_rows: Sequence[Mapping[str, object]],
    trading_days: Sequence[date],
    channel_specs: Mapping[str, Mapping[str, object]],
) -> Dict[str, object]:
    diagnostics = dict(fair.get("diagnostics") or {})
    platform_summary: List[Dict[str, object]] = []
    for raw in diagnostics.get("platformSummary", []):
        if not isinstance(raw, Mapping):
            continue
        key = str(raw.get("key") or raw.get("channel") or "")
        platform_summary.append({
            **raw,
            "key": key,
            "name": PLATFORM_NAMES.get(key, key),
            "volumeEligible": bool(raw.get("allowAttention", key != "ths")),
            "formalWeightMeaning": "目标席位固定25%；证据不足部分收缩到中性，不转给其他渠道",
        })
    diagnostics["platformSummary"] = platform_summary

    labelled_units: Dict[str, List[Dict[str, object]]] = {}
    for channel, raw_rows in (diagnostics.get("unitWeights") or {}).items():
        labelled_units[str(channel)] = [
            {**dict(row), "name": unit_names.get(str(row.get("key") or ""), str(row.get("key") or ""))}
            for row in raw_rows if isinstance(row, Mapping)
        ] if isinstance(raw_rows, Sequence) else []
    diagnostics["unitWeights"] = labelled_units

    # Compatibility summary for the existing representative-company cards.
    company_keys = [
        key for key in unit_names
        if any(
            str(spec.get("mode") or "") == "company"
            and key in [str(item) for item in spec.get("expectedUnits", [])]
            for spec in channel_specs.values()
        )
    ]
    company_weights: List[Dict[str, object]] = []
    for company_key in company_keys:
        target = 0.0
        influence = 0.0
        for channel, spec in channel_specs.items():
            expected = [str(item) for item in spec.get("expectedUnits", [])]
            if str(spec.get("mode") or "") != "company" or company_key not in expected:
                continue
            target += 0.25 / max(1, len(expected))
            for row in labelled_units.get(str(channel), []):
                if str(row.get("key") or "") == company_key:
                    influence += float(row.get("formalInfluence") or 0.0)
        company_weights.append({
            "key": company_key,
            "name": unit_names.get(company_key, company_key),
            "targetWeight": round(target, 6),
            "effectiveWeight": round(influence, 6),
            "neutralisedWeight": round(max(0.0, target - influence), 6),
        })
    diagnostics["companyWeights"] = company_weights
    diagnostics["expectedCompanyCount"] = len(company_keys)
    observed_company_keys = {
        str(row.get("unitKey") or "")
        for row in source_rows
        if str(row.get("unitKey") or "") in company_keys and bool((row.get("analysis") or {}).get("sampleCount"))
    }
    diagnostics["providedCompanyCount"] = len(observed_company_keys)

    daily_diagnostics = [
        row for row in diagnostics.get("effectiveDayWeights", []) if isinstance(row, Mapping)
    ]
    point_coverage = diagnostics.get("channelPointCoverage") if isinstance(diagnostics.get("channelPointCoverage"), Mapping) else {}
    coverage = sum(
        float((values or {}).get("optimism") or 0.0)
        for values in point_coverage.values() if isinstance(values, Mapping)
    ) / max(1, len(FAIR_CHANNELS))
    latest_channels = daily_diagnostics[0].get("channels", {}) if daily_diagnostics else {}
    latest_observed = sum(
        isinstance(row, Mapping) and str(row.get("status") or "") == "observed"
        for row in latest_channels.values()
    ) if isinstance(latest_channels, Mapping) else 0
    reliability = float(diagnostics.get("reliabilityPercent") or 0.0)

    base_composite = float((fair.get("scores") or {}).get("composite", 50.0))
    sensitivity_values = [base_composite]
    for omitted_channel in FAIR_CHANNELS:
        if any(str(row.get("channel") or "") == omitted_channel for row in source_rows):
            trial_rows = [
                row for row in source_rows
                if str(row.get("channel") or "") != omitted_channel
            ]
            trial = aggregate_channel_fairness(
                trial_rows,
                [day.isoformat() for day in trading_days],
                channel_specs,
            )
            sensitivity_values.append(float((trial.get("scores") or {}).get("composite", 50.0)))
    stability_low = min(sensitivity_values)
    stability_high = max(sensitivity_values)
    stability_delta = max(abs(value - base_composite) for value in sensitivity_values)
    concentration = diagnostics.get("concentration") if isinstance(diagnostics.get("concentration"), Mapping) else {}
    observed_channels = safe_int((fair.get("metrics") or {}).get("observedChannelCount"))
    rank_eligible = bool(
        fair.get("available")
        and observed_channels >= 3
        and coverage >= 0.45
        and latest_observed >= 2
        and reliability >= 20.0
        and stability_delta <= 10.0
        and not bool(concentration.get("capBreached"))
    )
    if reliability >= 55.0 and coverage >= 0.80 and observed_channels == 4 and stability_delta <= 5.0:
        confidence_label = "高"
    elif reliability >= 20.0 and coverage >= 0.45 and observed_channels >= 3 and stability_delta <= 10.0:
        confidence_label = "中"
    else:
        confidence_label = "低"
    diagnostics.update({
        "modelVersion": FAIRNESS_MODEL_VERSION,
        # Compatibility field retained; the precise meaning is that sample
        # proportions never buy a larger seat. Each unit's own n may only
        # shrink that fixed seat toward neutral.
        "formalPointEstimateNotSampleWeighted": True,
        "formalPointEstimateNotSampleProportionWeighted": True,
        "sampleCountAffectsOwnNeutralShrinkOnly": True,
        "fixedChannelTargetWeight": 0.25,
        "channelWeightsNeverRedistributed": True,
        "weightedCompanyCoverage": round(coverage, 6),
        "weightedChannelCoverage": round(coverage, 6),
        "latestObservedCompanyCount": len(observed_company_keys),
        "latestObservedChannelCount": latest_observed,
        "observedChannelCount": observed_channels,
        "rankEligible": rank_eligible,
        "confidenceLabel": confidence_label,
        "stabilityBand": {
            "type": "leave_one_channel_out",
            "low": round(stability_low, 1),
            "high": round(stability_high, 1),
            "maxDelta": round(stability_delta, 1),
            "isStatisticalConfidenceInterval": False,
        },
    })
    return diagnostics



def score_fair_group(
    results: Mapping[str, Dict[str, object]],
    source_keys: Sequence[str],
    analyses: Mapping[str, Mapping[str, object] | None],
    daily_rows: Mapping[str, Mapping[str, Mapping[str, object]]],
    unit_for_source: Mapping[str, str],
    unit_names: Mapping[str, str],
    trading_days: Sequence[date],
    channel_specs: Mapping[str, Mapping[str, object]],
) -> Dict[str, object]:
    source_rows = _fair_source_rows(
        results,
        source_keys,
        analyses,
        daily_rows,
        unit_for_source,
    )
    fair = aggregate_channel_fairness(
        source_rows,
        [day.isoformat() for day in trading_days],
        channel_specs,
    )
    fair["diagnostics"] = _summarize_fairness(
        fair,
        unit_names,
        source_rows,
        trading_days,
        channel_specs,
    )
    volume_rows = [
        row for row in source_rows
        if str(row.get("channel") or "") != "ths"
        and isinstance(row.get("analysis"), Mapping)
    ]
    lower_bound = sum(
        float((row.get("analysis", {}).get("activity") or {}).get("weightedDailyPosts3d") or 0.0)
        for row in volume_rows
    )
    expected_volume_units = sum(
        len(spec.get("expectedUnits", []))
        for channel, spec in channel_specs.items()
        if bool(spec.get("allowAttention", True))
    )
    observed_volume_units = len({
        (str(row.get("channel") or ""), str(row.get("unitKey") or ""))
        for row in volume_rows if bool((row.get("analysis") or {}).get("sampleCount"))
    })
    fair["metrics"] = {
        **dict(fair.get("metrics") or {}),
        "coverageAdjustedVolume": round(lower_bound, 3),
        "weightedVolumeLowerBound": round(lower_bound, 3),
        "volumeCoverage": round(observed_volume_units / max(1, expected_volume_units), 6),
    }
    return fair



def capped_distribution(raw_weights: Sequence[float], cap: float, target: float = 1.0) -> List[float]:
    if not raw_weights or target <= 0:
        return [0.0 for _ in raw_weights]
    target = min(target, cap * len(raw_weights))
    remaining = set(range(len(raw_weights)))
    result = [0.0] * len(raw_weights)
    remaining_target = target
    while remaining and remaining_target > 1e-9:
        total = sum(max(raw_weights[index], 1e-9) for index in remaining)
        newly_capped: List[int] = []
        for index in remaining:
            proposed = remaining_target * max(raw_weights[index], 1e-9) / total
            if proposed >= cap - 1e-9:
                result[index] = cap
                newly_capped.append(index)
        if not newly_capped:
            for index in remaining:
                result[index] = remaining_target * max(raw_weights[index], 1e-9) / total
            break
        for index in newly_capped:
            remaining.remove(index)
            remaining_target -= cap
    return result



def select_samples(
    analyses: Sequence[Dict[str, object]],
    limit: int = 10,
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> List[Dict[str, object]]:
    pool: List[Dict[str, object]] = []
    for analysis in analyses:
        pool.extend(analysis.get("posts", []))
    pool.sort(
        key=lambda post: (
            abs(int(post.get("rawScore") or 0)) * 2.5
            + (math.log1p(safe_int(post.get("comments"))) if record_type == MEASURED else 0.0)
            + (2.0 if post_age_days(post, as_of) <= 1 else 0.0)
            - post_age_days(post, as_of) * 0.05
        ),
        reverse=True,
    )
    selected: List[Dict[str, object]] = []
    platform_counts: Dict[str, int] = defaultdict(int)
    for post in pool:
        platform = str(post.get("platform") or "unknown")
        if platform_counts[platform] >= 4:
            continue
        selected.append({
            "title": post.get("title"),
            "label": post.get("label"),
            "score": safe_int(post.get("rawScore")),
            "comments": safe_int(post.get("comments")) if record_type == MEASURED else 0,
            "clicks": safe_int(post.get("clicks")) if record_type == MEASURED else 0,
            "source": post.get("source") or PLATFORM_NAMES.get(platform, "公开社区"),
            "platform": platform,
            "platformName": PLATFORM_NAMES.get(platform, platform),
            "publishTime": post.get("publishTime"),
        })
        platform_counts[platform] += 1
        if len(selected) >= limit:
            break
    return selected



def analyze_group(
    posts: Sequence[Dict[str, object]],
    calibration: Mapping[str, Mapping[str, Sequence[float]]] | None = None,
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> Dict[str, object]:
    by_source: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for post in posts:
        by_source[str(post.get("sourceKey") or "unknown")].append(post)
    source_rows: List[Tuple[str, Dict[str, object]]] = []
    for source_key, source_posts in by_source.items():
        analysis = source_analysis(source_posts, as_of, record_type)
        if analysis:
            platform = str(source_posts[0].get("platform") or source_key.split(":", 1)[0])
            if calibration:
                analysis = calibrate_source_analysis(analysis, platform, calibration)
            source_rows.append((source_key, analysis))

    if not source_rows:
        empty = {key: 50.0 for key in (*SIGNAL_KEYS, "heat")}
        empty["panic"] = 0.0
        return {
            "available": False,
            "signals": empty,
            "scores": score_personas(empty, 50.0),
            "diagnostics": {
                "totalPosts": len(posts), "recentPosts": 0, "recent7Posts": 0,
                "samplePosts": 0, "bullishPosts": 0, "bearishPosts": 0,
                "neutralPosts": 0, "bullishRatio": 0.0, "bearishRatio": 0.0,
                "fomoRate": 0.0, "panicRate": 0.0, "newcomerRate": 0.0,
                "platformCount": 0, "sourceCount": 0, "activityIndex": 50.0,
                "estimatedDailyPosts": 0.0, "uniqueAuthors24": 0,
                "weightedDailyPosts3d": 0.0, "tradingWindowPosts": 0, "uniqueAuthors3d": 0,
                "personaEvidence": persona_evidence_diagnostics([], 50.0, as_of),
            },
            "samplePosts": [], "platforms": [], "breadth": 50.0,
        }

    by_platform: Dict[str, List[Tuple[str, Dict[str, object]]]] = defaultdict(list)
    for source_key, analysis in source_rows:
        platform = source_key.split(":", 1)[0]
        by_platform[platform].append((source_key, analysis))

    platform_rows: List[Dict[str, object]] = []
    for platform, rows in by_platform.items():
        reliability = [min(1.0, math.sqrt(float(item["sampleCount"]) / 20.0)) for _, item in rows]
        source_cap = 1.0 if len(rows) == 1 else 0.60 if len(rows) == 2 else 0.55
        source_weights = capped_distribution(reliability, cap=source_cap, target=1.0)
        signals = {
            key: round1(sum(weight * float(item["signals"][key]) for weight, (_, item) in zip(source_weights, rows)))
            for key in (*SIGNAL_KEYS, "panic", "heat")
        }
        raw = {
            key: sum(weight * float(item["raw"][key]) for weight, (_, item) in zip(source_weights, rows))
            for key in ("net", "fomo", "panic", "newcomer", "intense")
        }
        activity_rows = [item.get("activity", {}) for _, item in rows]
        activity_relative = sum(
            weight * float(activity.get("relativeScore") or 50.0)
            for weight, activity in zip(source_weights, activity_rows)
        )
        platform_rows.append({
            "key": platform,
            "name": PLATFORM_NAMES.get(platform, platform),
            # 同花顺当前公开端点是 hot_feed，不是严格时间流；它可以参与
            # 文本情绪，但在找到可验证时间流前不参与发帖速度排名。
            "activityEligible": platform in {"eastmoney", "sina"},
            "signals": signals,
            "raw": raw,
            "sampleCount": sum(int(item["sampleCount"]) for _, item in rows),
            "sourceCount": len(rows),
            "recent24": sum(int(item["recent24"]) for _, item in rows),
            "activity": {
                "relativeScore": round1(activity_relative),
                "postingVelocity": round(sum(
                    float(activity.get("postingVelocity") or 0.0)
                    for activity in activity_rows
                ), 3),
                "weightedDailyPosts3d": round(sum(
                    float(activity.get("weightedDailyPosts3d") or activity.get("postingVelocity") or 0.0)
                    for activity in activity_rows
                ), 3),
                "uniqueAuthors3d": sum(
                    safe_int(activity.get("uniqueAuthors3d")) for activity in activity_rows
                ),
                "uniqueAuthors24": sum(
                    safe_int(activity.get("uniqueAuthors24")) for activity in activity_rows
                ),
                "lowerBoundSourceCount": sum(
                    bool(activity.get("lowerBound")) for activity in activity_rows
                ),
            },
            "reliability": min(1.0, math.sqrt(sum(int(item["sampleCount"]) for _, item in rows) / 24.0)),
        })

    # 缺平台属于覆盖度问题，不是“中性情绪”。只在真实可用的平台之间归一化，
    # 再通过 diagnostics / history.coverage 降低置信度，避免默认 50 污染指数。
    platform_cap = 1.0 if len(platform_rows) == 1 else 0.65 if len(platform_rows) == 2 else 0.40
    platform_weights = capped_distribution(
        [float(row["reliability"]) for row in platform_rows],
        cap=platform_cap,
        target=1.0,
    )
    signals: Dict[str, float] = {}
    for key in SIGNAL_KEYS:
        value = normalized_weighted_average(
            (weight, float(row["signals"][key])) for weight, row in zip(platform_weights, platform_rows)
        )
        signals[key] = round1(value if value is not None else 50.0)
    panic_value = normalized_weighted_average(
        (weight, float(row["signals"]["panic"])) for weight, row in zip(platform_weights, platform_rows)
    )
    signals["panic"] = round1(panic_value if panic_value is not None else 0.0)
    signals["heat"] = round1(
        signals["fomo"] * 0.30 + signals["newcomer"] * 0.15
        + signals["intensity"] * 0.25 + signals["attention"] * 0.30
    )
    activity_index_value = normalized_weighted_average(
        (
            weight if bool(row.get("activityEligible")) else 0.0,
            float(row.get("activity", {}).get("relativeScore") or 50.0),
        )
        for weight, row in zip(platform_weights, platform_rows)
    )
    activity_index = round1(activity_index_value if activity_index_value is not None else signals["attention"])

    confidence = min(1.0, len(source_rows) / 5.0)
    hot_share = sum(1 for _, analysis in source_rows if float(analysis["signals"]["heat"]) >= 50.0) / len(source_rows)
    breadth = round1(50.0 + (hot_share - 0.5) * 100.0 * confidence)
    scores = score_personas(signals, breadth)

    annotated = [analysis for _, analysis in source_rows]
    persona_diagnostics = persona_evidence_diagnostics(annotated, signals["optimism"], as_of)
    sample_count = sum(int(item["sampleCount"]) for item in annotated)
    bullish = sum(int(item["bullish"]) for item in annotated)
    bearish = sum(int(item["bearish"]) for item in annotated)
    neutral = sum(int(item["neutral"]) for item in annotated)
    raw_weights = [int(item["sampleCount"]) for item in annotated]
    raw_total = max(sum(raw_weights), 1)
    raw_aggregate = {
        key: sum(weight * float(item["raw"][key]) for weight, item in zip(raw_weights, annotated)) / raw_total
        for key in ("fomo", "panic", "newcomer")
    }
    for weight, row in zip(platform_weights, platform_rows):
        row["weight"] = round(weight * 100.0, 1)
        row.pop("reliability", None)
    attention_status = Counter(
        str(item.get("attentionBaseline", {}).get("status") or "unknown") for item in annotated
    )
    attention_reasons = Counter(
        str(item.get("attentionBaseline", {}).get("reason"))
        for item in annotated if item.get("attentionBaseline", {}).get("reason")
    )
    sampling_rows = [item.get("samplingPolicy", {}) for item in annotated]
    window_days = scoring_trading_days(as_of)
    daily_count_map: Dict[str, Dict[str, float]] = {
        day.isoformat(): {"postCount": 0.0, "equivalentPosts": 0.0, "uniqueAuthors": 0.0}
        for day in window_days
    }
    for item in annotated:
        activity = item.get("activity") if isinstance(item.get("activity"), Mapping) else {}
        rows = activity.get("dailyPostCounts") if isinstance(activity.get("dailyPostCounts"), list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            day_text = str(row.get("date") or "")
            if day_text not in daily_count_map:
                continue
            daily_count_map[day_text]["postCount"] += max(0, safe_int(row.get("postCount")))
            daily_count_map[day_text]["equivalentPosts"] += max(0.0, float(row.get("equivalentPosts") or 0.0))
            daily_count_map[day_text]["uniqueAuthors"] += max(0, safe_int(row.get("uniqueAuthors")))
    group_effective_weights = normalized_trading_day_weights(
        as_of,
        (
            date.fromisoformat(day_text)
            for day_text, counts in daily_count_map.items()
            if counts["postCount"] > 0
        ),
    )
    daily_post_counts = [
        {
            "date": day.isoformat(),
            "rawWeight": TRADING_DAY_WEIGHTS[index],
            "effectiveWeight": round(group_effective_weights.get(day, 0.0), 6),
            "postCount": int(daily_count_map[day.isoformat()]["postCount"]),
            "equivalentPosts": round(daily_count_map[day.isoformat()]["equivalentPosts"], 1),
            "uniqueAuthors": int(daily_count_map[day.isoformat()]["uniqueAuthors"]),
            "available": daily_count_map[day.isoformat()]["postCount"] > 0,
        }
        for index, day in enumerate(window_days)
    ]
    diagnostics = {
        "totalPosts": len(posts),
        "recentPosts": sum(int(item["recent24"]) for item in annotated),
        "recent7Posts": sum(int(item["recent7"]) for item in annotated),
        "samplePosts": sample_count,
        "bullishPosts": bullish,
        "bearishPosts": bearish,
        "neutralPosts": neutral,
        "bullishRatio": round(bullish / max(sample_count, 1) * 100.0, 1),
        "bearishRatio": round(bearish / max(sample_count, 1) * 100.0, 1),
        "fomoRate": round(raw_aggregate["fomo"] * 100.0, 1),
        "panicRate": round(raw_aggregate["panic"] * 100.0, 1),
        "newcomerRate": round(raw_aggregate["newcomer"] * 100.0, 1),
        "platformCount": len(platform_rows),
        "sourceCount": len(source_rows),
        "activityIndex": activity_index,
        "estimatedDailyPosts": round(sum(
            float(item.get("activity", {}).get("postingVelocity") or 0.0)
            for item in annotated
        ), 1),
        "weightedDailyPosts3d": round(sum(
            float(item.get("activity", {}).get("weightedDailyPosts3d") or 0.0)
            for item in annotated
        ), 1),
        "tradingDays": [day.isoformat() for day in window_days],
        "dailyPostCounts": daily_post_counts,
        "currentTradingDayPosts": int(daily_post_counts[0]["postCount"]) if daily_post_counts else 0,
        "activityVelocityWeight": ACTIVITY_VELOCITY_WEIGHT,
        "activityBreadthWeight": ACTIVITY_BREADTH_WEIGHT,
        "tradingWindowPosts": sum(
            safe_int(item.get("tradingWindowPosts")) for item in annotated
        ),
        "uniqueAuthors3d": sum(
            safe_int(item.get("activity", {}).get("uniqueAuthors3d")) for item in annotated
        ),
        "uniqueAuthors24": sum(
            safe_int(item.get("activity", {}).get("uniqueAuthors24")) for item in annotated
        ),
        "activityLowerBoundSourceCount": sum(
            bool(item.get("activity", {}).get("lowerBound")) for item in annotated
        ),
        "available": True,
        "personaEvidence": persona_diagnostics,
        "samplingPolicy": {
            "mode": record_type,
            "capPerSourcePublishedDay": ESTIMATED_SOURCE_DAY_LIMIT if record_type != MEASURED else None,
            "postsBefore": sum(safe_int(item.get("postsBefore")) for item in sampling_rows),
            "postsAfterAuthorLimit": sum(safe_int(item.get("postsAfterAuthorLimit")) for item in sampling_rows),
            "postsSelected": sum(safe_int(item.get("postsSelected")) for item in sampling_rows),
            "activityPosts": sum(safe_int(item.get("activityPostCount")) for item in annotated),
            "sourceDayBucketsCapped": sum(safe_int(item.get("sourceDayBucketsCapped")) for item in sampling_rows),
            "selectionUsesSentiment": False,
        },
        "attentionBaseline": {
            "mode": record_type,
            "method": "three_trading_day_weighted_post_volume",
            "sourceStatusCounts": dict(attention_status),
            "fallbackReasons": dict(attention_reasons),
            "missingDaysAssumedZero": False,
            "tradingDayWeights": list(TRADING_DAY_WEIGHTS),
            "usesFutureDates": False,
            "usesInteractions": False,
        },
    }
    return {
        "available": True,
        "signals": signals,
        "scores": scores,
        "diagnostics": diagnostics,
        "samplePosts": select_samples(annotated, as_of=as_of, record_type=record_type),
        "platforms": platform_rows,
        "breadth": breadth,
    }



def source_public_info(result: Mapping[str, object]) -> Dict[str, object]:
    return {
        "code": result.get("code"),
        "name": result.get("name"),
        "url": result.get("url"),
        "platform": result.get("platform"),
        "platformName": PLATFORM_NAMES.get(str(result.get("platform")), str(result.get("platform"))),
        "fresh": bool(result.get("fresh")),
        "coverage": {
            "rawRows": safe_int(result.get("rawRows")),
            "pageCount": safe_int(result.get("pageCount")),
            "oldestSeen": result.get("oldestSeen"),
            "newestSeen": result.get("newestSeen"),
            "capHit": result.get("capHit"),
            "chronological": bool(result.get("chronological")),
            "coverageDays": list(result.get("coverageDays") or []),
            "thirdTradingDayReached": bool(result.get("thirdTradingDayReached")),
            "freshFetchedNow": bool(result.get("freshFetchedNow")),
            "cacheAge": result.get("cacheAge"),
            "coverageStatus": result.get("coverageStatus"),
            "paginationMode": result.get("paginationMode"),
        },
    }



def sector_attention_weights(sectors: Sequence[Dict[str, object]]) -> List[float]:
    if not sectors:
        return []
    equal = 1.0 / len(sectors)
    reliability = [
        math.sqrt(
            min(1.0, float(item.get("metrics", {}).get("sourceCount", 0)) / max(1, int(item.get("expectedSourceCount") or 5)))
            * min(1.0, float(item.get("metrics", {}).get("platformCount", 0)) / len(PLATFORM_NAMES))
        )
        for item in sectors
    ]
    reliability_total = max(sum(reliability), 1e-9)
    attention = [
        max(1.0, float(item["signals"].get("attention", 50.0))) * max(0.15, quality)
        for item, quality in zip(sectors, reliability)
    ]
    total_attention = sum(attention)
    # “等权”部分也按真实覆盖质量分配；缺来源通过低权重/低置信度表达，
    # 不用中性 50 伪造一个完整板块。
    raw = [
        0.5 * quality / reliability_total + 0.5 * value / total_attention
        for quality, value in zip(reliability, attention)
    ]
    return capped_distribution(raw, cap=2.0 * equal, target=1.0)



def aggregate_sector_parents(
    sectors: Sequence[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Use fixed leaf seats inside fixed parent seats.

    A hotter child, an extra platform, or a missing sibling must never acquire
    another child's vote.  Missing leaves retain a neutral seat and are exposed
    as lower coverage instead of silently donating their weight.
    """

    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    expected_counts: Counter[str] = Counter()
    parent_names: Dict[str, str] = {}
    for item in sectors:
        sector_key = str(item.get("key") or "")
        parent_key = str(item.get("parentKey") or sector_key)
        parent_name = str(item.get("parentName") or item.get("name") or parent_key)
        expected_counts[parent_key] += 1
        parent_names[parent_key] = parent_name
        if bool(item.get("available")):
            grouped[parent_key].append(item)

    ordered_parent_keys = list(PARENT_SECTORS)
    ordered_parent_keys.extend(sorted(set(grouped) - set(ordered_parent_keys)))
    rows: List[Dict[str, object]] = []
    for parent_key in ordered_parent_keys:
        children = grouped.get(parent_key, [])
        expected = max(1, int(expected_counts.get(parent_key) or sum(
            1 for definition in SECTORS.values()
            if str(definition.get("parentKey") or "") == parent_key
        )))
        signals: Dict[str, float] = {}
        for signal_key in (*SIGNAL_KEYS, "panic", "heat"):
            baseline = FAIR_SIGNAL_BASELINES.get(signal_key, 50.0)
            deviation = sum(
                float(child["signals"].get(signal_key, baseline)) - baseline
                for child in children
            ) / expected
            signals[signal_key] = round1(baseline + deviation)
        rows.append({
            "key": parent_key,
            "name": parent_names.get(parent_key, PARENT_SECTORS.get(parent_key, parent_key)),
            "available": bool(children),
            "availableChildCount": len(children),
            "expectedChildCount": expected,
            "coverage": round(len(children) / expected, 6),
            "childWeight": round(1.0 / expected, 6),
            "missingChildrenNeutralised": max(0, expected - len(children)),
            "signals": signals,
        })
    return rows



def prepare_history() -> List[Dict[str, object]]:
    old_latest = load_json(LATEST_PATH, {})
    old_history = load_json(HISTORY_PATH, [])
    # 只有明确读到带版本号的旧快照才执行版本迁移。latest.json 暂时缺失、
    # 损坏或尚未写完时，绝不能把一份完好的历史静默清空。
    has_explicit_version = isinstance(old_latest, dict) and "version" in old_latest
    old_version = safe_int(old_latest.get("version"), VERSION) if has_explicit_version else VERSION
    if has_explicit_version and old_version < VERSION:
        if isinstance(old_history, list) and old_history:
            backup = V3_HISTORY_PATH if old_version == 3 else LEGACY_HISTORY_PATH
            if not backup.exists():
                save_json(backup, old_history)
        # Keep the old points visible, but never pass them off as comparable to
        # the fixed-channel v6 series.  The methodology segment is attached
        # below and the first v6 update starts a new measured regime.
    history = old_history if isinstance(old_history, list) else []
    upgraded: List[Dict[str, object]] = []
    for raw in history:
        if not isinstance(raw, Mapping):
            continue
        point = dict(raw)
        point.setdefault("methodology", "legacy_unspecified")
        try:
            point_day = date.fromisoformat(str(point.get("date") or ""))
        except ValueError:
            continue
        # 新口径的历史横轴只保留真实交易日；旧周末/休市点在下次更新时清理。
        if not is_trading_day(point_day):
            continue
        sectors = [
            dict(item) for item in point.get("sectors", [])
            if isinstance(item, Mapping)
        ]
        if sectors:
            # 只迁移旧结构；已经过 v2 截面校准或因果平滑的历史点必须原样
            # 保留，否则每天点击更新都会把历史平滑结果重新覆盖成原始值。
            needs_heat_upgrade = any(
                item.get("relativeHeat") is None
                or not isinstance(item.get("heatModel"), Mapping)
                for item in sectors
            )
            if needs_heat_upgrade:
                calibrate_sector_heats(sectors, float(point.get("heat") or 50.0))
            point["sectors"] = sectors
        upgraded.append(point)
    return upgraded



def score_results_as_of(
    results: Mapping[str, Dict[str, object]],
    market_keys: Sequence[str],
    sector_source_keys: Mapping[str, Sequence[str]],
    *,
    as_of: datetime = NOW,
    record_type: str = MEASURED,
) -> Dict[str, object]:
    """从一份原始文本集合计算指定时点快照，不执行任何联网请求。

    所有底层分析都收到同一个 ``as_of``，未来文本会在 source_analysis 中
    被排除。缺失的来源、平台和板块只降低覆盖度，不以 50 分混入总值。
    ``record_type != measured`` 时互动累计量不参与权重或活跃度。
    """

    trading_days = scoring_trading_days(as_of)
    analyses: Dict[str, Mapping[str, object] | None] = {
        key: source_analysis(source.get("posts", []), as_of, record_type)
        for key, source in results.items()
    }
    platform_calibration = build_platform_calibration(results, as_of, record_type)
    daily_calibrated_rows = build_daily_calibrated_source_rows(
        results,
        analyses,
        trading_days,
        as_of,
    )
    visible_source_keys = {
        key
        for key, analysis in analyses.items()
        if analysis is not None
    }
    visible_platforms = {
        str(results[key].get("platform") or "unknown")
        for key in visible_source_keys
    }

    sector_indices: List[Dict[str, object]] = []
    for sector_key, definition in SECTORS.items():
        keys = list(sector_source_keys.get(sector_key, []))
        posts = merge_posts(results, keys)
        analyzed = analyze_group(posts, platform_calibration, as_of, record_type)
        representatives = [
            dict(company)
            for company in definition.get("representativeCompanies", [])
            if isinstance(company, Mapping)
        ][:3]
        company_names = {
            str(company.get("code") or ""): str(company.get("name") or company.get("code") or "")
            for company in representatives
        }
        ths_communities = [
            dict(item) for item in definition.get("thsCommunities", [])
            if isinstance(item, Mapping)
        ]
        ths_codes = [str(item.get("code") or "") for item in ths_communities if item.get("code")]
        unit_names = {
            **company_names,
            **{
                str(item.get("code") or ""): str(item.get("name") or item.get("code") or "")
                for item in ths_communities
            },
        }
        channel_specs = {
            "eastmoney": {"mode": "company", "expectedUnits": list(company_names), "allowAttention": True},
            "sina": {"mode": "company", "expectedUnits": list(company_names), "allowAttention": True},
            "ths": {"mode": "sector", "expectedUnits": ths_codes, "allowAttention": False},
            "taoguba": {"mode": "company", "expectedUnits": list(company_names), "allowAttention": True},
        }
        unit_for_source: Dict[str, str] = {}
        for source_key in keys:
            if source_key not in results:
                continue
            platform = str(results[source_key].get("platform") or "")
            source_code = str(results[source_key].get("code") or "")
            if not source_code and platform in {"eastmoney", "sina", "taoguba"}:
                source_code = source_key.split(":", 1)[-1]
            expected_units = [str(item) for item in channel_specs.get(platform, {}).get("expectedUnits", [])]
            if source_code in expected_units:
                unit_for_source[source_key] = source_code
        fair = score_fair_group(
            results,
            keys,
            analyses,
            daily_calibrated_rows,
            unit_for_source,
            unit_names,
            trading_days,
            channel_specs,
        )
        diagnostics = analyzed["diagnostics"]
        fairness = fair["diagnostics"]
        formal_signals = fair["signals"]
        formal_scores = fair["scores"]
        working_sources = [
            source_public_info(results[key])
            for key in keys if key in visible_source_keys
        ]
        platform_summary = {
            str(row.get("key")): row
            for row in fairness.get("platformSummary", [])
            if isinstance(row, Mapping)
        }
        public_platforms = []
        for row in analyzed["platforms"]:
            summary = platform_summary.get(str(row.get("key")), {})
            public_platforms.append({
                **row,
                "weight": round(float(summary.get("emotionWeight") or 0.0) * 100.0, 1),
                "attentionWeight": round(float(summary.get("attentionWeight") or 0.0) * 100.0, 1),
                "targetWeight": round(float(summary.get("targetWeight") or 0.25) * 100.0, 1),
                "formalInfluence": round(float(summary.get("formalInfluence") or 0.0) * 100.0, 1),
                "formalWeightMeaning": "正式情绪/关注影响；缺失部分保留中性席位",
                "diagnosticOnlyLegacySignals": True,
            })
        sector_indices.append({
            "key": sector_key,
            "name": definition["name"],
            "parentKey": definition.get("parentKey") or sector_key,
            "parentName": definition.get("parentName") or definition["name"],
            "description": str(definition.get("description") or ""),
            "representativeCompanies": representatives,
            "catalogVersion": SECTOR_CATALOG_VERSION,
            "available": bool(fair.get("available")),
            "expectedSourceCount": len(keys),
            "scores": formal_scores,
            "signals": formal_signals,
            "heat": formal_signals["heat"],
            "direction": formal_signals["optimism"],
            "metrics": {
                "recentPosts": diagnostics["recentPosts"],
                "recent7Posts": diagnostics["recent7Posts"],
                "samplePosts": diagnostics["samplePosts"],
                "bullishRatio": diagnostics["bullishRatio"],
                "bearishRatio": diagnostics["bearishRatio"],
                "fomoRate": diagnostics["fomoRate"],
                "panicRate": diagnostics["panicRate"],
                "newcomerRate": diagnostics["newcomerRate"],
                "activityIndex": formal_signals.get("attention", 50.0),
                "estimatedDailyPosts": fair.get("metrics", {}).get("coverageAdjustedVolume") or 0.0,
                "weightedDailyPosts3d": fair.get("metrics", {}).get("coverageAdjustedVolume") or 0.0,
                "weightedVolumeLowerBound": fair.get("metrics", {}).get("weightedVolumeLowerBound") or 0.0,
                "volumeCoverage": fair.get("metrics", {}).get("volumeCoverage") or 0.0,
                "effectiveSample": fair.get("metrics", {}).get("effectiveSample") or 0.0,
                "tradingDays": [day.isoformat() for day in trading_days],
                "dailyPostCounts": diagnostics.get("dailyPostCounts", []),
                "currentTradingDayPosts": diagnostics.get("currentTradingDayPosts", 0),
                "tradingWindowPosts": diagnostics.get("tradingWindowPosts", 0),
                "uniqueAuthors24": diagnostics.get("uniqueAuthors24", 0),
                "uniqueAuthors3d": diagnostics.get("uniqueAuthors3d", 0),
                "activityLowerBoundSourceCount": diagnostics.get("activityLowerBoundSourceCount", 0),
                "sourceCount": diagnostics["sourceCount"],
                "platformCount": diagnostics["platformCount"],
                "personaEvidence": diagnostics.get("personaEvidence", {}),
                "samplingPolicy": diagnostics.get("samplingPolicy", {}),
                "attentionBaseline": diagnostics.get("attentionBaseline", {}),
                "fairness": fairness,
            },
            "platforms": public_platforms,
            "forumSources": working_sources,
            "samplePosts": analyzed["samplePosts"],
        })

    available_sectors = [item for item in sector_indices if item["available"]]
    parent_rows = aggregate_sector_parents(sector_indices)
    sector_signals: Dict[str, float] | None = None
    sector_scores: Dict[str, int] | None = None
    breadth_score: float | None = None
    if parent_rows:
        # 大类严格固定席位：缺失大类保持中性，不把票转送给仍可用的大类。
        parent_weight = 1.0 / len(parent_rows)
        sector_signals = {
            key: round1(sum(
                parent_weight * float(item["signals"][key])
                for item in parent_rows
            ))
            for key in (*SIGNAL_KEYS, "panic", "heat")
        }
        hot_parent_count = sum(
            1 for item in parent_rows if float(item["signals"]["heat"]) > 50.0
        )
        cold_parent_count = sum(
            1 for item in parent_rows if float(item["signals"]["heat"]) < 50.0
        )
        breadth_score = round1(
            50.0 + 50.0 * (hot_parent_count - cold_parent_count) / len(parent_rows)
        )
        sector_scores = score_personas(sector_signals, breadth_score)

    market_posts = merge_posts(results, market_keys)
    broad_legacy = analyze_group(market_posts, platform_calibration, as_of, record_type)
    formal_market_keys = list(market_keys)
    eastmoney_market_units = [code for code, _ in MARKET_EASTMONEY_SOURCES]
    market_channel_specs = {
        "eastmoney": {"mode": "sector", "expectedUnits": eastmoney_market_units, "allowAttention": True},
        "sina": {"mode": "sector", "expectedUnits": ["broad-market"], "allowAttention": True},
        "ths": {"mode": "sector", "expectedUnits": ["1A0001"], "allowAttention": False},
        "taoguba": {"mode": "sector", "expectedUnits": ["broad-market"], "allowAttention": True},
    }
    market_unit_for_source = {
        key: _market_unit_for_key(key, results)
        for key in formal_market_keys if key in results
    }
    market_unit_names = {
        **{code: name for code, name in MARKET_EASTMONEY_SOURCES},
        "1A0001": "同花顺上证指数讨论",
        "broad-market": "全市场社区席位",
    }
    broad_fair = score_fair_group(
        results,
        formal_market_keys,
        analyses,
        daily_calibrated_rows,
        market_unit_for_source,
        market_unit_names,
        trading_days,
        market_channel_specs,
    )
    broad_diagnostics = dict(broad_legacy.get("diagnostics") or {})
    broad_diagnostics.update({
        "activityIndex": broad_fair["signals"].get("attention", 50.0),
        "weightedDailyPosts3d": broad_fair.get("metrics", {}).get("coverageAdjustedVolume") or 0.0,
        "fairness": broad_fair["diagnostics"],
        "thsMarketFeedFormalEmotionTargetWeight": 0.25,
        "thsMarketFeedFormalAttentionWeight": 0.0,
    })
    broad = {
        **broad_legacy,
        "available": bool(broad_fair.get("available")),
        "signals": broad_fair["signals"],
        "scores": broad_fair["scores"],
        "breadth": broad_fair["breadth"],
        "diagnostics": broad_diagnostics,
    }
    broad_available = bool(broad.get("available"))
    if not broad_available and not available_sectors:
        raise RuntimeError(f"{as_of.strftime('%Y-%m-%d')} 没有可用于评分的公开社区文本")

    market_scores: Dict[str, int] = {}
    for key in ("baoma", "novice", "retail"):
        value = (
            0.40 * float(broad["scores"][key] if broad_available else 50.0)
            + 0.50 * float(sector_scores[key] if sector_scores else 50.0)
            + 0.10 * float(breadth_score if breadth_score is not None else 50.0)
        )
        market_scores[key] = int(round(clamp(value)))
    market_scores["composite"] = int(round(sum(market_scores.values()) / 3.0))

    market_signals: Dict[str, float] = {}
    for key in (*SIGNAL_KEYS, "panic"):
        baseline = FAIR_SIGNAL_BASELINES.get(key, 50.0)
        value = (
            0.40 * float(broad["signals"].get(key, baseline) if broad_available else baseline)
            + 0.60 * float(sector_signals.get(key, baseline) if sector_signals else baseline)
        )
        market_signals[key] = round1(value)
    market_signals["breadth"] = round1(
        breadth_score if breadth_score is not None else broad.get("breadth", 50.0)
    )
    heat_value = (
        0.40 * float(broad["signals"].get("heat", 50.0) if broad_available else 50.0)
        + 0.50 * float(sector_signals.get("heat", 50.0) if sector_signals else 50.0)
        + 0.10 * float(breadth_score if breadth_score is not None else 50.0)
    )
    market_signals["heat"] = round1(heat_value)

    # 正式三类指数与市场总热度保持原口径；板块页面热度另做一次最终截面
    # 校准，把“全市场整体冷热”和“同日板块相对声量”分开后再合成。
    sector_heat_audit = calibrate_sector_heats(sector_indices, market_signals["heat"])

    sector_indices.sort(
        key=lambda item: (
            bool(item.get("metrics", {}).get("fairness", {}).get("rankEligible")),
            bool(item["available"]),
            item["scores"]["composite"],
        ),
        reverse=True,
    )
    return {
        "scores": market_scores,
        "signals": market_signals,
        "sectorIndices": sector_indices,
        "broad": broad,
        "availableSourceKeys": sorted(visible_source_keys),
        "availableSourceCount": len(visible_source_keys),
        "availablePlatformCount": len(visible_platforms),
        "availableSectorCount": len(available_sectors),
        "availableParentSectorCount": sum(bool(item.get("available")) for item in parent_rows),
        "parentSectorSummary": parent_rows,
        "availableFreshSourceCount": sum(
            1 for key in visible_source_keys if bool(results[key].get("fresh"))
        ),
        "sectorHeatAudit": sector_heat_audit,
    }





# ---------------- sqlite归档 ----------------

def archive_to_sqlite(results: Mapping[str, Mapping[str, object]], db_path: Path = SQLITE_ARCHIVE_PATH) -> Dict[str, int]:
    """发帖快照进sqlite，按(平台,源,帖子ID,发布时间)去重。只追加不修改历史。"""
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts(
                platform TEXT NOT NULL,
                source_key TEXT NOT NULL,
                post_id TEXT NOT NULL,
                source_name TEXT,
                published_at TEXT,
                observed_at TEXT,
                title TEXT,
                author_hash TEXT,
                kind TEXT,
                PRIMARY KEY(platform, source_key, post_id, published_at)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_published ON posts(published_at)"
        )
        now_iso = datetime.now(CN_TZ).isoformat()
        added = 0
        for key, source in results.items():
            platform = str(source.get("platform") or key.split(":", 1)[0])
            source_key = str(key)
            source_name = str(source.get("name") or "")
            for post in source.get("posts", []):
                if not isinstance(post, Mapping):
                    continue
                published = parse_time(post.get("publishTime") or post.get("published"))
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO posts(
                        platform, source_key, post_id, source_name,
                        published_at, observed_at, title, author_hash, kind
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        platform,
                        source_key,
                        str(post.get("id") or ""),
                        source_name,
                        published.isoformat() if published else "",
                        now_iso,
                        str(post.get("title") or "")[:500],
                        str(post.get("authorKey") or ""),
                        str(post.get("kind") or "post"),
                    ),
                )
                added += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        return {"added": added, "total": total}
    finally:
        conn.close()


# ---------------- 采集调度 ----------------

def fetch_source(spec: Mapping[str, object], as_of: datetime = NOW) -> Dict[str, object]:
    platform = str(spec["platform"])
    fetcher = {
        "eastmoney": fetch_eastmoney,
        "sina": fetch_sina,
        "ths": fetch_ths,
        "taoguba": fetch_taoguba,
    }.get(platform)
    if fetcher is None:
        raise RuntimeError(f"未知平台：{platform}")
    return fetcher(spec, as_of)


def collect_sources(
    specs: Mapping[str, Mapping[str, object]],
    cached: Mapping[str, Mapping[str, object]],
    on_source: "Optional[Callable[[Dict[str, object]], None]]" = None,
) -> Tuple[Dict[str, Dict[str, object]], List[str]]:
    """单共享池+每平台信号量：平台间并行、单站并发仍受10/5/3/6上限约束，
    池子不嵌套。45分钟内已成功的来源直接续用，只补失败项；每平台完成即落盘。"""

    fresh_results: Dict[str, Dict[str, object]] = {
        key: {**value, "fresh": True, "resumedFromCache": True}
        for key, value in cached.items()
        if key in specs and cache_is_recent(value, CACHE_RESUME_MINUTES)
    }
    failures: List[str] = []
    total_sources = len(specs)
    resumed_count = len(fresh_results)
    print(f"需要读取 {total_sources} 个公开社区来源（4个平台）……", flush=True)
    if fresh_results:
        print(f"已续用本轮刚完成的 {len(fresh_results)} 个来源，只补读取其余来源。", flush=True)

    from collectors import PLATFORM_WORKERS
    semaphores = {platform: threading.Semaphore(limit) for platform, limit in PLATFORM_WORKERS.items()}
    todo = [spec for key, spec in specs.items() if key not in fresh_results]
    counters = {platform: [0, 0] for platform in PLATFORM_WORKERS}  # [完成数, 平台总数]

    def run_one(spec: Mapping[str, object]) -> Tuple[str, object, object]:
        platform = str(spec["platform"])
        with semaphores[platform]:
            try:
                return str(spec["key"]), spec, fetch_source(spec)
            except Exception as exc:  # 单源失败计入failures，交给写入门槛决定，不炸整轮
                return str(spec["key"]), spec, exc

    max_workers = sum(PLATFORM_WORKERS.values())
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_one, spec) for spec in todo]
        for future in as_completed(futures):
            key, spec, result = future.result()
            platform = str(spec["platform"])
            counters[platform][0] += 1
            counters[platform][1] += 1
            if isinstance(result, Exception):
                failures.append(f"{spec['name']}[{platform}]: {result}")
            else:
                if platform == "taoguba":
                    result = accumulate_taoguba_snapshots(result, cached.get(key))
                result = {**result, "fresh": True}
                fresh_results[key] = result
            done = sum(c[0] for c in counters.values())
            print(f"{PLATFORM_NAMES.get(platform, platform)} {counters[platform][0]}/{len([s for s in todo if s['platform'] == platform])}（总计 {done}/{len(todo)}）", flush=True)
            if on_source:
                try:
                    on_source({
                        "type": "progress",
                        "key": key,
                        "platform": PLATFORM_NAMES.get(platform, platform),
                        "done": resumed_count + done,
                        "total": total_sources,
                    })
                except Exception:
                    pass  # 表现层钩子故障不影响采集主流程
            # 每平台结束即落盘：某站临时变慢或进程中断时，其余完成项可续用
            save_forum_cache({**{k: v for k, v in cached.items() if cache_is_usable(v)}, **fresh_results})
            if platform == "sina":
                save_sina_bids()
            if platform == "ths":
                save_ths_config()
    return fresh_results, failures



def _emit_progress(
    callback: "Optional[Callable[[Dict[str, object]], None]]",
    payload: Dict[str, object],
) -> None:
    """表现层钩子：回调故障一律吞掉，绝不影响采集与写盘主流程。"""
    if not callback:
        return
    try:
        callback(payload)
    except Exception:
        pass


def _market_unit_for_key(key: str, results) -> str:
    """大盘层席位映射：新浪/淘股吧大盘源归入 broad-market 固定席位。"""
    if key.startswith(("sina:", "taoguba:")):
        return "broad-market"
    return str(
        results[key].get("code")
        or (key.split(":", 1)[-1] if key.startswith("eastmoney:") else key.rsplit(":", 1)[-1])
    )


def build_snapshot(
    progress_callback: "Optional[Callable[[Dict[str, object]], None]]" = None,
) -> Dict[str, object]:
    """progress_callback 为纯表现层钩子：progress 事件每源一条，state 事件 run/ok/bad。
    不传时与旧行为完全一致。"""
    try:
        snapshot = _build_snapshot_core(progress_callback)
    except Exception as exc:
        _emit_progress(progress_callback, {"type": "state", "state": "bad", "error": str(exc)})
        raise
    scores = snapshot.get("scores") or {}
    diag = snapshot.get("diagnostics") or {}
    _emit_progress(progress_callback, {
        "type": "state", "state": "ok",
        "updatedAt": snapshot.get("updatedAt"),
        "composite": scores.get("composite"),
        "fresh": diag.get("freshSourceCount"),
        "total": diag.get("sourceCount"),
        "failures": len(snapshot.get("failures") or []),
    })
    return snapshot


def _build_snapshot_core(
    progress_callback: "Optional[Callable[[Dict[str, object]], None]]" = None,
) -> Dict[str, object]:
    market_day = scoring_market_day(NOW)
    specs, market_keys, sector_source_keys = build_source_specs()
    cached = load_forum_cache()

    _emit_progress(progress_callback, {"type": "state", "state": "run", "total": len(specs)})
    fresh_results, failures = collect_sources(specs, cached, on_source=progress_callback)

    fresh_platforms = {str(result.get("platform")) for result in fresh_results.values() if result.get("posts")}
    minimum_fresh = max(20, int(len(specs) * MIN_FRESH_SOURCE_RATIO))
    if len(fresh_results) < minimum_fresh or len(fresh_platforms) < MIN_FRESH_PLATFORMS:
        # 写入门槛：不满足则整体失败，main()不触碰任何输出文件，上一次成功结果原样保留
        raise RuntimeError(
            f"新鲜社区源仅成功 {len(fresh_results)}/{len(specs)}、{len(fresh_platforms)} 个平台；已保留上一次成功结果"
        )

    results = dict(fresh_results)
    cached_used = 0
    for key in specs:
        if key not in results and key in cached and cache_is_usable(cached[key]):
            results[key] = {**cached[key], "fresh": False, "staleReason": "本次读取失败，使用72小时内缓存"}
            cached_used += 1
    save_forum_cache(results)
    save_sina_bids()
    save_ths_config()

    try:
        quality_report = build_quality_report(results)
    except Exception as exc:  # K1：质量诊断故障不得拖垮主更新
        quality_report = {"summary": {"error": str(exc)}, "sources": {}}
    for _line in format_console_lines(quality_report):
        print(_line, flush=True)

    scored = score_results_as_of(results, market_keys, sector_source_keys, as_of=NOW, record_type=MEASURED)
    market_scores = scored["scores"]
    market_signals = scored["signals"]
    sector_indices = scored["sectorIndices"]
    broad = scored["broad"]
    history = prepare_history()
    date_key = market_day.isoformat()
    prior_points = [
        point for point in history
        if isinstance(point, Mapping)
        and str(point.get("date") or "") < date_key
        and isinstance(point.get("sectors"), list)
    ]
    if prior_points:
        prior_point = max(prior_points, key=lambda point: str(point.get("date") or ""))
        try:
            temporal_gap_days = trading_day_gap(
                datetime.strptime(str(prior_point.get("date")), "%Y-%m-%d").date(),
                market_day,
            )
        except ValueError:
            temporal_gap_days = 1
        temporal_audit = stabilize_sector_heats(
            sector_indices,
            prior_point.get("sectors", []),
            market_signals["heat"],
            gap_days=temporal_gap_days,
        )
        scored["sectorHeatAudit"] = {**dict(scored.get("sectorHeatAudit") or {}), **temporal_audit}

    archive_stats = archive_results(results, COMMUNITY_ARCHIVE_DIR, collected_at=NOW)
    sqlite_stats = archive_to_sqlite(results)
    all_sample_posts = broad["diagnostics"]["samplePosts"] + sum(
        int(item["metrics"]["samplePosts"]) for item in sector_indices
    )
    history_point = build_history_point(
        as_of=market_day,
        scores=market_scores,
        signals=market_signals,
        sectors=sector_indices,
        platform_count=int(scored["availablePlatformCount"]),
        expected_platform_count=len(PLATFORM_NAMES),
        source_count=int(scored["availableSourceCount"]),
        expected_source_count=len(specs),
        sector_count=int(scored["availableSectorCount"]),
        expected_sector_count=len(SECTORS),
        sample_posts=all_sample_posts,
        record_type=MEASURED,
        fresh_source_count=int(scored["availableFreshSourceCount"]),
        captured_at=NOW,
        methodology="community_text_four_channel_grassroots_three_trading_day_v3",
    )
    history = upsert_history(history, history_point)
    history_meta = build_history_meta(history, updated_at=NOW)
    previous_meta = load_json(HISTORY_META_PATH, {})
    if (
        isinstance(previous_meta, dict)
        and isinstance(previous_meta.get("retrospectiveArchive"), dict)
        and any(record_type_of(item) == ESTIMATED for item in history if isinstance(item, Mapping))
    ):
        history_meta["retrospectiveArchive"] = previous_meta["retrospectiveArchive"]

    all_sector_samples: List[Dict[str, object]] = []
    for item in sector_indices:
        all_sector_samples.extend(item["samplePosts"][:2])
    community_samples = broad["samplePosts"] + all_sector_samples
    community_samples.sort(key=lambda item: str(item.get("publishTime") or ""), reverse=True)
    platform_counts = {
        platform: sum(1 for result in results.values() if result.get("platform") == platform)
        for platform in PLATFORM_NAMES
    }
    confidence = int(history_point["confidence"])
    diagnostics = {
        **broad["diagnostics"],
        "marketDate": date_key,
        "dataQuality": quality_report,
        "scoringTradingDays": [day.isoformat() for day in scoring_trading_days(NOW)],
        "tradingDayWeights": list(TRADING_DAY_WEIGHTS),
        "calendarMode": calendar_mode(market_day),
        "sectorSamplePosts": sum(int(item["metrics"]["samplePosts"]) for item in sector_indices),
        "allSamplePosts": all_sample_posts,
        "platformCount": int(scored["availablePlatformCount"]),
        "sourceCount": int(scored["availableSourceCount"]),
        "freshSourceCount": int(scored["availableFreshSourceCount"]),
        "cachedSourceCount": cached_used,
        "collectedSourceCount": len(results),
        "configuredSourceCount": len(specs),
        "availableSectorCount": int(scored["availableSectorCount"]),
        "availableParentSectorCount": int(scored["availableParentSectorCount"]),
        "configuredSectorCount": len(SECTORS),
        "configuredParentSectorCount": len(PARENT_SECTORS),
        "sectorCatalogVersion": SECTOR_CATALOG_VERSION,
        "sectorCatalogId": SECTOR_CATALOG_ID,
        "sectorSourcePlan": {
            "eastmoneyStocksPerSector": 3,
            "sinaStocksPerSector": 3,
            "taogubaStocksPerSector": 3,
            "thsCommunitiesPerSector": "1-2",
            "thsRelatedScenesDiscovered": True,
            "broadIndustryForumsUsed": False,
            "representativeCompaniesPerSector": 3,
        },
        "parentSectorSummary": scored.get("parentSectorSummary", []),
        "coverage": history_point["coverage"],
        "archiveAddedPosts": archive_stats["added"],
        "archiveExistingPosts": archive_stats["existing"],
        "sqliteArchiveAddedPosts": sqlite_stats["added"],
        "sqliteArchiveTotalPosts": sqlite_stats["total"],
        "sectorHeatModel": scored.get("sectorHeatAudit", {}),
        "fairnessModelVersion": FAIRNESS_MODEL_VERSION,
        "rankEligibleSectorCount": sum(
            bool(item.get("metrics", {}).get("fairness", {}).get("rankEligible"))
            for item in sector_indices
        ),
        "lowConfidenceSectorCount": sum(
            item.get("metrics", {}).get("fairness", {}).get("confidenceLabel") == "低"
            for item in sector_indices
        ),
        "companyEqualWeighting": True,
        "channelEqualWeighting": True,
        "fixedChannelTargetWeight": 0.25,
        "grassrootsStyleWeighting": True,
        "missingEvidenceNeutralised": True,
        "thsExcludedFromFormalVolume": True,
        "chronologicalCoverageSourceCount": sum(
            bool(result.get("chronological")) and bool(result.get("thirdTradingDayReached"))
            for result in results.values()
        ),
        "hotSectorCount": sum(
            1 for item in sector_indices
            if item["available"] and float(item["signals"]["heat"]) >= 50.0
        ),
    }
    return {
        "version": VERSION,
        "sectorCatalogVersion": SECTOR_CATALOG_VERSION,
        "sectorCatalogId": SECTOR_CATALOG_ID,
        "methodology": "community_text_four_channel_grassroots_three_trading_day_v3",
        "scoringModel": "four_channels_equal_then_units_equal_grassroots_50_30_20_v3",
        "marketDate": date_key,
        "updatedAt": NOW.isoformat(),
        "confidence": confidence,
        "scores": market_scores,
        "signals": market_signals,
        "heat": market_signals["heat"],
        "direction": market_signals["optimism"],
        "diagnostics": diagnostics,
        "community": {
            "sources": [source_public_info(results[key]) for key in market_keys if key in results],
            "samplePosts": community_samples[:12],
            "platforms": [
                {"key": key, "name": PLATFORM_NAMES[key], "sourceCount": platform_counts[key]}
                for key in PLATFORM_NAMES
            ],
        },
        "sectorIndices": sector_indices,
        "history": history,
        "historyMeta": history_meta,
        "sources": {
            "community": "东方财富股吧 · 新浪股吧 · 同花顺板块社区/关联社区 · 淘股吧公开个股讨论",
            "window": "仅最近3个A股交易日文本；先在每个渠道内部按固定单位席位聚合，再以最近50%/上一交易日30%/上两个交易日20%合成；缺失渠道或单位保持中性且不重新分权",
            "tradingCalendar": f"A股交易日历（{calendar_mode(market_day)}）；周末与休市帖子不计分，非交易日更新归属最近交易日",
            "marketFormula": "固定席位为大盘社区40% + 板块社区50% + 热度扩散面10%；细分板块在大类内等权、8个大类再等权；缺失席位保持中性并降低置信度，不把权重转送给其余来源",
            "marketPriceUsed": False,
            "rqUsed": False,
            "xueqiuUsed": False,
            "xueqiuNote": "雪球匿名自动访问受登录与官方抓取规则限制，仅保留人工核验，不自动计分",
            "platformCalibration": "每个平台、每个交易日单独做横截面校准；情绪保留70%—75%绝对文本含义，关注度只比较同平台当日分位，避免网页容量制造热度",
            "channelFairness": "东方财富、新浪、同花顺、淘股吧固定各占25%；样本数只通过n/(n+12)决定该席位向中性收缩多少，不制造额外投票权；缺失席位不转让",
            "companyFairness": "东方财富、新浪、淘股吧各自在三家典型公司内固定等权；同花顺按明确配置的板块社区固定等权，概念页和关联场景先合成同花顺这一票",
            "grassrootsVoice": "只判断表达形态，不推断用户智力或真实身份：非认证普通评论、第一人称持仓感受和口语提问保持高权重；机构/官方账号剔除，长篇投研、多股票推广和专业模板对称降权",
            "sectorHeat": "板块主热度由固定渠道的关注度与情绪温度形成，再做同日稳健横截面定位；达到渠道覆盖、有效样本和留一渠道稳定性门槛才给正式名次",
            "sectorActivity": "关注度中发帖声量占85%、独立参与广度占15%；东方财富、新浪与淘股吧的可见时序样本参与，其中淘股吧标为20条匿名页下限；同花顺热门排序流只参与文本情绪，不参与发帖量和关注度",
            "sectorCatalog": f"细分板块目录 {SECTOR_CATALOG_ID}（版本{SECTOR_CATALOG_VERSION}）：30个细分板块、8个大类，每板块展示3家唯一主归类的典型公司",
            "sectorSourcePlan": f"共配置{len(specs)}个公开社区入口；每个细分板块读取三家公司的东方财富、新浪、淘股吧社区，并读取1—2个同花顺板块社区及其关联场景；所有入口先在所属渠道内合并，不会把入口数变成额外渠道票",
            "representativeCompanyNote": "典型公司仅用于解释板块边界与定位论坛，不是指数成份股或投资推荐",
        },
        "failures": failures,
    }


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_log("开始四平台固定渠道社区文本版更新")
    try:
        snapshot = build_snapshot()
        # data.js 是页面真正读取的提交点，最后替换；中途失败则页面继续显示上一份完整快照
        save_json(HISTORY_PATH, snapshot["history"])
        save_json(HISTORY_META_PATH, snapshot["historyMeta"])
        save_json(LATEST_PATH, snapshot)
        save_text(DATA_JS_PATH, "window.INDEX_DATA = " + json.dumps(snapshot, ensure_ascii=False, indent=2) + ";\n")
        write_log(
            f"更新完成：综合{snapshot['scores']['composite']}，热度{snapshot['heat']}，"
            f"新鲜社区源{snapshot['diagnostics']['freshSourceCount']}/{snapshot['diagnostics']['sourceCount']}，"
            f"失败{len(snapshot['failures'])}个"
        )
        print("\n更新完成。", flush=True)
        print(
            f"综合 {snapshot['scores']['composite']} · 宝妈 {snapshot['scores']['baoma']} · "
            f"小白 {snapshot['scores']['novice']} · 散户 {snapshot['scores']['retail']}",
            flush=True,
        )
        print(
            f"社区热度 {snapshot['heat']} · 情绪方向 {snapshot['direction']} · "
            f"四平台共分析 {snapshot['diagnostics']['allSamplePosts']} 条文本样本。",
            flush=True,
        )
        if snapshot["failures"]:
            print(f"有 {len(snapshot['failures'])} 个来源暂不可用，其余来源及72小时缓存已正常参与。", flush=True)
        return 0
    except Exception as exc:
        detail = traceback.format_exc()
        write_log(f"更新失败：{type(exc).__name__}: {exc}\n{detail}")
        print(f"\n更新失败：{exc}", file=sys.stderr, flush=True)
        print(detail, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
