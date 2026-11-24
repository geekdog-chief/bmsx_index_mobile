"""板块社区热度 v3：把跨日绝对温度与当日板块相对位置分开。

这个模块只接收已经完成平台校准、去重和低质文本过滤的板块结果，
不读取行情，也不联网。正式宝妈/小白/散户分数仍由原模型计算；这里
只负责给“板块热度”一个更贴近社区声量差异、同时可解释的展示尺度。
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Mapping, MutableMapping, Sequence


HEAT_MODEL_VERSION = "sector_community_heat_v4_company_equal"

# 三交易日原始信号本身已经按 50% / 30% / 20% 保留了短期记忆。这里不再
# 额外用低 alpha 把旧日结果黏住；只保留宽松的极端变化安全阀，防止单个平台
# 瞬时异常把板块热度直接推到两端。
TEMPORAL_SMOOTHING = "three_trading_day_signal_with_safety_cap"
TEMPORAL_ALPHA = 1.0
TEMPORAL_RELATIVE_DAILY_CAP = 20.0
TEMPORAL_FINAL_DAILY_CAP = 25.0

# V4 使用公司等权后的真实讨论数量/独立参与者。情绪躁动仍保留，但只作为
# 声量证据的补充，避免几条激烈文本压过大规模普通讨论。
ABSOLUTE_ATTENTION_WEIGHT = 0.75
VOLUME_ACTIVITY_WEIGHT = 0.75
RELATIVE_VOLUME_WEIGHT = 0.85
FINAL_RELATIVE_WEIGHT = 0.65


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def round1(value: float) -> float:
    return round(clamp(value), 1)


def _number(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _quantile(values: Sequence[float], probability: float) -> float:
    """确定性的线性插值分位数；空序列返回 0。"""

    ordered = sorted(float(item) for item in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = clamp(probability, 0.0, 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _midrank_percentile(values: Sequence[float], value: float) -> float:
    if not values:
        return 0.5
    lower = sum(1 for candidate in values if candidate < value)
    equal = sum(1 for candidate in values if candidate == value)
    return (lower + 0.5 * max(equal, 1)) / len(values)


def _metric(item: Mapping[str, object], key: str, default: float = 0.0) -> float:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
    if key in metrics:
        return _number(metrics.get(key), default)
    return _number(item.get(key), default)


def _sector_reliability(item: Mapping[str, object]) -> float:
    """把样本、来源和平台覆盖折成 0~1 的收缩系数。

    指数使用较缓的幂次，避免一个维度稍弱就把其它真实证据全部抹掉；
    但单平台、单来源或极小样本仍会明显向 50 收缩。
    """

    metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
    fairness = metrics.get("fairness") if isinstance(metrics.get("fairness"), Mapping) else None
    if isinstance(fairness, Mapping):
        evidence = clamp(_number(fairness.get("reliabilityPercent"), 0.0) / 100.0, 0.0, 1.0)
        coverage = clamp(_number(fairness.get("weightedCompanyCoverage"), 0.0), 0.0, 1.0)
        stability = fairness.get("stabilityBand") if isinstance(fairness.get("stabilityBand"), Mapping) else {}
        max_delta = max(0.0, _number(stability.get("maxDelta"), 20.0))
        stability_quality = clamp(1.0 - max_delta / 20.0, 0.35, 1.0)
        return clamp(math.sqrt(evidence * coverage) * stability_quality, 0.0, 1.0)

    sample_posts = max(0.0, _metric(item, "samplePosts"))
    source_count = max(0.0, _metric(item, "sourceCount"))
    platform_count = int(max(0.0, _metric(item, "platformCount")))
    expected_sources = max(1.0, _number(item.get("expectedSourceCount"), 5.0))

    sample_quality = math.sqrt(sample_posts / (sample_posts + 40.0)) if sample_posts else 0.0
    source_quality = min(1.0, source_count / min(5.0, expected_sources))
    platform_quality = {3: 1.0, 2: 0.78, 1: 0.45}.get(platform_count, 0.0)
    reliability = (
        sample_quality ** 0.35
        * source_quality ** 0.30
        * platform_quality ** 0.35
    )
    return clamp(reliability, 0.0, 1.0)


def _rank_eligible(item: Mapping[str, object]) -> bool:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
    fairness = metrics.get("fairness") if isinstance(metrics.get("fairness"), Mapping) else None
    if isinstance(fairness, Mapping) and "rankEligible" in fairness:
        return bool(fairness.get("rankEligible"))
    return bool(item.get("available", True))


def _heat_inputs(item: Mapping[str, object]) -> Dict[str, float]:
    signals = item.get("signals") if isinstance(item.get("signals"), Mapping) else {}
    attention = _number(signals.get("attention"), 50.0)
    emotion_heat = _number(
        item.get("legacyHeat")
        if item.get("legacyHeat") is not None
        else signals.get("heat"),
        50.0,
    )
    activity_index = _metric(item, "activityIndex", attention)

    # absoluteHeat 可跨日阅读；relativeLatent 主要服务同日板块比较。
    # V4 把公司等权后的发帖速度、独立参与者和讨论数量作为主证据：相对潜变量中 85%
    # 来自声量因子，情绪躁动仅占 15%。
    absolute_heat = (
        ABSOLUTE_ATTENTION_WEIGHT * attention
        + (1.0 - ABSOLUTE_ATTENTION_WEIGHT) * emotion_heat
    )
    volume_factor = (
        VOLUME_ACTIVITY_WEIGHT * activity_index
        + (1.0 - VOLUME_ACTIVITY_WEIGHT) * attention
    )
    relative_latent = (
        RELATIVE_VOLUME_WEIGHT * volume_factor
        + (1.0 - RELATIVE_VOLUME_WEIGHT) * emotion_heat
    )
    return {
        "attention": attention,
        "emotionHeat": emotion_heat,
        "activityIndex": activity_index,
        "volumeFactor": volume_factor,
        "absoluteHeat": absolute_heat,
        "relativeLatent": relative_latent,
    }


def _heat_tier(relative_value: float, has_spread: bool = True) -> str:
    if not has_spread:
        return "差异不显著"
    if relative_value >= 70:
        return "头部热点"
    if relative_value >= 58:
        return "明显升温"
    if relative_value >= 42:
        return "市场中段"
    if relative_value >= 30:
        return "圈层偏窄"
    return "相对冷门"


def calibrate_sector_heats(
    sectors: Sequence[MutableMapping[str, object]],
    market_anchor: float | None = None,
) -> Dict[str, object]:
    """原地写入板块热度 v4，并返回当日截面审计摘要。

    ``absoluteHeat`` 保留跨日绝对温度；``relativeHeat`` 表示当日在可用
    板块中的稳健相对位置；``heat`` 以全市场温度为锚，把两者合并成页面
    主热度。原始情绪合成值保存在 ``legacyHeat``，不会丢失。
    """

    available: List[MutableMapping[str, object]] = [
        item for item in sectors
        if bool(item.get("available", True))
        and isinstance(item.get("signals"), Mapping)
    ]
    if not available:
        return {
            "version": HEAT_MODEL_VERSION,
            "status": "unavailable",
            "sectorCount": 0,
        }

    inputs = [_heat_inputs(item) for item in available]
    rankable = [item for item in available if _rank_eligible(item)]
    # A very small eligible set cannot define a useful cross-section.  Values
    # are still shown provisionally, but no hard rank is manufactured below.
    reference_items = rankable if len(rankable) >= 5 else available
    ranking_enabled = len(rankable) >= 5
    reference_inputs = [_heat_inputs(item) for item in reference_items]
    latent_values = [row["relativeLatent"] for row in reference_inputs]
    absolute_values = [row["absoluteHeat"] for row in reference_inputs]
    latent_median = float(statistics.median(latent_values))
    absolute_median = float(statistics.median(absolute_values))
    mad = float(statistics.median(abs(value - latent_median) for value in latent_values))
    iqr_scale = (_quantile(latent_values, 0.75) - _quantile(latent_values, 0.25)) / 1.349
    robust_scale = max(1.4826 * mad, iqr_scale, 2.0)
    p10 = _quantile(latent_values, 0.10)
    p90 = _quantile(latent_values, 0.90)
    p90_p10 = max(0.0, p90 - p10)

    noise_rows = []
    for item in reference_items:
        source_count = max(1.0, _metric(item, "sourceCount", 1.0))
        platform_count = max(1.0, _metric(item, "platformCount", 1.0))
        noise_rows.append((6.0 / math.sqrt(source_count)) * math.sqrt(3.0 / platform_count))
    noise_floor = float(statistics.median(noise_rows)) if noise_rows else 6.0
    # 差异没有明显超过抽样噪声时不靠硬排名造出大跨度。
    dispersion_gate = clamp((p90_p10 / max(noise_floor, 1e-9) - 1.0) / 1.5, 0.0, 1.0)
    if mad <= 1e-9 and p90_p10 <= 1e-9:
        dispersion_gate = 0.0

    anchor = clamp(
        market_anchor if market_anchor is not None else absolute_median,
        5.0,
        95.0,
    )
    staged: List[Dict[str, float]] = []
    for item, row in zip(available, inputs):
        reliability = _sector_reliability(item)
        robust_z = clamp(
            (row["relativeLatent"] - latent_median) / robust_scale,
            -3.0,
            3.0,
        )
        z_map = 50.0 + 32.0 * math.tanh(robust_z / 1.5)
        percentile = _midrank_percentile(latent_values, row["relativeLatent"])
        rank_map = 15.0 + 70.0 * percentile
        relative_heat = 50.0 + reliability * dispersion_gate * (
            0.75 * (z_map - 50.0) + 0.25 * (rank_map - 50.0)
        )
        final_heat = (
            anchor
            + FINAL_RELATIVE_WEIGHT * (relative_heat - 50.0)
            + (1.0 - FINAL_RELATIVE_WEIGHT) * reliability
            * (row["absoluteHeat"] - absolute_median)
        )
        staged.append({
            **row,
            "reliability": reliability,
            "robustZ": robust_z,
            "percentile": percentile,
            "relativeHeat": relative_heat,
            "finalHeat": clamp(final_heat, 5.0, 95.0),
        })

    eligible_indices = [index for index, item in enumerate(available) if _rank_eligible(item)]
    order = sorted(
        eligible_indices,
        key=lambda index: (staged[index]["finalHeat"], staged[index]["relativeLatent"]),
        reverse=True,
    )
    ranks = {index: position + 1 for position, index in enumerate(order)}
    for index, (item, row) in enumerate(zip(available, staged)):
        legacy = round1(row["emotionHeat"])
        relative_value = round1(row["relativeHeat"])
        rank_eligible = _rank_eligible(item)
        heat_tier = (
            _heat_tier(relative_value, dispersion_gate > 0 and ranking_enabled)
            if rank_eligible else "数据不足，暂不硬排名"
        )
        item["legacyHeat"] = legacy
        item["absoluteHeat"] = round1(row["absoluteHeat"])
        item["relativeHeat"] = relative_value
        item["heat"] = round1(row["finalHeat"])
        item["heatRank"] = (
            ranks.get(index)
            if dispersion_gate > 0 and ranking_enabled and rank_eligible else None
        )
        item["heatTier"] = heat_tier
        metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else None
        if isinstance(metrics, MutableMapping):
            metrics["heatReliability"] = round(row["reliability"] * 100.0, 1)
        item["heatModel"] = {
            "version": HEAT_MODEL_VERSION,
            "status": (
                "applied" if dispersion_gate > 0 and ranking_enabled and rank_eligible
                else "provisional_not_ranked" if not rank_eligible
                else "no_distinguishable_spread"
            ),
            "marketAnchor": round1(anchor),
            "legacyEmotionHeat": legacy,
            "absoluteHeat": round1(row["absoluteHeat"]),
            "activityIndex": round1(row["activityIndex"]),
            "volumeFactor": round1(row["volumeFactor"]),
            "relativeLatent": round1(row["relativeLatent"]),
            "relativeHeat": round1(row["relativeHeat"]),
            "heatTier": heat_tier,
            "percentile": round(row["percentile"] * 100.0, 1),
            "robustZ": round(row["robustZ"], 3),
            "reliability": round(row["reliability"] * 100.0, 1),
            "rankEligible": rank_eligible,
            "dispersionGate": round(dispersion_gate, 3),
            "crossSectionMedian": round(latent_median, 3),
            "crossSectionMAD": round(mad, 3),
            "crossSectionP90P10": round(p90_p10, 3),
            "noiseFloor": round(noise_floor, 3),
            "usesMarketPrice": False,
            "formalPersonaScoresChanged": False,
            "weights": {
                "absoluteAttention": ABSOLUTE_ATTENTION_WEIGHT,
                "absoluteEmotion": round(1.0 - ABSOLUTE_ATTENTION_WEIGHT, 2),
                "volumeActivity": VOLUME_ACTIVITY_WEIGHT,
                "volumeAttention": round(1.0 - VOLUME_ACTIVITY_WEIGHT, 2),
                "relativeVolume": RELATIVE_VOLUME_WEIGHT,
                "relativeEmotion": round(1.0 - RELATIVE_VOLUME_WEIGHT, 2),
                "finalRelative": FINAL_RELATIVE_WEIGHT,
                "finalAbsolute": round(1.0 - FINAL_RELATIVE_WEIGHT, 2),
            },
        }

    for item in sectors:
        if item in available:
            continue
        signals = item.get("signals") if isinstance(item.get("signals"), Mapping) else {}
        legacy = round1(_number(signals.get("heat"), 50.0))
        item["legacyHeat"] = legacy
        item["absoluteHeat"] = legacy
        item["relativeHeat"] = 50.0
        item["heat"] = legacy
        item["heatRank"] = None
        item["heatTier"] = "数据不足"
        item["heatModel"] = {
            "version": HEAT_MODEL_VERSION,
            "status": "unavailable",
            "usesMarketPrice": False,
            "formalPersonaScoresChanged": False,
        }

    final_values = [float(item["heat"]) for item in available]
    relative_values = [float(item["relativeHeat"]) for item in available]
    return {
        "version": HEAT_MODEL_VERSION,
        "status": "applied" if dispersion_gate > 0 else "no_distinguishable_spread",
        "sectorCount": len(available),
        "rankEligibleSectorCount": len(rankable),
        "rankedSectorCount": len(ranks) if dispersion_gate > 0 and ranking_enabled else 0,
        "marketAnchor": round1(anchor),
        "latentMedian": round(latent_median, 3),
        "absoluteMedian": round(absolute_median, 3),
        "crossSectionMAD": round(mad, 3),
        "crossSectionP90P10": round(p90_p10, 3),
        "noiseFloor": round(noise_floor, 3),
        "dispersionGate": round(dispersion_gate, 3),
        "relativeRange": round(max(relative_values) - min(relative_values), 1),
        "finalRange": round(max(final_values) - min(final_values), 1),
        "usesMarketPrice": False,
        "formalPersonaScoresChanged": False,
    }


def stabilize_sector_heats(
    sectors: Sequence[MutableMapping[str, object]],
    prior_sectors: Sequence[Mapping[str, object]],
    market_anchor: float | None = None,
    gap_days: int = 1,
) -> Dict[str, object]:
    """用且只用前一日结果稳定板块热度，不改正式指数或原始信号。

    三交易日输入已经按 50% / 30% / 20% 形成短期记忆，因此这里 alpha=1，
    不再额外平均旧结果，只以 ±20/±25 作为极端情况下的安全阀。全市场锚
    的变化会原样传递，因此不会把普涨或普冷误当作板块排名波动。
    """

    prior_by_key = {
        str(item.get("key")): item
        for item in prior_sectors
        if isinstance(item, Mapping) and item.get("key")
    }
    available = [
        item for item in sectors
        if bool(item.get("available", True)) and item.get("key")
    ]
    if not available or not prior_by_key:
        return {
            "temporalSmoothing": "not_applied_no_prior_day",
            "temporalAlpha": TEMPORAL_ALPHA,
            "stabilizedSectorCount": 0,
        }

    gap = max(1, int(gap_days))
    effective_alpha = 1.0 - (1.0 - TEMPORAL_ALPHA) ** gap
    relative_cap = TEMPORAL_RELATIVE_DAILY_CAP * gap
    final_cap = TEMPORAL_FINAL_DAILY_CAP * gap
    anchor = clamp(
        market_anchor
        if market_anchor is not None
        else _number((available[0].get("heatModel") or {}).get("marketAnchor"), 50.0),
        5.0,
        95.0,
    )
    stabilized = 0
    cap_relative_hits = 0
    cap_final_hits = 0
    for item in available:
        key = str(item.get("key"))
        prior = prior_by_key.get(key)
        if prior is None:
            continue
        model_value = item.get("heatModel")
        model = dict(model_value) if isinstance(model_value, Mapping) else {}
        prior_model = prior.get("heatModel") if isinstance(prior.get("heatModel"), Mapping) else {}
        raw_relative = _number(model.get("rawRelativeHeat"), _number(item.get("relativeHeat"), 50.0))
        raw_final = _number(model.get("rawFinalHeat"), _number(item.get("heat"), anchor))
        raw_absolute = _number(model.get("rawAbsoluteHeat"), _number(item.get("absoluteHeat"), 50.0))
        prior_relative = _number(prior.get("relativeHeat"), 50.0)
        prior_final = _number(prior.get("heat"), anchor)
        prior_anchor = _number(prior_model.get("marketAnchor"), anchor)

        unconstrained_relative_delta = effective_alpha * (raw_relative - prior_relative)
        relative_delta = clamp(
            unconstrained_relative_delta,
            -relative_cap,
            relative_cap,
        )
        cap_relative_hits += int(abs(unconstrained_relative_delta) > relative_cap)
        smoothed_relative = clamp(prior_relative + relative_delta)

        # 只替换最终值中的相对热度分量，绝对热度仍保留当天原始信息。
        candidate_final = raw_final + FINAL_RELATIVE_WEIGHT * (
            smoothed_relative - raw_relative
        )
        prior_centered = prior_final + (anchor - prior_anchor)
        final_delta = candidate_final - prior_centered
        constrained_final_delta = clamp(
            final_delta,
            -final_cap,
            final_cap,
        )
        cap_final_hits += int(abs(final_delta) > final_cap)
        smoothed_final = clamp(prior_centered + constrained_final_delta, 5.0, 95.0)

        relative_value = round1(smoothed_relative)
        item["relativeHeat"] = relative_value
        item["heat"] = round1(smoothed_final)
        item["heatTier"] = _heat_tier(relative_value, True)
        model.update({
            "rawRelativeHeat": round1(raw_relative),
            "rawFinalHeat": round1(raw_final),
            "rawAbsoluteHeat": round1(raw_absolute),
            "relativeHeat": relative_value,
            "heatTier": item["heatTier"],
            "marketAnchor": round1(anchor),
            "temporalSmoothing": TEMPORAL_SMOOTHING,
            "temporalAlpha": TEMPORAL_ALPHA,
            "temporalGapDays": gap,
            "temporalEffectiveAlpha": round(effective_alpha, 6),
            "relativeDailyCap": TEMPORAL_RELATIVE_DAILY_CAP,
            "finalDailyCap": TEMPORAL_FINAL_DAILY_CAP,
            "previousRelativeHeat": round1(prior_relative),
            "previousFinalHeat": round1(prior_final),
        })
        item["heatModel"] = model
        stabilized += 1

    order = sorted(
        (
            item for item in available
            if item.get("heat") is not None and _rank_eligible(item)
        ),
        key=lambda item: (_number(item.get("heat")), _number(item.get("relativeHeat"))),
        reverse=True,
    )
    final_values = [_number(item.get("heat")) for item in order]
    relative_values = [_number(item.get("relativeHeat")) for item in order]
    has_spread = bool(
        len(order) >= 5
        and (
            max(final_values) - min(final_values) > 1e-9
            or max(relative_values) - min(relative_values) > 1e-9
        )
    )
    for item in available:
        if _rank_eligible(item):
            continue
        item["heatRank"] = None
        item["heatTier"] = "数据不足，暂不硬排名"
        model_value = item.get("heatModel")
        if isinstance(model_value, MutableMapping):
            model_value["status"] = "provisional_not_ranked"
            model_value["rankEligible"] = False
            model_value["heatTier"] = "数据不足，暂不硬排名"
    for rank, item in enumerate(order, 1):
        item["heatRank"] = rank if has_spread else None
        if not has_spread:
            item["heatTier"] = "差异不显著"
            model_value = item.get("heatModel")
            if isinstance(model_value, MutableMapping):
                model_value["heatTier"] = "差异不显著"
    return {
        "temporalSmoothing": TEMPORAL_SMOOTHING,
        "temporalAlpha": TEMPORAL_ALPHA,
        "temporalGapDays": gap,
        "temporalEffectiveAlpha": round(effective_alpha, 6),
        "relativeDailyCap": TEMPORAL_RELATIVE_DAILY_CAP,
        "finalDailyCap": TEMPORAL_FINAL_DAILY_CAP,
        "stabilizedSectorCount": stabilized,
        "relativeCapHitCount": cap_relative_hits,
        "finalCapHitCount": cap_final_hits,
        "relativeRange": round(max(relative_values) - min(relative_values), 1) if relative_values else 0.0,
        "finalRange": round(max(final_values) - min(final_values), 1) if final_values else 0.0,
        "usesFutureDates": False,
        "usesMarketPrice": False,
        "formalPersonaScoresChanged": False,
    }
