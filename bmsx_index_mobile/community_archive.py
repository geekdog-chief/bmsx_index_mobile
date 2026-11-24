"""按发布日期增量保存公开社区原始文本，供未来回溯重算。

档案与短期失败缓存分离：forum_cache 可以继续覆盖，而这里按帖子去重追加。
每个发布日期一个 JSON 文件，另有 manifest.json 记录成功采集日和来源覆盖。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping


CN_TZ = timezone(timedelta(hours=8))
ARCHIVE_RETENTION_DAYS = 400
MANIFEST_NAME = "manifest.json"


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed.replace(tzinfo=CN_TZ) if parsed.tzinfo is None else parsed.astimezone(CN_TZ)


def _load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _archive_key(post: Mapping[str, object], platform: str) -> str:
    raw_id = str(post.get("id") or "").strip()
    if raw_id:
        return raw_id if raw_id.startswith(f"{platform}:") else f"{platform}:{raw_id}"
    # 正常采集路径始终有 id；该回退只服务旧缓存迁移。
    return f"{platform}:{post.get('sourceKey', '')}:{post.get('publishTime', '')}:{post.get('title', '')}"


def _serializable_post(
    post: Mapping[str, object],
    source: Mapping[str, object],
    collected_at: datetime,
) -> Dict[str, object] | None:
    published = _parse_datetime(post.get("publishTime") or post.get("published"))
    if published is None:
        return None
    platform = str(post.get("platform") or source.get("platform") or "unknown")
    source_key = str(post.get("sourceKey") or source.get("key") or "unknown")
    return {
        "archiveKey": _archive_key(post, platform),
        "id": str(post.get("id") or ""),
        "title": str(post.get("title") or ""),
        "publishTime": published.isoformat(),
        "collectedAt": collected_at.isoformat(),
        "firstCollectedAt": collected_at.isoformat(),
        "lastCollectedAt": collected_at.isoformat(),
        "commentsAtCollection": int(post.get("comments") or 0),
        "clicksAtCollection": int(post.get("clicks") or 0),
        "likesAtCollection": int(post.get("likes") or 0),
        "sourceKey": source_key,
        "sourceKeys": [source_key],
        "sourceCode": str(post.get("sourceCode") or source.get("code") or ""),
        "source": str(post.get("source") or source.get("name") or ""),
        "platform": platform,
        "platformName": str(post.get("platformName") or ""),
        "authorKey": str(post.get("authorKey") or ""),
        "kind": str(post.get("kind") or "post"),
    }


def archive_results(
    results: Mapping[str, Mapping[str, object]],
    archive_dir: Path,
    *,
    collected_at: datetime,
    retention_days: int = ARCHIVE_RETENTION_DAYS,
) -> Dict[str, int]:
    """增量归档，返回新增/已存在/清理文件数。

    同一 platform + post id 只保留一份正文和首次采集互动量；重复看到时仅
    更新 lastCollectedAt 并合并 sourceKeys。这样历史互动不会被后续累计值覆盖。
    """

    collected = collected_at.replace(tzinfo=CN_TZ) if collected_at.tzinfo is None else collected_at.astimezone(CN_TZ)
    archive_dir.mkdir(parents=True, exist_ok=True)
    by_day: Dict[str, list[Dict[str, object]]] = {}
    for source in results.values():
        for post in source.get("posts", []):
            if not isinstance(post, Mapping):
                continue
            item = _serializable_post(post, source, collected)
            if item is None:
                continue
            day = str(item["publishTime"])[:10]
            by_day.setdefault(day, []).append(item)

    added = 0
    existing_count = 0
    for day, incoming in by_day.items():
        path = archive_dir / f"{day}.json"
        old = _load_json(path, [])
        current = {
            str(item.get("archiveKey")): dict(item)
            for item in old if isinstance(old, list) and isinstance(item, Mapping) and item.get("archiveKey")
        }
        for item in incoming:
            key = str(item["archiveKey"])
            previous = current.get(key)
            if previous is None:
                current[key] = item
                added += 1
                continue
            existing_count += 1
            previous["lastCollectedAt"] = collected.isoformat()
            sources = {str(value) for value in previous.get("sourceKeys", []) if value}
            sources.update(str(value) for value in item.get("sourceKeys", []) if value)
            previous["sourceKeys"] = sorted(sources)
        _atomic_json(path, sorted(current.values(), key=lambda item: (str(item.get("publishTime")), str(item.get("archiveKey")))))

    manifest_path = archive_dir / MANIFEST_NAME
    raw_manifest = _load_json(manifest_path, {})
    manifest = dict(raw_manifest) if isinstance(raw_manifest, Mapping) else {}
    runs = dict(manifest.get("collectionRuns") or {})
    run_day = collected.strftime("%Y-%m-%d")
    platforms = {str(source.get("platform") or "unknown") for source in results.values() if source.get("posts")}
    source_coverage = [
        {
            "key": str(source.get("key") or source_key),
            "code": str(source.get("code") or ""),
            "platform": str(source.get("platform") or "unknown"),
            "rawRows": int(source.get("rawRows") or 0),
            "pageCount": int(source.get("pageCount") or 0),
            "oldestSeen": source.get("oldestSeen"),
            "newestSeen": source.get("newestSeen"),
            "capHit": source.get("capHit"),
            "chronological": bool(source.get("chronological")),
            "coverageDays": list(source.get("coverageDays") or []),
            "thirdTradingDayReached": bool(source.get("thirdTradingDayReached")),
            "freshFetchedNow": bool(source.get("freshFetchedNow")),
            "coverageStatus": source.get("coverageStatus"),
            "paginationMode": source.get("paginationMode"),
        }
        for source_key, source in sorted(results.items())
    ]
    runs[run_day] = {
        "collectedAt": collected.isoformat(),
        "sourceCount": sum(bool(source.get("posts")) for source in results.values()),
        "platformCount": len(platforms),
        "postCountSeen": sum(len(source.get("posts", [])) for source in results.values()),
        "newPostCount": added,
        "sourceCoverage": source_coverage,
    }

    cutoff = (collected - timedelta(days=max(120, int(retention_days)))).date()
    removed = 0
    archive_root = archive_dir.resolve()
    for path in archive_dir.glob("????-??-??.json"):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            continue
        resolved = path.resolve()
        if resolved.parent != archive_root:
            continue
        resolved.unlink()
        removed += 1
    runs = {day: info for day, info in runs.items() if day >= cutoff.isoformat()}
    manifest.update({
        "schemaVersion": 2,
        "retentionDays": max(120, int(retention_days)),
        "updatedAt": collected.isoformat(),
        "collectionRuns": runs,
    })
    _atomic_json(manifest_path, manifest)
    return {"added": added, "existing": existing_count, "removedFiles": removed}


def load_archived_results(
    archive_dir: Path,
    *,
    start: datetime,
    end: datetime,
    observed_at: datetime | None = None,
) -> Dict[str, Dict[str, object]]:
    """加载一个时间窗口，并恢复为 score_results_as_of 可消费的 results。

    ``observed_at`` 用于重建历史实测截面：只纳入在该时点之前已经首次采到
    的帖子，避免把后来才翻页发现的旧帖回填进当时的“实测”结果。一个帖子
    若曾出现在多个板块来源，会按 ``sourceKeys`` 恢复到每个来源。
    """

    start_at = start.replace(tzinfo=CN_TZ) if start.tzinfo is None else start.astimezone(CN_TZ)
    end_at = end.replace(tzinfo=CN_TZ) if end.tzinfo is None else end.astimezone(CN_TZ)
    observed_cutoff = (
        observed_at.replace(tzinfo=CN_TZ)
        if observed_at is not None and observed_at.tzinfo is None
        else observed_at.astimezone(CN_TZ) if observed_at is not None else None
    )
    if end_at < start_at:
        raise ValueError("archive start 不能晚于 end")
    results: Dict[str, Dict[str, object]] = {}
    day = start_at.date()
    while day <= end_at.date():
        raw = _load_json(archive_dir / f"{day.isoformat()}.json", [])
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                published = _parse_datetime(item.get("publishTime"))
                if published is None or not (start_at <= published <= end_at):
                    continue
                first_collected = _parse_datetime(item.get("firstCollectedAt") or item.get("collectedAt"))
                if observed_cutoff is not None and first_collected is not None and first_collected > observed_cutoff:
                    continue
                platform = str(item.get("platform") or "unknown")
                source_keys = [
                    str(value) for value in item.get("sourceKeys", []) if str(value).strip()
                ]
                if not source_keys:
                    source_keys = [str(item.get("sourceKey") or "unknown")]
                for source_key in dict.fromkeys(source_keys):
                    source = results.setdefault(source_key, {
                        "key": source_key,
                        "code": str(item.get("sourceCode") or source_key.split(":", 1)[-1]),
                        "name": str(item.get("source") or source_key),
                        "platform": platform,
                        "posts": [],
                        "fresh": False,
                        "archive": True,
                    })
                    source["posts"].append({
                        "id": str(item.get("id") or item.get("archiveKey") or ""),
                        "title": str(item.get("title") or ""),
                        "published": published,
                        "publishTime": published.isoformat(),
                        # 这些是首次采集时的互动，只供 measured 迁移；estimated 明确不用。
                        "comments": int(item.get("commentsAtCollection") or 0),
                        "clicks": int(item.get("clicksAtCollection") or 0),
                        "likes": int(item.get("likesAtCollection") or 0),
                        "sourceKey": source_key,
                        "sourceCode": str(item.get("sourceCode") or source_key.split(":", 1)[-1]),
                        "source": str(item.get("source") or ""),
                        "platform": platform,
                        "platformName": str(item.get("platformName") or ""),
                        "authorKey": str(item.get("authorKey") or ""),
                        "kind": str(item.get("kind") or "post"),
                        "collectedAt": str(item.get("firstCollectedAt") or item.get("collectedAt") or ""),
                    })
        day += timedelta(days=1)
    manifest = _load_json(archive_dir / MANIFEST_NAME, {})
    runs = manifest.get("collectionRuns", {}) if isinstance(manifest, Mapping) else {}
    run_day = (observed_cutoff or end_at).date().isoformat()
    run = runs.get(run_day, {}) if isinstance(runs, Mapping) else {}
    coverage_rows = run.get("sourceCoverage", []) if isinstance(run, Mapping) else []
    for coverage in coverage_rows if isinstance(coverage_rows, list) else []:
        if not isinstance(coverage, Mapping):
            continue
        source_key = str(coverage.get("key") or "")
        if not source_key:
            continue
        source = results.setdefault(source_key, {
            "key": source_key,
            "code": str(coverage.get("code") or source_key.split(":", 1)[-1]),
            "name": source_key,
            "platform": str(coverage.get("platform") or "unknown"),
            "posts": [],
            "fresh": False,
            "archive": True,
        })
        source.update({
            key: coverage.get(key)
            for key in (
                "rawRows", "pageCount", "oldestSeen", "newestSeen", "capHit",
                "chronological", "coverageDays", "thirdTradingDayReached",
                "freshFetchedNow", "coverageStatus", "paginationMode",
            )
        })
        source["fetchedAt"] = run.get("collectedAt")
        source["rawRowsEstimated"] = False
    return results
