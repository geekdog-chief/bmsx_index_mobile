"""四平台公开社区采集层。每平台低并发上限（东财10/新浪5/同花顺3/淘股吧6），
平台之间并行，池子不嵌套。只读匿名公开接口，不绕过登录/验证码/风控。

平台特殊规则：
* 同花顺：草根内容权重高；认证/勋章/官方账号清零剔除；概念区+关联社区
  （从论坛元数据 related_scenes 发现）合并抓取；每作者每日1帖；非时序热门流；
  情绪权重封顶15%（在公平层执行）；attention与volume禁用
* 淘股吧：匿名首页20行下限快照，认证用户剔除，volume标记为下限
* 全平台：作者去重、跨平台同文本去重（评分层）、营销号过滤（sentiment层）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from sentiment import AUTHOR_BLOCK_WORDS, CN_TZ, clean_text, normalize_text, parse_time, safe_int, valid_text

ROOT = Path(__file__).resolve().parent
SINA_BID_CACHE_PATH = ROOT / "data" / "sina_bid_cache.json"
THS_CONFIG_CACHE_PATH = ROOT / "data" / "ths_forum_cache.json"

PLATFORM_NAMES = {
    "eastmoney": "东方财富股吧",
    "sina": "新浪股吧",
    "ths": "同花顺板块社区",
    "taoguba": "淘股吧",
}
# 每平台并发上限：平台间并行、平台内限流，总并发可控
PLATFORM_WORKERS = {"eastmoney": 10, "sina": 5, "ths": 3, "taoguba": 6}
COLLECTOR_PAGE_LIMIT = 3
EASTMONEY_PAGE_SIZE = 100
SINA_PAGE_SIZE = 50
TAOGUBA_PAGE_SIZE = 20
THS_CURSOR_PAGE_LIMIT = 1
TAOGUBA_AUTHOR_DAILY_CAP = 2  # 淘股吧匿名页内每作者每日上限
THS_API_BASE = None

def request_text(url: str, platform: str, attempts: int = 2, timeout: int = 12) -> str:
    referers = {
        "eastmoney": "https://guba.eastmoney.com/",
        "sina": "https://guba.sina.cn/",
        "ths": "https://t.10jqka.com.cn/",
        "taoguba": "https://www.tgb.cn/quotes/",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Referer": referers.get(platform, "https://www.baidu.com/"),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    last_error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    return body.decode(charset)
                except (LookupError, UnicodeDecodeError):
                    return body.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.2 * (2 ** attempt))
    raise RuntimeError(f"读取失败：{url} ({last_error})")


def request_json(url: str, platform: str, attempts: int = 2, timeout: int = 12) -> Dict:
    try:
        payload = json.loads(request_text(url, platform, attempts=attempts, timeout=timeout))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{PLATFORM_NAMES.get(platform, platform)} 返回了非 JSON 内容") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{PLATFORM_NAMES.get(platform, platform)} 返回结构异常")
    return payload


def make_post(*, post_id, text, published, comments, clicks, likes,
              source_key, source_code, source_name, platform, author_key="",
              kind="post", time_inherited=False, parent_post_id="") -> Dict:
    post = {
        "id": f"{platform}:{post_id}",
        "title": text,
        "published": published,
        "publishTime": published.isoformat(),
        "comments": max(0, safe_int(comments)),
        "clicks": max(0, safe_int(clicks)),
        "likes": max(0, safe_int(likes)),
        "sourceKey": source_key,
        "sourceCode": source_code,
        "source": source_name,
        "platform": platform,
        "platformName": PLATFORM_NAMES[platform],
        "authorKey": str(author_key or ""),
        "kind": kind,
        "timeInherited": bool(time_inherited),
    }
    if parent_post_id:
        post["parentPostId"] = str(parent_post_id)
    return post


def author_daily_admit(counts: Dict[Tuple[str, str], int], author_key: object, published: datetime, *, limit: int = 3) -> bool:
    """作者限额按 作者×发布日 计，防止高产作者吃掉全部配额或跨日错杀。"""
    local = published.replace(tzinfo=CN_TZ) if published.tzinfo is None else published.astimezone(CN_TZ)
    bucket = (str(author_key or "unknown"), local.date().isoformat())
    if counts[bucket] >= max(1, int(limit)):
        return False
    counts[bucket] += 1
    return True


AUTHOR_BLOCK_CAP = 3  # 作者每日3帖（sentiment.AUTHOR_DAILY_LIMIT同值，采集层先执行）


def collection_metadata(*, raw_times, posts, raw_rows, page_count, cap_hit,
                        chronological, fresh_fetched_now, fetched_at=None, third_day_reached=None,
                        now=None) -> Dict:
    observed = []
    for value in raw_times:
        if isinstance(value, datetime):
            observed.append(value.replace(tzinfo=CN_TZ) if value.tzinfo is None else value.astimezone(CN_TZ))
    if not observed:
        for post in posts:
            value = post.get("published")
            if isinstance(value, datetime):
                observed.append(value)
    fetched = parse_time(fetched_at) or datetime.now(CN_TZ)
    return {
        "rawRows": max(0, int(raw_rows)),
        "pageCount": max(0, int(page_count)),
        "oldestSeen": min(observed).isoformat() if observed else None,
        "newestSeen": max(observed).isoformat() if observed else None,
        "capHit": cap_hit,
        "chronological": bool(chronological),
        "coverageDays": sorted({item.date().isoformat() for item in observed}, reverse=True),
        "thirdTradingDayReached": bool(third_day_reached) if third_day_reached is not None else False,
        "freshFetchedNow": bool(fresh_fetched_now),
        "cacheAge": max(0, int(((now if now is not None else datetime.now(CN_TZ)) - fetched).total_seconds())),
        "cacheAgeUnit": "seconds",
    }


def third_scoring_day(as_of: datetime):
    from update_index import scoring_market_day, scoring_trading_days
    days = scoring_trading_days(as_of)
    return days[-1] if days else scoring_market_day(as_of)


def page_reaches_third_scoring_day(times: Sequence[datetime], as_of: datetime) -> bool:
    """用时序尾部判断，避免置顶旧帖阻断翻页。命中≥3条或占尾部多数即算到达。"""
    if not times:
        return False
    boundary = third_scoring_day(as_of)
    tail = list(times[-min(10, len(times)):])
    reached = sum(
        1 for item in tail
        if (item.replace(tzinfo=CN_TZ) if item.tzinfo is None else item.astimezone(CN_TZ)).date() <= boundary
    )
    return reached >= min(3, len(tail))


def reached_third_scoring_day(times: Sequence[datetime], as_of: datetime) -> bool:
    boundary = third_scoring_day(as_of)
    return any(
        (item.replace(tzinfo=CN_TZ) if item.tzinfo is None else item.astimezone(CN_TZ)).date() <= boundary
        for item in times
    )


# ---------------- 东方财富 ----------------

def fetch_eastmoney(spec: Mapping, as_of: datetime = None) -> Dict:
    as_of = as_of or datetime.now(CN_TZ)
    code = str(spec["code"])
    label = str(spec["name"])
    source_key = str(spec["key"])
    page_url = f"https://guba.eastmoney.com/list,{code}.html"
    all_rows: List[Dict] = []
    raw_times: List[datetime] = []
    raw_rows = 0
    page_count = 0
    cap_hit = False
    coverage_status = "unknown"
    pagination_mode = "page"
    target_reached = False

    for page_number in range(1, COLLECTOR_PAGE_LIMIT + 1):
        payload = None
        last_error = None
        query = urllib.parse.urlencode({
            "code": code, "sorttype": 1, "ps": EASTMONEY_PAGE_SIZE, "p": page_number,
            "from": "CommonBaPost",
            "deviceid": "2f7f40de-2fb0-4d84-8a31-111111111111",
            "version": 200, "product": "Guba", "plat": "Web",
        })
        api_url = "https://gbapi.eastmoney.com/webarticlelist/api/Article/Articlelist?" + query
        for attempt in range(3):
            try:
                candidate = request_json(api_url, "eastmoney", attempts=1, timeout=10)
                message = str(candidate.get("me") or "").strip()
                rows_value = candidate.get("re")
                if message == "到底啦":
                    payload = {**candidate, "re": rows_value if isinstance(rows_value, list) else []}
                    break
                if not isinstance(rows_value, list):
                    raise RuntimeError(str(candidate.get("me") or "帖子列表为空"))
                if message not in ("", "操作成功"):
                    raise RuntimeError(message)
                payload = candidate
                break
            except RuntimeError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.0 + attempt)

        if payload is None:
            # 公开HTML兜底只有一页，显式标记完整性未知
            if page_number > 1:
                raise RuntimeError(str(last_error or f"{label} 第 {page_number} 页读取失败"))
            page = request_text(page_url, "eastmoney", attempts=1, timeout=10)
            if "身份核实" in page:
                raise RuntimeError(f"{label} 触发站点临时身份核实")
            matched = re.search(r"var\s+article_list\s*=\s*(\{.*?\});", page, re.DOTALL)
            if not matched:
                raise RuntimeError(f"{label} 页面没有公开帖子列表")
            payload = json.loads(matched.group(1))
            pagination_mode = "html_single_page_fallback"
            cap_hit = None

        rows = payload.get("re") if isinstance(payload.get("re"), list) else []
        page_count += 1
        raw_rows += len(rows)
        page_times: List[datetime] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            all_rows.append(row)
            observed = parse_time(row.get("post_publish_time"))
            if observed is not None:
                raw_times.append(observed)
                page_times.append(observed)

        reached_target = page_reaches_third_scoring_day(page_times, as_of)
        total_count = safe_int(payload.get("count"), 0)
        source_exhausted = (
            not rows or len(rows) < EASTMONEY_PAGE_SIZE
            or (total_count > 0 and total_count <= page_number * EASTMONEY_PAGE_SIZE)
        )
        if reached_target:
            target_reached = True
            coverage_status = "third_trading_day_reached"
            break
        if pagination_mode != "page":
            coverage_status = "single_page_fallback_unknown"
            break
        if source_exhausted:
            coverage_status = "source_exhausted_before_third_trading_day"
            break
        if page_number == COLLECTOR_PAGE_LIMIT:
            cap_hit = True
            coverage_status = "page_cap_reached_before_third_trading_day"

    posts: List[Dict] = []
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    seen_ids = set()
    for row in all_rows:
        if safe_int(row.get("post_type"), -1) != 0:
            continue
        nickname = str(row.get("user_nickname") or "").strip()
        if not nickname or str(row.get("post_source_id") or "").strip():
            continue
        if any(word in nickname for word in AUTHOR_BLOCK_WORDS):
            continue
        user_key = str(row.get("user_id") or nickname)
        text = clean_text(row.get("post_title"))
        published = parse_time(row.get("post_publish_time"))
        if not valid_text(text) or published is None:
            continue
        post_id = str(row.get("post_id") or normalize_text(text))
        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        if not author_daily_admit(author_day_counts, user_key, published):
            continue
        posts.append(make_post(
            post_id=post_id, text=text, published=published,
            comments=row.get("post_comment_count"), clicks=row.get("post_click_count"),
            likes=0, source_key=source_key, source_code=code, source_name=label,
            platform="eastmoney", author_key=user_key,
        ))
    return {
        "key": source_key, "code": code, "name": label, "platform": "eastmoney",
        "url": page_url, "posts": posts, "fetchedAt": datetime.now(CN_TZ).isoformat(),
        "fresh": True,
        **collection_metadata(raw_times=raw_times, posts=posts, raw_rows=raw_rows,
                              page_count=page_count, cap_hit=cap_hit, chronological=True,
                              fresh_fetched_now=True, third_day_reached=target_reached),
        "coverageStatus": coverage_status, "paginationMode": pagination_mode,
        "rawRowsEstimated": False,
    }


# ---------------- 新浪 ----------------

_SINA_BIDS: Dict[str, object] = {}

def load_sina_bids() -> Dict:
    global _SINA_BIDS
    if not _SINA_BIDS:
        try:
            _SINA_BIDS = dict(json.loads(SINA_BID_CACHE_PATH.read_text(encoding="utf-8")) or {})
        except (OSError, json.JSONDecodeError):
            _SINA_BIDS = {}
    return _SINA_BIDS


def save_sina_bids() -> None:
    SINA_BID_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SINA_BID_CACHE_PATH.with_name(SINA_BID_CACHE_PATH.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(_SINA_BIDS, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SINA_BID_CACHE_PATH)


def sina_symbol(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def discover_sina_bid(code: str, symbol: str = None) -> str:
    bids = load_sina_bids()
    cached = bids.get(code)
    if cached:
        return str(cached)
    symbol = symbol or sina_symbol(code)
    page_url = f"https://guba.sina.cn/?s=bar&name={symbol}&from=redirect"
    page = request_text(page_url, "sina", attempts=1, timeout=12)
    for pattern in (r'["\']bid["\']\s*:\s*["\']?(\d+)', r"\bbid\s*[:=]\s*['\"]?(\d+)", r"data-bid=['\"](\d+)"):
        matched = re.search(pattern, page, re.I)
        if matched:
            bids[code] = matched.group(1)
            return matched.group(1)
    raise RuntimeError(f"新浪股吧未找到 {code} 的公开论坛编号")


def _truthy(value: object) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes"}


def fetch_sina(spec: Mapping, as_of: datetime = None) -> Dict:
    as_of = as_of or datetime.now(CN_TZ)
    code = str(spec["code"])
    stock_name = str(spec["name"])
    label = f"{stock_name}新浪股吧"
    source_key = str(spec["key"])
    symbol = str(spec.get("symbol") or sina_symbol(code))
    page_url = f"https://guba.sina.cn/?s=bar&name={symbol}&from=redirect"
    bid = discover_sina_bid(code, symbol)
    all_rows: List[Dict] = []
    raw_times: List[datetime] = []
    raw_rows = 0
    page_count = 0
    cap_hit = False
    coverage_status = "unknown"
    target_reached = False
    cursor_value = ""
    cursor_tid = ""
    seen_cursors = set()

    for page_number in range(1, COLLECTOR_PAGE_LIMIT + 1):
        query: Dict[str, object] = {"s": "h5bar", "bid": bid, "num": SINA_PAGE_SIZE}
        if cursor_value and cursor_tid:
            query["relate_value"] = cursor_value
            query["relate_tid"] = cursor_tid
        api_url = "https://guba.sina.cn/api/?" + urllib.parse.urlencode(query)
        payload = request_json(api_url, "sina", attempts=2, timeout=12)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = data.get("threads") if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError(f"{label} 返回结构异常")
        page_count += 1
        raw_rows += len(rows)
        page_times: List[datetime] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            all_rows.append(row)
            observed = parse_time(row.get("timestamp") or row.get("lastctime") or row.get("ctime"))
            if observed is not None:
                raw_times.append(observed)
                page_times.append(observed)

        if page_reaches_third_scoring_day(page_times, as_of):
            target_reached = True
            coverage_status = "third_trading_day_reached"
            break
        if len(rows) < SINA_PAGE_SIZE or not rows:
            coverage_status = "source_exhausted_before_third_trading_day"
            break
        last_row = rows[-1] if isinstance(rows[-1], dict) else {}
        next_cursor = (str(last_row.get("timestamp") or ""), str(last_row.get("tid") or ""))
        if not all(next_cursor) or next_cursor in seen_cursors or next_cursor == (cursor_value, cursor_tid):
            cap_hit = None
            coverage_status = "cursor_stalled_before_third_trading_day"
            break
        seen_cursors.add(next_cursor)
        cursor_value, cursor_tid = next_cursor
        if page_number == COLLECTOR_PAGE_LIMIT:
            cap_hit = True
            coverage_status = "page_cap_reached_before_third_trading_day"

    posts: List[Dict] = []
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    seen_ids = set()
    for row in all_rows:
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        if _truthy(user.get("verified")):
            continue
        nickname = str(user.get("nick") or user.get("name") or "")
        if any(word in nickname for word in AUTHOR_BLOCK_WORDS):
            continue
        user_key = str(user.get("uid") or nickname or row.get("tid") or "unknown")
        text = clean_text(row.get("title") or row.get("content") or row.get("long_content"))
        published = parse_time(row.get("timestamp") or row.get("lastctime") or row.get("ctime"))
        if not valid_text(text) or published is None:
            continue
        post_id = str(row.get("tid") or normalize_text(text))
        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        if not author_daily_admit(author_day_counts, user_key, published):
            continue
        posts.append(make_post(
            post_id=post_id, text=text, published=published,
            comments=row.get("reply"), clicks=row.get("views"), likes=row.get("like"),
            source_key=source_key, source_code=code, source_name=label,
            platform="sina", author_key=user_key,
        ))
    return {
        "key": source_key, "code": code, "name": label, "platform": "sina",
        "url": page_url, "posts": posts, "fetchedAt": datetime.now(CN_TZ).isoformat(),
        "fresh": True,
        **collection_metadata(raw_times=raw_times, posts=posts, raw_rows=raw_rows,
                              page_count=page_count, cap_hit=cap_hit, chronological=True,
                              fresh_fetched_now=True, third_day_reached=target_reached),
        "coverageStatus": coverage_status, "paginationMode": "cursor",
        "rawRowsEstimated": False,
    }


# ---------------- 同花顺 ----------------

THS_CONFIG: Dict[str, object] = {}


def load_ths_config() -> Dict:
    global THS_CONFIG
    if not THS_CONFIG:
        try:
            THS_CONFIG = dict(json.loads(THS_CONFIG_CACHE_PATH.read_text(encoding="utf-8")) or {})
        except (OSError, json.JSONDecodeError):
            THS_CONFIG = {}
    return THS_CONFIG


def save_ths_config() -> None:
    THS_CONFIG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = THS_CONFIG_CACHE_PATH.with_name(THS_CONFIG_CACHE_PATH.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(THS_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, THS_CONFIG_CACHE_PATH)


def _ths_market_fallback(code: str) -> int:
    if code == "1A0001":
        return 16
    return 17 if code.startswith(("5", "6", "9")) else 33


def ths_get_json(path_and_query: str) -> Dict:
    # 只读公开社区端点，不传账号/Cookie。Windows下HTTPS常先挂起再断开，
    # 同一公开JSON的HTTP入口稳定；失败回退HTTPS。
    global THS_API_BASE
    if THS_API_BASE:
        try:
            return request_json(THS_API_BASE + path_and_query, "ths", attempts=1, timeout=8)
        except RuntimeError:
            THS_API_BASE = None
    last_error = None
    for base in ("http://c.10jqka.com.cn", "https://c.10jqka.com.cn"):
        try:
            payload = request_json(base + path_and_query, "ths", attempts=1, timeout=8)
            THS_API_BASE = base
            return payload
        except RuntimeError as exc:
            last_error = exc
    raise last_error or RuntimeError("同花顺公开社区端点不可用")


def _ths_cached_config(cache_key: str):
    value = load_ths_config().get(cache_key)
    if not isinstance(value, dict):
        return None
    discovered = parse_time(value.get("discoveredAt"))
    if discovered is None or datetime.now(CN_TZ) - discovered > timedelta(days=7):
        return None
    return dict(value)


def discover_ths_forum(spec: Mapping, as_of: datetime = None) -> Dict:
    """概念区 + 关联社区：从论坛元数据 related_scenes 发现，合并为一个来源单位。"""
    cache_key = str(spec["key"])
    cached = _ths_cached_config(cache_key)
    if cached:
        return cached
    forum_code = str(spec["code"])
    market_id = safe_int(spec.get("marketId"), _ths_market_fallback(forum_code))
    forum_name = str(spec.get("name") or forum_code)
    related_scenes: List[Dict] = []
    discovery_status = "direct_only"
    if bool(spec.get("preferScene", True)):
        query = urllib.parse.urlencode({"code": forum_code, "marketId": market_id, "source": "stock"})
        try:
            payload = ths_get_json("/lgt/cache/open/api/forum/v2/index?" + query)
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
            configs = data.get("function_configs") if isinstance(data.get("function_configs"), Mapping) else {}
            raw_scenes = configs.get("related_scenes") if isinstance(configs, Mapping) else []
            for scene in raw_scenes if isinstance(raw_scenes, list) else []:
                if not isinstance(scene, Mapping):
                    continue
                scene_code = str(scene.get("scene_code") or "").strip()
                scene_market = safe_int(scene.get("scene_market"), 0)
                if not scene_code or not scene_market:
                    continue
                related_scenes.append({
                    "forumCode": scene_code, "marketId": scene_market,
                    "forumName": str(scene.get("scene_name") or scene_code),
                    "talkCount": max(0, safe_int(scene.get("talk_num"))),
                })
            discovery_status = "metadata_with_related_scenes"
        except RuntimeError:
            discovery_status = "metadata_unavailable_direct_fallback"
    result = {
        "forumCode": forum_code, "marketId": market_id, "forumName": forum_name,
        "relatedScenes": related_scenes, "discoveryStatus": discovery_status,
        "discoveredAt": datetime.now(CN_TZ).isoformat(),
        "directFallback": not bool(related_scenes),
    }
    load_ths_config()[cache_key] = result
    return result


def fetch_ths_hot_pages(forum_code: str, market_id: int):
    """非时序热门流，last_score游标，严格限页。不参与attention/volume/零帖核验。"""
    collected: List[Dict] = []
    seen = set()
    last_score = None
    page_count = 0
    has_more = False
    first_path = ""
    for _ in range(THS_CURSOR_PAGE_LIMIT):
        params: Dict[str, object] = {
            "code": forum_code, "market_id": market_id, "page": 1, "page_size": 15,
        }
        if last_score not in (None, ""):
            params["last_score"] = last_score
        path = "/lgt/post/open/api/forum/content/v1/hot_feed?" + urllib.parse.urlencode(params)
        if not first_path:
            first_path = path
        payload = ths_get_json(path)
        if safe_int(payload.get("status_code"), -1) != 0:
            raise RuntimeError(str(payload.get("status_msg") or "同花顺社区读取失败"))
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        rows = data.get("feed") if isinstance(data.get("feed"), list) else []
        page_count += 1
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
            identity = str(info.get("id") or hashlib.sha256(
                json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest())
            if identity in seen:
                continue
            seen.add(identity)
            collected.append(row)
            added += 1
        has_more = bool(data.get("has_more"))
        next_score = data.get("last_score")
        if not rows or not has_more or not added or next_score in (None, "", last_score):
            break
        last_score = next_score
        time.sleep(0.8)
    return collected, page_count, has_more, first_path


def fetch_ths(spec: Mapping, as_of: datetime = None) -> Dict:
    as_of = as_of or datetime.now(CN_TZ)
    time.sleep(0.5)
    config = discover_ths_forum(spec, as_of)
    forum_code = str(config["forumCode"])
    forum_name = str(config.get("forumName") or spec.get("name") or forum_code)
    label = f"同花顺{forum_name}"
    source_key = str(spec["key"])
    entries = [{
        "forumCode": forum_code, "marketId": safe_int(config["marketId"]),
        "forumName": forum_name, "layer": "concept",
    }]
    for scene in config.get("relatedScenes", []):
        if isinstance(scene, Mapping):
            entries.append({**dict(scene), "layer": "related_scene"})

    feed_rows: List = []
    page_count = 0
    cursor_has_more = False
    first_path = ""
    for entry in entries:
        rows, pages, has_more, path = fetch_ths_hot_pages(
            str(entry["forumCode"]), safe_int(entry["marketId"]))
        page_count += pages
        cursor_has_more = cursor_has_more or has_more
        if not first_path:
            first_path = path
        feed_rows.extend((row, entry) for row in rows)
        if entry is not entries[-1]:
            time.sleep(0.8)
    api_url = "https://c.10jqka.com.cn" + first_path

    posts: List[Dict] = []
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    seen_ids = set()
    raw_times: List[datetime] = []
    ordinary_post_count = 0
    ordinary_comment_count = 0
    related_scene_post_count = 0
    for row, entry in feed_rows:
        if not isinstance(row, dict):
            continue
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        stat = row.get("stat") if isinstance(row.get("stat"), dict) else {}
        published = parse_time(info.get("ctime"))
        if published is None:
            continue
        raw_times.append(published)
        post_id = str(info.get("id") or "")
        author_name = str(author.get("name") or "")
        author_key = str(author.get("id") or author_name or post_id)
        # 认证/勋章/官方账号清零剔除；草根内容权重高
        is_ordinary_post = (
            str(info.get("state") or "") == "passAudit"
            and str(info.get("type") or "") == "post"
            and safe_int(info.get("community_biz_type"), -1) == 1
            and not author.get("identity_tag")
            and not author.get("medals")
            and not author.get("medal")
            and not any(word in author_name for word in AUTHOR_BLOCK_WORDS)
            and not row.get("forward")
        )
        if is_ordinary_post and post_id not in seen_ids:
            def full_nested(value) -> str:
                return clean_text(value.get("content"), max_chars=2000) if isinstance(value, Mapping) else clean_text(value, max_chars=2000)
            full_text = " ".join(filter(None, (full_nested(row.get("title")), full_nested(row.get("abstract")))))
            text = clean_text(full_text)
            # 同花顺每作者每日1帖
            if valid_text(text) and author_daily_admit(author_day_counts, author_key, published, limit=1):
                seen_ids.add(post_id)
                post = make_post(
                    post_id=post_id or normalize_text(text), text=text, published=published,
                    comments=stat.get("comment_num"), clicks=stat.get("forward_num"),
                    likes=stat.get("like_num"), source_key=source_key, source_code=forum_code,
                    source_name=label, platform="ths", author_key=author_key,
                )
                post.update({
                    "originalLength": len(normalize_text(full_text)),
                    "stockTagCount": sum(
                        1 for tag in row.get("tags", [])
                        if isinstance(tag, Mapping) and str(tag.get("type") or "").lower() == "stock"
                    ) if isinstance(row.get("tags"), list) else 0,
                    "communityLayer": entry.get("layer"),
                    "communityName": entry.get("forumName"),
                    "certified": False,
                })
                posts.append(post)
                ordinary_post_count += 1
                if entry.get("layer") == "related_scene":
                    related_scene_post_count += 1

        comment_block = row.get("comment") if isinstance(row.get("comment"), dict) else {}
        comments = comment_block.get("comments") if isinstance(comment_block.get("comments"), list) else []
        for index, comment in enumerate(comments):
            if not isinstance(comment, dict) or str(comment.get("tag") or "") != "ordinary":
                continue
            nickname = str(comment.get("nickname") or "")
            if any(word in nickname for word in AUTHOR_BLOCK_WORDS):
                continue
            user_key = str(comment.get("uid") or nickname or f"{post_id}:{index}")
            text = clean_text(comment.get("content"), max_chars=180)
            if not valid_text(text):
                continue
            comment_published = parse_time(
                comment.get("ctime") or comment.get("create_time")
                or comment.get("timestamp") or comment.get("publish_time")
            )
            time_inherited = comment_published is None
            if comment_published is None:
                comment_published = published
            else:
                raw_times.append(comment_published)
            if not author_daily_admit(author_day_counts, user_key, comment_published, limit=1):
                continue
            comment_id = str(comment.get("id") or f"{post_id}:comment:{index}")
            if comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)
            post = make_post(
                post_id=comment_id, text=text, published=comment_published,
                comments=0, clicks=0, likes=0, source_key=source_key, source_code=forum_code,
                source_name=label, platform="ths", author_key=user_key, kind="comment",
                time_inherited=time_inherited, parent_post_id=post_id,
            )
            post.update({
                "originalLength": len(normalize_text(text)),
                "stockTagCount": 0,
                "communityLayer": entry.get("layer"),
                "communityName": entry.get("forumName"),
                "certified": False,
            })
            posts.append(post)
            ordinary_comment_count += 1
    if not feed_rows:
        raise RuntimeError(f"{label} 公开热门流为空")
    return {
        "key": source_key, "code": forum_code, "name": label, "platform": "ths",
        "url": api_url, "posts": posts, "fetchedAt": datetime.now(CN_TZ).isoformat(),
        "fresh": True,
        **collection_metadata(raw_times=raw_times, posts=posts, raw_rows=len(feed_rows),
                              page_count=page_count, cap_hit=cursor_has_more,
                              chronological=False, fresh_fetched_now=True),
        "coverageStatus": "non_chronological_hot_feed",
        "paginationMode": "last_score_cursor_bounded",
        "feedType": "concept_plus_related_scene_hot_feed",
        "rawRowsEstimated": False,
        "hotFeedCensored": True,
        "ordinaryPostCount": ordinary_post_count,
        "ordinaryCommentCount": ordinary_comment_count,
        "relatedScenePostCount": related_scene_post_count,
        "relatedScenes": [
            {"code": entry.get("forumCode"), "marketId": entry.get("marketId"),
             "name": entry.get("forumName"), "layer": entry.get("layer")}
            for entry in entries
        ],
        "attentionEligible": False,
        "discoveryStatus": config.get("discoveryStatus"),
    }


# ---------------- 淘股吧 ----------------

def fetch_taoguba(spec: Mapping, as_of: datetime = None) -> Dict:
    as_of = as_of or datetime.now(CN_TZ)
    """匿名首页20行下限快照。认证用户剔除。续页需登录，显式标记为下限。"""
    code = str(spec["code"])
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    full_code = str(spec.get("symbol") or (prefix + code))
    source_key = str(spec["key"])
    source_name = f"淘股吧{spec.get('name') or code}讨论"
    url = f"https://www.tgb.cn/quotes/{full_code}"
    page = request_text(url, "taoguba", attempts=2, timeout=12)
    matched = re.search(r"var\s+coolAttr\s*=\s*(\[.*?\]);\s*var\s+tempFeedID", page, re.DOTALL)
    if not matched:
        matched = re.search(r"var\s+coolAttr\s*=\s*(\[.*?\])\s*;", page, re.DOTALL)
    if not matched:
        raise RuntimeError(f"{source_name} 页面没有公开最新讨论数据")
    try:
        raw_value = json.loads(matched.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{source_name} 最新讨论结构异常") from exc
    rows = raw_value if isinstance(raw_value, list) else []
    if not rows:
        raise RuntimeError(f"{source_name} 公开最新讨论为空")

    posts: List[Dict] = []
    raw_times: List[datetime] = []
    seen_ids = set()
    author_day_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    excluded_certified = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        published = parse_time(row.get("actionDate"))
        if published is None:
            continue
        raw_times.append(published)
        user_title = row.get("userTitle") if isinstance(row.get("userTitle"), Mapping) else {}
        if safe_int(row.get("auth"), 0) > 0 or str(user_title.get("title") or "").strip():
            excluded_certified += 1
            continue
        if str(row.get("deleteFlag") or "N").upper() not in {"", "N", "0", "FALSE"}:
            continue
        author_name = clean_text(row.get("userName"), max_chars=60)
        if any(word in author_name for word in AUTHOR_BLOCK_WORDS):
            excluded_certified += 1
            continue
        post_id = str(row.get("rID") or row.get("riD") or row.get("newTopicID") or "")
        if not post_id or post_id in seen_ids:
            continue
        raw_subject = str(row.get("subject") or "")
        raw_body = str(row.get("body") or "")
        raw_html = " ".join(filter(None, (raw_subject, raw_body)))
        kind = "reply" if str(row.get("rType") or row.get("rtype") or "").upper() == "R" else "post"
        text = clean_text(raw_body if kind == "reply" else raw_html)
        if not valid_text(text):
            continue
        author_key = str(row.get("userID") or author_name or post_id)
        if not author_daily_admit(author_day_counts, author_key, published, limit=TAOGUBA_AUTHOR_DAILY_CAP):
            continue
        seen_ids.add(post_id)
        post = make_post(
            post_id=post_id, text=text, published=published,
            comments=row.get("replyNum"), clicks=row.get("viewNum"),
            likes=row.get("usefulNum"), source_key=source_key, source_code=code,
            source_name=source_name, platform="taoguba", author_key=author_key, kind=kind,
        )
        post.update({
            "originalLength": len(normalize_text(clean_text(raw_html, max_chars=4000))),
            "stockTagCount": len(re.findall(r"<font[^>]*color\s*=\s*['\"]?#?4793F1", raw_html, re.I)),
            "certified": False,
            "anonymousPublicInitialPage": True,
        })
        posts.append(post)

    third_reached = reached_third_scoring_day(raw_times, as_of)
    return {
        "key": source_key, "code": code, "name": source_name, "platform": "taoguba",
        "url": url, "posts": posts, "fetchedAt": datetime.now(CN_TZ).isoformat(),
        "fresh": True,
        **collection_metadata(raw_times=raw_times, posts=posts, raw_rows=len(rows),
                              page_count=1, cap_hit=len(rows) >= TAOGUBA_PAGE_SIZE,
                              chronological=True, fresh_fetched_now=True,
                              third_day_reached=third_reached),
        "coverageStatus": "third_day_reached" if third_reached else "anonymous_page_cap_lower_bound",
        "paginationMode": "anonymous_initial_page_only",
        "feedType": "latest_stock_discussion",
        "rawRowsEstimated": False,
        "volumeIsLowerBound": True,
        "excludedCertifiedCount": excluded_certified,
        "attentionEligible": True,
    }


FETCHERS = {
    "eastmoney": fetch_eastmoney,
    "sina": fetch_sina,
    "ths": fetch_ths,
    "taoguba": fetch_taoguba,
}
