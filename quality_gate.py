"""K1 采集质量门：把 collectors 记录的质量字段变成可消费的观测信号。

原则（ChatGPT 架构审查 2026-11-27 批准稿）：
- 不改动评分输入与公式（宪法冻结）；残缺源继续进窗口，只打标、告警、落盘。
- 判定顺序：empty → hot_feed → 已知截断状态 → unknown → full。
  hot_feed 必须先于 capHit：同花顺热帖流 capHit 可为 True，先判 capHit 会误报 partial。
- capHit 严格按布尔 True 处理；None/缺失 ≠ False。
- rawRows/coverageDays 只是诊断证据，不设数量门槛。
- 映射表逐字核对自 collectors.py 当前实现，不推测。
"""

from typing import Dict, List, Mapping, Optional

# 字面值映射，逐项核对自 collectors.py（2026-11-27 HEAD=3d7ebe79）：
_STATUS_MAP = {
    # full：采集器确认覆盖到位
    "third_trading_day_reached": "full",
    "source_exhausted_before_third_trading_day": "full",
    "third_day_reached": "full",  # 淘股吧匿名快照
    # partial：确认截断或无法证明完整
    "page_cap_reached_before_third_trading_day": "partial",
    "cursor_stalled_before_third_trading_day": "partial",
    "anonymous_page_cap_lower_bound": "partial",
    # unknown：证据不足
    "single_page_fallback_unknown": "unknown",
    "legacy_cache_unknown": "unknown",
    "unknown": "unknown",
    # hot_feed：设计属性，非截断（必须在 capHit 之前判定）
    "non_chronological_hot_feed": "hot_feed",
}

# 已知字面值里属于 partial 的（供 capHit 冲突时参考）
_PARTIAL_STATUSES = {
    status for status, label in _STATUS_MAP.items() if label == "partial"
}


def _strict_cap_hit(value: object) -> Optional[bool]:
    """capHit 严格解析：只认布尔 True/False；None/缺失/其他类型一律 None（未知）。"""
    if isinstance(value, bool):
        return value
    return None


def assess_source_quality(key: str, source: Mapping[str, object]) -> Dict[str, object]:
    """单源质量判定。任何输入畸形都返回有效结果，绝不抛异常。"""
    if not isinstance(source, Mapping):
        return {
            "key": str(key), "platform": None, "name": None,
            "status": "unknown", "reason": "源数据非对象",
            "rawRows": None, "coverageDays": None,
            "capHit": None, "coverageStatus": None,
        }

    posts = source.get("posts")
    post_count = len(posts) if isinstance(posts, list) else 0
    if not isinstance(posts, list) or post_count == 0:
        status, reason = "empty", "无有效帖子列表"
    else:
        raw_status = source.get("coverageStatus")
        status_text = str(raw_status) if raw_status is not None else None
        mapped = _STATUS_MAP.get(status_text) if status_text else None
        cap_hit = _strict_cap_hit(source.get("capHit"))
        if mapped == "hot_feed":
            status, reason = "hot_feed", "非时序热帖流（设计属性，非截断）"
        elif mapped == "partial":
            status, reason = "partial", f"采集器确认截断：{status_text}"
        elif mapped == "full":
            if cap_hit is True:
                # capHit 是更强的截断证据，与 full 状态冲突时从严
                status, reason = "partial", f"capHit=True 与 {status_text} 冲突，从严按截断"
            else:
                status, reason = "full", status_text or "覆盖确认"
        elif cap_hit is True:
            status, reason = "partial", "capHit=True（触分页上限，截断）"
        elif status_text is None:
            status, reason = "unknown", "无 coverageStatus，质量未知"
        else:
            status, reason = "unknown", f"未登记状态 {status_text}，按未知处理"

    coverage_days = source.get("coverageDays")
    return {
        "key": str(key),
        "platform": source.get("platform"),
        "name": source.get("name") or source.get("forumName") or str(key),
        "status": status,
        "reason": reason,
        "rawRows": source.get("rawRows"),
        "coverageDays": list(coverage_days) if isinstance(coverage_days, list) else None,
        "capHit": _strict_cap_hit(source.get("capHit")),
        "coverageStatus": str(source.get("coverageStatus")) if source.get("coverageStatus") is not None else None,
    }


def build_quality_report(results: Mapping[str, object]) -> Dict[str, object]:
    """全源质量报告。任何单源异常不得拖垮整体。"""
    sources: Dict[str, Dict[str, object]] = {}
    counts = {"full": 0, "partial": 0, "hot_feed": 0, "unknown": 0, "empty": 0}
    if not isinstance(results, Mapping):
        results = {}
    for key, source in results.items():
        try:
            entry = assess_source_quality(key, source)
        except Exception as exc:  # 防御：质量诊断不得拖垮主流程
            entry = {
                "key": str(key), "platform": None, "name": str(key),
                "status": "unknown", "reason": f"质量判定异常：{exc}",
                "rawRows": None, "coverageDays": None,
                "capHit": None, "coverageStatus": None,
            }
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        sources[str(key)] = entry
    total = len(sources)
    summary = {
        "fullCount": counts["full"],
        "partialCount": counts["partial"],
        "hotFeedCount": counts["hot_feed"],
        "unknownCount": counts["unknown"],
        "emptyCount": counts["empty"],
        "totalSources": total,
    }
    assert summary["fullCount"] + summary["partialCount"] + summary["hotFeedCount"] + summary["unknownCount"] + summary["emptyCount"] == total
    return {"summary": summary, "sources": sources}


def format_console_lines(report: Mapping[str, object]) -> List[str]:
    """控制台诊断行：一行汇总 + 一行被标记源清单。"""
    summary = report.get("summary") or {}
    line = (
        f"[数据质量] 共{summary.get('totalSources', 0)}源："
        f"full {summary.get('fullCount', 0)} / "
        f"partial {summary.get('partialCount', 0)} / "
        f"hot_feed {summary.get('hotFeedCount', 0)} / "
        f"unknown {summary.get('unknownCount', 0)} / "
        f"empty {summary.get('emptyCount', 0)}"
    )
    flagged = [
        f"{entry.get('key')}({entry.get('status')})"
        for entry in (report.get("sources") or {}).values()
        if isinstance(entry, Mapping) and entry.get("status") in ("partial", "empty")
    ]
    lines = [line]
    if flagged:
        lines.append("[数据质量] 截断/空源 " + str(len(flagged)) + " 个：" + "、".join(flagged))
    return lines
