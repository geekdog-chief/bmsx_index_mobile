"""历史快照的纯函数工具。

该模块不联网，也不依赖页面代码。它负责：

* 明确区分每日实测快照与历史回溯估算；
* 保存总市场六项信号及每个板块的三类人群指数/六项信号；
* 用覆盖度与置信度表达缺失，而不是把缺失来源伪装成 50 分；
* 按日期安全合并历史，确保实测点不会被估算点覆盖。
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Sequence

from trading_calendar import calendar_mode, is_trading_day, latest_trading_day


CN_TZ = timezone(timedelta(hours=8))
HISTORY_SCHEMA_VERSION = 5
HISTORY_LIMIT = 365
SCORE_KEYS = ("baoma", "novice", "retail", "composite")
SIGNAL_KEYS = ("optimism", "fomo", "panic", "newcomer", "intensity", "attention")
MEASURED = "measured"
ESTIMATED = "estimated"
QUALITY_MEASURED = "measured"
QUALITY_USABLE = "usable"
QUALITY_PROVISIONAL = "provisional"
MIN_ARCHIVE_COMPLETENESS = 70.0
MIN_ESTIMATED_CONFIDENCE = 55
MIN_RECENT_PUBLISHED_PLATFORMS = 2


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def round1(value: object, default: float = 0.0) -> float:
    try:
        return round(clamp(float(value)), 1)
    except (TypeError, ValueError):
        return round(clamp(default), 1)


def as_of_datetime(value: datetime | date | str, tz: timezone = CN_TZ) -> datetime:
    """把日期规范为该自然日末，把 datetime 规范为指定时区。

    历史日按日末截面计算，因而当天 23:59:59.999999 之后发布的文本不会
    混入；实时 datetime 则保留准确时刻。
    """

    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    if isinstance(value, date):
        return datetime.combine(value, time.max, tzinfo=tz)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无效的 as_of 日期：{value}") from exc
    if "T" not in text and " " not in text:
        return datetime.combine(parsed.date(), time.max, tzinfo=tz)
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


def visible_at(
    published: datetime,
    as_of: datetime | date | str,
    lookback_days: int = 30,
) -> bool:
    """判断一条文本在 as_of 截面是否可见，严格排除未来文本。"""

    cutoff = as_of_datetime(as_of)
    instant = published.replace(tzinfo=cutoff.tzinfo) if published.tzinfo is None else published.astimezone(cutoff.tzinfo)
    age = (cutoff - instant).total_seconds() / 86400.0
    return 0.0 <= age <= float(lookback_days)


def normalized_weighted_average(items: Iterable[tuple[float, float]]) -> float | None:
    """只在已有观测之间归一化；没有观测返回 None，不注入默认 50。"""

    usable = [(max(0.0, float(weight)), float(value)) for weight, value in items if value is not None]
    total = sum(weight for weight, _ in usable)
    if total <= 0.0:
        return None
    return sum(weight * value for weight, value in usable) / total


def coverage_details(
    *,
    platform_count: int,
    expected_platform_count: int,
    source_count: int,
    expected_source_count: int,
    sector_count: int,
    expected_sector_count: int,
    sample_posts: int,
    fresh_source_count: int | None = None,
    archive_completeness: float | None = None,
) -> Dict[str, object]:
    """生成透明的覆盖明细及 0–100 覆盖度。

    样本目标按每个预期来源 8 条设置，只用于判断是否稀疏，不参与指数值。
    回溯任务可额外传入 archive_completeness（0–1），反映历史翻页完整度。
    """

    platform_expected = max(1, int(expected_platform_count))
    source_expected = max(1, int(expected_source_count))
    sector_expected = max(1, int(expected_sector_count))
    platform_ratio = min(1.0, max(0, int(platform_count)) / platform_expected)
    source_ratio = min(1.0, max(0, int(source_count)) / source_expected)
    sector_ratio = min(1.0, max(0, int(sector_count)) / sector_expected)
    sample_target = max(24, source_expected * 8)
    sample_ratio = min(1.0, math.sqrt(max(0, int(sample_posts)) / sample_target))
    archive_ratio = None if archive_completeness is None else min(1.0, max(0.0, float(archive_completeness)))

    weighted = 0.28 * platform_ratio + 0.34 * source_ratio + 0.24 * sector_ratio + 0.14 * sample_ratio
    if archive_ratio is not None:
        # 历史档案完整性是回溯估算的硬约束，不能被大量单日文本掩盖。
        weighted = 0.75 * weighted + 0.25 * archive_ratio
    coverage = round(clamp(weighted * 100.0), 1)
    result: Dict[str, object] = {
        "score": coverage,
        "platformCount": max(0, int(platform_count)),
        "expectedPlatformCount": platform_expected,
        "platformRatio": round(platform_ratio * 100.0, 1),
        "sourceCount": max(0, int(source_count)),
        "expectedSourceCount": source_expected,
        "sourceRatio": round(source_ratio * 100.0, 1),
        "sectorCount": max(0, int(sector_count)),
        "expectedSectorCount": sector_expected,
        "sectorRatio": round(sector_ratio * 100.0, 1),
        "samplePosts": max(0, int(sample_posts)),
        "sampleTarget": sample_target,
        "sampleRatio": round(sample_ratio * 100.0, 1),
    }
    if fresh_source_count is not None:
        fresh = max(0, min(int(fresh_source_count), max(0, int(source_count))))
        result["freshSourceCount"] = fresh
        result["freshSourceRatio"] = round(fresh / source_expected * 100.0, 1)
    if archive_ratio is not None:
        result["archiveCompleteness"] = round(archive_ratio * 100.0, 1)
    return result


def confidence_from_coverage(
    details: Mapping[str, object],
    *,
    record_type: str,
) -> int:
    """覆盖度回答“有多少数据”，置信度再纳入新鲜度与回溯偏差。"""

    coverage = round1(details.get("score"))
    expected_sources = max(1, int(details.get("expectedSourceCount") or 1))
    if record_type == MEASURED:
        if "freshSourceCount" in details:
            fresh_ratio = min(1.0, max(0, int(details.get("freshSourceCount") or 0)) / expected_sources)
            value = coverage * 0.72 + fresh_ratio * 100.0 * 0.28
        else:
            value = coverage
        return int(round(clamp(value)))

    archive_ratio = float(details.get("archiveCompleteness") or 0.0) / 100.0
    # 当前看到的互动量不是历史当日互动量，故回溯估算即使覆盖完整也不超过 85。
    value = coverage * (0.62 + 0.23 * archive_ratio)
    return int(round(min(85.0, clamp(value))))


def quality_status_of(point: Mapping[str, object]) -> str:
    """返回历史点的质量状态，并兼容尚无质量字段的旧记录。

    旧 estimated 点无法还原当时的档案完整度与 7 日发布平台，因此只按
    已保存的 confidence 推断；新版回溯点必须由三道门槛显式判定。
    """

    if record_type_of(point) == MEASURED:
        return QUALITY_MEASURED
    explicit = str(point.get("qualityStatus") or "").lower()
    if explicit in {QUALITY_USABLE, QUALITY_PROVISIONAL}:
        return explicit
    try:
        confidence = float(point.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return QUALITY_USABLE if confidence >= MIN_ESTIMATED_CONFIDENCE else QUALITY_PROVISIONAL


def retrospective_quality(
    *,
    archive_completeness: float | None,
    confidence: int,
    recent_published_platform_count: int | None,
    recent_published_platforms: Sequence[str] | None = None,
) -> tuple[str, Dict[str, object]]:
    """按冻结门槛判定回溯点是否足以用于平滑与趋势解读。"""

    archive_pct = round(
        min(1.0, max(0.0, float(archive_completeness or 0.0))) * 100.0,
        1,
    )
    platform_count = max(0, int(recent_published_platform_count or 0))
    platforms = sorted({str(item) for item in (recent_published_platforms or []) if str(item)})
    checks = {
        "archiveCompleteness": archive_pct >= MIN_ARCHIVE_COMPLETENESS,
        "confidence": int(confidence) >= MIN_ESTIMATED_CONFIDENCE,
        "recent7PublishedPlatformCount": platform_count >= MIN_RECENT_PUBLISHED_PLATFORMS,
    }
    reasons = [key for key, passed in checks.items() if not passed]
    status = QUALITY_USABLE if not reasons else QUALITY_PROVISIONAL
    return status, {
        "archiveCompleteness": archive_pct,
        "archiveCompletenessThreshold": MIN_ARCHIVE_COMPLETENESS,
        "confidence": int(confidence),
        "confidenceThreshold": MIN_ESTIMATED_CONFIDENCE,
        "recent7PublishedPlatformCount": platform_count,
        "recent7PublishedPlatformThreshold": MIN_RECENT_PUBLISHED_PLATFORMS,
        "recent7PublishedPlatforms": platforms,
        "publishedWindowDays": 7,
        "usesPublishedAt": True,
        "passed": not reasons,
        "failedChecks": reasons,
    }


def _scores(values: Mapping[str, object]) -> Dict[str, int]:
    return {key: int(round(clamp(float(values.get(key) or 0.0)))) for key in SCORE_KEYS}


def _signals(values: Mapping[str, object]) -> Dict[str, float]:
    result = {key: round1(values.get(key), 50.0) for key in SIGNAL_KEYS}
    if "heat" in values:
        result["heat"] = round1(values.get("heat"), 50.0)
    if "breadth" in values:
        result["breadth"] = round1(values.get("breadth"), 50.0)
    return result


def compact_sector_snapshot(
    item: Mapping[str, object],
    *,
    record_type: str = MEASURED,
    archive_completeness: float | None = None,
) -> Dict[str, object]:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
    scores = item.get("scores") if isinstance(item.get("scores"), Mapping) else {}
    signals = item.get("signals") if isinstance(item.get("signals"), Mapping) else {}
    source_count = int(metrics.get("sourceCount") or 0)
    platform_count = int(metrics.get("platformCount") or 0)
    sample_posts = int(metrics.get("samplePosts") or 0)
    details = coverage_details(
        platform_count=platform_count,
        expected_platform_count=3,
        source_count=source_count,
        expected_source_count=int(item.get("expectedSourceCount") or 5),
        sector_count=1 if item.get("available", sample_posts > 0) else 0,
        expected_sector_count=1,
        sample_posts=sample_posts,
        archive_completeness=archive_completeness if record_type == ESTIMATED else None,
    )
    heat_model = item.get("heatModel") if isinstance(item.get("heatModel"), Mapping) else {}
    compact_heat_model = {
        key: heat_model[key]
        for key in (
            "version", "status", "marketAnchor", "activityIndex", "volumeFactor", "relativeLatent", "heatTier",
            "percentile", "robustZ", "reliability", "dispersionGate",
            "crossSectionMedian", "crossSectionMAD", "crossSectionP90P10",
            "noiseFloor", "rawRelativeHeat", "rawFinalHeat", "rawAbsoluteHeat",
            "temporalSmoothing", "temporalAlpha", "temporalGapDays", "temporalEffectiveAlpha",
            "relativeDailyCap", "finalDailyCap",
            "weights",
            "previousRelativeHeat", "previousFinalHeat",
            "usesMarketPrice", "formalPersonaScoresChanged", "rankEligible",
        )
        if key in heat_model
    }
    display_heat = item.get("heat") if item.get("heat") is not None else signals.get("heat")
    legacy_heat = item.get("legacyHeat") if item.get("legacyHeat") is not None else signals.get("heat")
    absolute_heat = item.get("absoluteHeat") if item.get("absoluteHeat") is not None else legacy_heat
    relative_heat = item.get("relativeHeat") if item.get("relativeHeat") is not None else 50.0
    representatives = item.get("representativeCompanies")
    if not isinstance(representatives, Sequence) or isinstance(representatives, (str, bytes)):
        representatives = []
    compact_representatives = []
    for company in representatives[:3]:
        if not isinstance(company, Mapping):
            continue
        compact_representatives.append({
            key: company[key]
            for key in ("code", "name", "role")
            if key in company
        })
    result = {
        "key": str(item.get("key") or ""),
        "name": str(item.get("name") or item.get("key") or ""),
        "parentKey": str(item.get("parentKey") or ""),
        "parentName": str(item.get("parentName") or ""),
        "description": str(item.get("description") or ""),
        "representativeCompanies": compact_representatives,
        "available": bool(item.get("available", sample_posts > 0)),
        "scores": _scores(scores),
        "signals": _signals(signals),
        "heat": round1(display_heat, 50.0),
        "legacyHeat": round1(legacy_heat, 50.0),
        "absoluteHeat": round1(absolute_heat, 50.0),
        "relativeHeat": round1(relative_heat, 50.0),
        "heatRank": int(item.get("heatRank")) if item.get("heatRank") is not None else None,
        "heatTier": str(item.get("heatTier") or ""),
        "direction": round1(signals.get("optimism"), 50.0),
        "coverage": details["score"],
        "confidence": confidence_from_coverage(details, record_type=record_type),
        "samplePosts": sample_posts,
        "platformCount": platform_count,
        "sourceCount": source_count,
    }
    fairness = metrics.get("fairness") if isinstance(metrics.get("fairness"), Mapping) else None
    if isinstance(fairness, Mapping):
        result["fairness"] = {
            key: fairness[key]
            for key in (
                "modelVersion", "reliabilityPercent", "confidenceLabel",
                "weightedCompanyCoverage", "rankEligible", "stabilityBand", "flags",
            )
            if key in fairness
        }
    if compact_heat_model:
        result["heatModel"] = compact_heat_model
    if item.get("catalogVersion"):
        result["catalogVersion"] = str(item.get("catalogVersion"))
    return result


def build_history_point(
    *,
    as_of: datetime | date | str,
    scores: Mapping[str, object],
    signals: Mapping[str, object],
    sectors: Sequence[Mapping[str, object]],
    platform_count: int,
    expected_platform_count: int,
    source_count: int,
    expected_source_count: int,
    sector_count: int,
    expected_sector_count: int,
    sample_posts: int,
    record_type: str = MEASURED,
    fresh_source_count: int | None = None,
    archive_completeness: float | None = None,
    recent_published_platform_count: int | None = None,
    recent_published_platforms: Sequence[str] | None = None,
    captured_at: datetime | str | None = None,
    methodology: str = "community_text_three_trading_days_v1",
) -> Dict[str, object]:
    if record_type not in {MEASURED, ESTIMATED}:
        raise ValueError(f"未知历史类型：{record_type}")
    cutoff = as_of_datetime(as_of)
    details = coverage_details(
        platform_count=platform_count,
        expected_platform_count=expected_platform_count,
        source_count=source_count,
        expected_source_count=expected_source_count,
        sector_count=sector_count,
        expected_sector_count=expected_sector_count,
        sample_posts=sample_posts,
        fresh_source_count=fresh_source_count,
        archive_completeness=archive_completeness,
    )
    confidence = confidence_from_coverage(details, record_type=record_type)
    if record_type == MEASURED:
        quality_status = QUALITY_MEASURED
        quality_details: Dict[str, object] = {
            "passed": True,
            "basis": "daily_measured_snapshot",
        }
    else:
        quality_status, quality_details = retrospective_quality(
            archive_completeness=archive_completeness,
            confidence=confidence,
            recent_published_platform_count=recent_published_platform_count,
            recent_published_platforms=recent_published_platforms,
        )
    clean_scores = _scores(scores)
    clean_signals = _signals(signals)
    captured = captured_at or datetime.now(CN_TZ)
    captured_text = captured.isoformat() if isinstance(captured, datetime) else str(captured)
    compact_sectors = [
        compact_sector_snapshot(
            item,
            record_type=record_type,
            archive_completeness=archive_completeness,
        )
        for item in sectors
    ]
    catalog_versions = {
        str(item.get("catalogVersion"))
        for item in compact_sectors
        if item.get("catalogVersion")
    }
    result = {
        "schemaVersion": HISTORY_SCHEMA_VERSION,
        "date": cutoff.strftime("%Y-%m-%d"),
        **clean_scores,
        "heat": round1(signals.get("heat"), 50.0),
        "direction": round1(signals.get("optimism"), 50.0),
        "signals": clean_signals,
        "sectors": compact_sectors,
        "type": record_type,
        "source": "daily_snapshot" if record_type == MEASURED else "retrospective_text_reconstruction",
        "mode": "daily" if record_type == MEASURED else "retrospective",
        "isEstimated": record_type == ESTIMATED,
        "coverage": details["score"],
        "confidence": confidence,
        "qualityStatus": quality_status,
        "qualityDetails": quality_details,
        "samplePosts": max(0, int(sample_posts)),
        "platformCount": max(0, int(platform_count)),
        "sourceCount": max(0, int(source_count)),
        "sectorCount": max(0, int(sector_count)),
        "coverageDetails": details,
        "asOf": cutoff.isoformat(),
        "capturedAt": captured_text,
        "methodology": methodology,
        "tradingDay": is_trading_day(cutoff.date()),
        "calendarMode": calendar_mode(cutoff.date()),
    }
    if len(catalog_versions) == 1:
        result["sectorCatalogVersion"] = next(iter(catalog_versions))
    return result


def record_type_of(point: Mapping[str, object]) -> str:
    explicit = str(point.get("type") or "").lower()
    if explicit in {MEASURED, ESTIMATED}:
        return explicit
    if bool(point.get("isEstimated")) or str(point.get("mode") or "").lower() in {"estimated", "retrospective"}:
        return ESTIMATED
    # 旧版无类型的点来自当日一键更新，应按实测保护。
    return MEASURED


def upsert_history(
    history: Sequence[Mapping[str, object]],
    point: Mapping[str, object],
    *,
    limit: int = HISTORY_LIMIT,
) -> List[Dict[str, object]]:
    """按日期合并；同日实测优先于估算，同类型以新点覆盖旧点。"""

    incoming = dict(point)
    incoming_date = str(incoming.get("date") or "")
    try:
        datetime.strptime(incoming_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"历史点缺少有效 date：{incoming_date}") from exc

    by_date: Dict[str, Dict[str, object]] = {}
    for raw in history:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        key = str(item.get("date") or "")
        try:
            datetime.strptime(key, "%Y-%m-%d")
        except ValueError:
            continue
        previous = by_date.get(key)
        if previous is None or record_type_of(item) == MEASURED or record_type_of(previous) == ESTIMATED:
            by_date[key] = item

    existing = by_date.get(incoming_date)
    if existing is None or record_type_of(incoming) == MEASURED or record_type_of(existing) == ESTIMATED:
        by_date[incoming_date] = incoming
    ordered = [by_date[key] for key in sorted(by_date)]
    return ordered[-max(1, int(limit)):]


def build_history_meta(history: Sequence[Mapping[str, object]], *, updated_at: datetime | str | None = None) -> Dict[str, object]:
    valid = [dict(item) for item in history if isinstance(item, Mapping) and item.get("date")]
    valid.sort(key=lambda item: str(item["date"]))
    segments: List[Dict[str, object]] = []
    for item in valid:
        kind = record_type_of(item)
        day = str(item["date"])
        methodology = str(item.get("methodology") or "legacy_unspecified")
        previous_day = (
            date.fromisoformat(str(segments[-1]["endDate"])) if segments else None
        )
        current_day = date.fromisoformat(day)
        # 历史现在只保存交易日；周五到下周一、节前到节后仍属于相邻点。
        is_contiguous = (
            previous_day is not None
            and is_trading_day(current_day)
            and latest_trading_day(current_day - timedelta(days=1)) == previous_day
        )
        if (
            not segments
            or segments[-1]["type"] != kind
            or segments[-1].get("methodology") != methodology
            or not is_contiguous
        ):
            segments.append({
                "type": kind,
                "methodology": methodology,
                "startDate": day,
                "endDate": day,
                "count": 1,
            })
        else:
            segments[-1]["endDate"] = day
            segments[-1]["count"] = int(segments[-1]["count"]) + 1
    stamp = updated_at or datetime.now(CN_TZ)
    stamp_text = stamp.isoformat() if isinstance(stamp, datetime) else str(stamp)
    return {
        "schemaVersion": HISTORY_SCHEMA_VERSION,
        "updatedAt": stamp_text,
        "startDate": str(valid[0]["date"]) if valid else None,
        "endDate": str(valid[-1]["date"]) if valid else None,
        "count": len(valid),
        "measuredCount": sum(record_type_of(item) == MEASURED for item in valid),
        "estimatedCount": sum(record_type_of(item) == ESTIMATED for item in valid),
        "usableEstimatedCount": sum(
            record_type_of(item) == ESTIMATED and quality_status_of(item) == QUALITY_USABLE
            for item in valid
        ),
        "provisionalEstimatedCount": sum(
            record_type_of(item) == ESTIMATED and quality_status_of(item) == QUALITY_PROVISIONAL
            for item in valid
        ),
        "segments": segments,
        "calendar": "A股交易日；2025/2026按上交所官方休市安排，其他年份按工作日回退。",
        "methodologyBreakCount": max(0, len({
            str(item.get("methodology") or "legacy_unspecified") for item in valid
        }) - 1),
        "latestMethodology": str(valid[-1].get("methodology") or "legacy_unspecified") if valid else None,
        "note": "实测与回溯分开展示；不同 methodology 段属于不同统计口径，只看各段内变化，不直接比较断点两侧绝对值。",
    }
