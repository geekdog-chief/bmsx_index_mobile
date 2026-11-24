"""Company-first aggregation for the community sentiment index.

This module deliberately contains no I/O and imports no project scoring code.  It
is therefore safe to exercise with synthetic observations before it is wired into
``update_index.py``.

Canonical input
===============

``aggregate_sector_company_first`` accepts three company rows.  Each row contains
platform rows and each platform contains one row for every requested trading day::

    {
        "key": "company-a",
        "platforms": [
            {
                "key": "eastmoney",
                "days": {
                    "2026-08-06": {
                        "status": "observed",
                        "sampleCount": 24,
                        "signals": {
                            "optimism": 58, "fomo": 52, "panic": 10,
                            "newcomer": 49, "intensity": 55,
                            "attention": 63,
                        },
                        "volume": 18,
                    },
                    "2026-08-05": {"status": "verified_zero"},
                    "2026-08-04": {"status": "unobserved"},
                },
            }
        ],
    }

``verified_zero`` means the collector proved that the stream was successfully
read and contained no qualifying post.  ``unobserved`` means that no conclusion
can be drawn (fetch failure, pagination did not reach the day, or unknown stream
completeness).  The two states are never silently conflated.

The aggregation order is platform -> company -> sector for each trading day,
then exactly 50/30/20 across the three days.  Missing evidence is neutralised and
reported as lower reliability; day weights are never renormalised.  Thus missing
data cannot acquire the weight of the evidence that happened to remain.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple


MODEL_VERSION = "company_first_fairness_v1"
DEFAULT_DAY_WEIGHTS: Tuple[float, float, float] = (0.50, 0.30, 0.20)
EXPECTED_COMPANY_COUNT = 3
COMPANY_INFLUENCE_CAP = 0.40
THS_EMOTION_CAP = 0.15
PLATFORM_SAMPLE_PRIOR = 12.0

EMOTION_KEYS: Tuple[str, ...] = (
    "optimism",
    "fomo",
    "panic",
    "newcomer",
    "intensity",
)
SIGNAL_KEYS: Tuple[str, ...] = (*EMOTION_KEYS, "attention")
SIGNAL_BASELINES: Dict[str, float] = {
    "optimism": 50.0,
    "fomo": 50.0,
    "panic": 0.0,
    "newcomer": 50.0,
    "intensity": 50.0,
    "attention": 50.0,
}
VALID_STATUSES = {"observed", "verified_zero", "unobserved"}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _round1(value: float) -> float:
    return round(_clamp(value), 1)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _normalise_day(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError("trading day cannot be empty")
    # Validate the common ISO form while retaining a plain, JSON-friendly key.
    return date.fromisoformat(text).isoformat()


def _normalise_status(row: Mapping[str, object] | None) -> str:
    if row is None:
        return "unobserved"
    raw = str(row.get("status") or "").strip().lower().replace("-", "_")
    aliases = {
        "zero": "verified_zero",
        "verifiedzero": "verified_zero",
        "missing": "unobserved",
        "unknown": "unobserved",
        "available": "observed",
    }
    raw = aliases.get(raw, raw)
    if not raw:
        has_signals = isinstance(row.get("signals"), Mapping) and bool(row.get("signals"))
        has_samples = _safe_float(row.get("sampleCount"), 0.0) > 0
        raw = "observed" if has_signals or has_samples else "unobserved"
    if raw not in VALID_STATUSES:
        raise ValueError(f"invalid observation status: {raw!r}")
    return raw


def _platform_key(row: Mapping[str, object]) -> str:
    return str(row.get("key") or row.get("platform") or "unknown").strip().lower()


def _platform_days(row: Mapping[str, object]) -> Mapping[str, object]:
    value = row.get("days") if isinstance(row.get("days"), Mapping) else row.get("daily")
    return value if isinstance(value, Mapping) else {}


def _company_platforms(row: Mapping[str, object]) -> List[Mapping[str, object]]:
    value = row.get("platforms")
    if isinstance(value, Mapping):
        result: List[Mapping[str, object]] = []
        for key, platform in value.items():
            if isinstance(platform, Mapping):
                result.append({"key": key, **platform})
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _sample_precision(sample_count: float, prior: float = PLATFORM_SAMPLE_PRIOR) -> float:
    """Bounded empirical precision used only for platform mixing.

    The saturating curve prevents a 100-post page from receiving five times the
    weight of a 20-post page, while one post still receives only 1/13 of one unit
    of platform evidence.  This is intentionally not a minimum platform weight.
    """

    sample_count = max(0.0, sample_count)
    return sample_count / (sample_count + max(prior, 1e-9))


def _capped_normalise(
    raw_weights: Sequence[float],
    caps: Sequence[float],
    *,
    target: float = 1.0,
) -> List[float]:
    """Normalise non-negative weights subject to individual upper bounds.

    If the available caps cannot sum to ``target`` (for example THS is the only
    source and is capped at 15%), the returned weights deliberately sum to less
    than one.  The residual is neutral mass, not a hidden re-normalisation.
    """

    if len(raw_weights) != len(caps):
        raise ValueError("raw_weights and caps must have equal length")
    if not raw_weights:
        return []
    safe_caps = [max(0.0, min(1.0, _safe_float(cap))) for cap in caps]
    target = max(0.0, min(_safe_float(target), sum(safe_caps)))
    result = [0.0] * len(raw_weights)
    remaining = set(range(len(raw_weights)))
    remaining_target = target
    while remaining and remaining_target > 1e-12:
        total_raw = sum(max(_safe_float(raw_weights[index]), 1e-12) for index in remaining)
        capped: List[int] = []
        for index in sorted(remaining):
            proposed = remaining_target * max(_safe_float(raw_weights[index]), 1e-12) / total_raw
            if proposed >= safe_caps[index] - 1e-12:
                result[index] = safe_caps[index]
                capped.append(index)
        if not capped:
            for index in sorted(remaining):
                result[index] = (
                    remaining_target
                    * max(_safe_float(raw_weights[index]), 1e-12)
                    / total_raw
                )
            break
        for index in capped:
            remaining.remove(index)
            remaining_target = max(0.0, remaining_target - result[index])
    return result


def _platform_day(
    platform: Mapping[str, object],
    day: str,
) -> Dict[str, object]:
    days = _platform_days(platform)
    raw = days.get(day)
    row = raw if isinstance(raw, Mapping) else None
    status = _normalise_status(row)
    signals = row.get("signals") if isinstance(row, Mapping) and isinstance(row.get("signals"), Mapping) else {}
    sample_count = max(0, int(_safe_float(row.get("sampleCount"), 0.0))) if row else 0
    if status == "observed" and sample_count == 0 and signals:
        # A signal with no declared count is still one piece of evidence, not
        # infinitely precise and not silently discarded.
        sample_count = 1
    volume_value = None
    if row and status in {"observed", "verified_zero"}:
        for key in ("volume", "equivalentPosts", "postCount"):
            if row.get(key) is not None:
                volume_value = max(0.0, _safe_float(row.get(key)))
                break
        if status == "verified_zero" and volume_value is None:
            volume_value = 0.0
    return {
        "platform": _platform_key(platform),
        "status": status,
        "sampleCount": sample_count,
        "signals": {
            key: _clamp(_safe_float(signals.get(key), SIGNAL_BASELINES[key]))
            for key in SIGNAL_KEYS
            if signals.get(key) is not None
        },
        "volume": volume_value,
    }


def _company_day(
    company: Mapping[str, object],
    day: str,
    *,
    ths_emotion_cap: float,
    flags: set[str],
) -> Dict[str, object]:
    platform_rows = [_platform_day(platform, day) for platform in _company_platforms(company)]
    emotion_rows = [
        row for row in platform_rows
        if row["status"] == "observed"
        and row["sampleCount"] > 0
        and any(key in row["signals"] for key in EMOTION_KEYS)
    ]
    emotion_raw_weights = [_sample_precision(float(row["sampleCount"])) for row in emotion_rows]
    emotion_caps = [ths_emotion_cap if row["platform"] == "ths" else 1.0 for row in emotion_rows]
    emotion_weights = _capped_normalise(emotion_raw_weights, emotion_caps)
    emotion_mass = sum(emotion_weights)
    if any(
        row["platform"] == "ths" and weight >= ths_emotion_cap - 1e-9
        for row, weight in zip(emotion_rows, emotion_weights)
    ):
        flags.add("ths_emotion_capped")
    if any(row["platform"] == "ths" for row in platform_rows):
        flags.add("ths_excluded_from_volume_attention")

    # Attention is deliberately recomputed without THS hot_feed evidence.
    attention_rows = [
        row for row in platform_rows
        if row["platform"] != "ths"
        and row["status"] == "observed"
        and row["sampleCount"] > 0
        and "attention" in row["signals"]
    ]
    attention_raw_weights = [_sample_precision(float(row["sampleCount"])) for row in attention_rows]
    attention_weights = _capped_normalise(attention_raw_weights, [1.0] * len(attention_rows))

    # Volumes are company-level platform averages, never sums.  This prevents a
    # company with more configured websites from mechanically looking hotter.
    volume_rows = [
        row for row in platform_rows
        if row["platform"] != "ths"
        and row["status"] in {"observed", "verified_zero"}
        and row["volume"] is not None
    ]
    volume = (
        sum(float(row["volume"]) for row in volume_rows) / len(volume_rows)
        if volume_rows else None
    )

    signals: Dict[str, float] = {}
    signal_reliability: Dict[str, float] = {}
    signal_point_mass: Dict[str, float] = {}
    for key in EMOTION_KEYS:
        eligible = [
            (row, weight) for row, weight in zip(emotion_rows, emotion_weights)
            if key in row["signals"] and weight > 0
        ]
        mass = sum(weight for _, weight in eligible)
        if mass <= 0:
            continue
        value = sum(weight * float(row["signals"][key]) for row, weight in eligible) / mass
        effective_n = sum(weight * float(row["sampleCount"]) for row, weight in eligible) / mass
        signals[key] = _clamp(value)
        # Point mass is normally 1.0.  Sample size affects uncertainty, not the
        # formal point estimate.  It can be below one only when the available
        # platform caps themselves cannot provide a full vote (THS-only = 15%).
        signal_point_mass[key] = mass
        signal_reliability[key] = mass * _sample_precision(effective_n)

    if attention_rows and sum(attention_weights) > 0:
        mass = sum(attention_weights)
        signals["attention"] = _clamp(sum(
            weight * float(row["signals"]["attention"])
            for row, weight in zip(attention_rows, attention_weights)
        ) / mass)
        effective_n = sum(
            weight * float(row["sampleCount"])
            for row, weight in zip(attention_rows, attention_weights)
        ) / mass
        signal_point_mass["attention"] = mass
        signal_reliability["attention"] = mass * _sample_precision(effective_n)

    for row, weight in zip(emotion_rows, emotion_weights):
        if row["sampleCount"] <= 2 and weight < 0.20:
            flags.add("tiny_platform_downweighted")

    platform_diagnostics = []
    emotion_weight_by_id = {id(row): weight for row, weight in zip(emotion_rows, emotion_weights)}
    attention_weight_by_id = {id(row): weight for row, weight in zip(attention_rows, attention_weights)}
    for row in platform_rows:
        platform_diagnostics.append({
            "platform": row["platform"],
            "status": row["status"],
            "sampleCount": row["sampleCount"],
            "emotionWeight": round(emotion_weight_by_id.get(id(row), 0.0), 6),
            "attentionWeight": round(attention_weight_by_id.get(id(row), 0.0), 6),
            "volumeEligible": row["platform"] != "ths" and row["volume"] is not None,
        })

    statuses = [str(row["status"]) for row in platform_rows]
    if "observed" in statuses:
        status = "observed"
    elif statuses and all(item == "verified_zero" for item in statuses):
        status = "verified_zero"
    else:
        status = "unobserved"
    return {
        "status": status,
        "signals": signals,
        "signalPointMass": signal_point_mass,
        "signalReliability": signal_reliability,
        "volume": volume,
        "sampleCount": sum(int(row["sampleCount"]) for row in emotion_rows),
        "platforms": platform_diagnostics,
        "emotionRows": [
            {
                "platform": row["platform"],
                "sampleCount": row["sampleCount"],
                "weight": weight,
            }
            for row, weight in zip(emotion_rows, emotion_weights)
            if weight > 0
        ],
    }


def _kish_effective_sample(leaves: Iterable[Tuple[float, int]]) -> float:
    usable = [(max(0.0, weight), max(0, count)) for weight, count in leaves if weight > 0 and count > 0]
    if not usable:
        return 0.0
    total_weight = sum(weight for weight, _ in usable)
    denominator = sum((weight * weight) / count for weight, count in usable)
    return (total_weight * total_weight / denominator) if denominator > 0 else 0.0


def score_personas(signals: Mapping[str, float], breadth: float) -> Dict[str, int]:
    """Keep the public persona-score contract while avoiding an import cycle."""

    optimism = float(signals.get("optimism", 50.0))
    fomo = float(signals.get("fomo", 50.0))
    newcomer = float(signals.get("newcomer", 50.0))
    intensity = float(signals.get("intensity", 50.0))
    attention = float(signals.get("attention", 50.0))
    baoma = (
        optimism * 0.25 + fomo * 0.25 + newcomer * 0.15
        + intensity * 0.15 + attention * 0.15 + breadth * 0.05
    )
    novice = (
        optimism * 0.15 + fomo * 0.20 + newcomer * 0.30
        + intensity * 0.15 + attention * 0.15 + breadth * 0.05
    )
    retail = (
        optimism * 0.30 + fomo * 0.20 + newcomer * 0.05
        + intensity * 0.15 + attention * 0.20 + breadth * 0.10
    )
    result = {
        "baoma": int(round(_clamp(baoma))),
        "novice": int(round(_clamp(novice))),
        "retail": int(round(_clamp(retail))),
    }
    result["composite"] = int(round(sum(result.values()) / 3.0))
    return result


def aggregate_sector_company_first(
    companies: Sequence[Mapping[str, object]],
    trading_days: Sequence[object],
    *,
    day_weights: Sequence[float] = DEFAULT_DAY_WEIGHTS,
    expected_company_count: int = EXPECTED_COMPANY_COUNT,
    company_influence_cap: float = COMPANY_INFLUENCE_CAP,
    ths_emotion_cap: float = THS_EMOTION_CAP,
) -> Dict[str, object]:
    """Aggregate a sector without giving a multiply-sourced company extra votes.

    ``company_influence_cap`` applies to the final index coefficient, rather than
    to the conditional evidence share.  With two observed companies the evidence
    layer is 50/50, but each still contributes at most its fixed one-third seat;
    the missing third is neutral and lowers reliability.
    """

    days = [_normalise_day(day) for day in trading_days]
    if len(days) != len(day_weights) or not days:
        raise ValueError("trading_days and day_weights must have the same non-zero length")
    if len(set(days)) != len(days):
        raise ValueError("trading_days must be unique")
    raw_day_weights = [max(0.0, _safe_float(weight)) for weight in day_weights]
    weight_total = sum(raw_day_weights)
    if weight_total <= 0:
        raise ValueError("day_weights must contain positive mass")
    weights = [weight / weight_total for weight in raw_day_weights]
    if expected_company_count <= 0:
        raise ValueError("expected_company_count must be positive")
    if not 0 < company_influence_cap <= 1:
        raise ValueError("company_influence_cap must be in (0, 1]")
    if (1.0 / expected_company_count) > company_influence_cap + 1e-12:
        raise ValueError("the requested company cap is impossible for expected_company_count")
    if not 0 <= ths_emotion_cap <= 1:
        raise ValueError("ths_emotion_cap must be in [0, 1]")

    ordered_companies = sorted(
        [company for company in companies if isinstance(company, Mapping)],
        key=lambda item: str(item.get("key") or item.get("code") or item.get("name") or ""),
    )
    company_keys = [
        str(company.get("key") or company.get("code") or company.get("name") or f"company-{index}")
        for index, company in enumerate(ordered_companies)
    ]
    if len(set(company_keys)) != len(company_keys):
        raise ValueError("company keys must be unique")
    if len(ordered_companies) > expected_company_count:
        raise ValueError("more company rows supplied than expected_company_count")

    flags: set[str] = set()
    if len(ordered_companies) < expected_company_count:
        flags.add("missing_companies")
    company_day_rows: Dict[str, Dict[str, Dict[str, object]]] = {}
    for company_key, company in zip(company_keys, ordered_companies):
        company_day_rows[company_key] = {
            day: _company_day(
                company,
                day,
                ths_emotion_cap=ths_emotion_cap,
                flags=flags,
            )
            for day in days
        }

    final_signals = dict(SIGNAL_BASELINES)
    evidence_signals: Dict[str, float] = {}
    signal_reliability: Dict[str, float] = {}
    daily_diagnostics: List[Dict[str, object]] = []
    leaf_samples: List[Tuple[float, int]] = []
    company_effective = defaultdict(float)
    company_reliability_effective = defaultdict(float)
    company_available_day_mass = defaultdict(float)

    # First produce company-first sector values for each day; only afterwards do
    # the fixed day weights enter the calculation.
    day_signal_values: Dict[str, Dict[str, float]] = {}
    day_signal_reliability: Dict[str, Dict[str, float]] = {}
    for day, day_weight in zip(days, weights):
        values: Dict[str, float] = {}
        reliabilities: Dict[str, float] = {}
        observed_company_keys = [
            key for key in company_keys
            if company_day_rows[key][day]["status"] == "observed"
        ]
        verified_zero_keys = [
            key for key in company_keys
            if company_day_rows[key][day]["status"] == "verified_zero"
        ]
        if not observed_company_keys:
            flags.add("unobserved_day")
        if verified_zero_keys:
            flags.add("verified_zero_day")

        for key in SIGNAL_KEYS:
            available = [
                company_key for company_key in company_keys
                if key in company_day_rows[company_key][day]["signals"]
            ]
            conditional_weight = 1.0 / len(available) if available else 0.0
            evidence_value = (
                sum(float(company_day_rows[company_key][day]["signals"][key]) for company_key in available)
                / len(available)
                if available else SIGNAL_BASELINES[key]
            )
            # Every expected company owns one fixed seat.  A weak/missing seat is
            # pulled to the neutral baseline instead of being donated to company 1.
            deviation = 0.0
            reliability_mass = 0.0
            for company_key in available:
                point_mass = _clamp(
                    float(company_day_rows[company_key][day]["signalPointMass"].get(key, 0.0)),
                    0.0,
                    1.0,
                )
                company_reliability = _clamp(
                    float(company_day_rows[company_key][day]["signalReliability"].get(key, 0.0)),
                    0.0,
                    1.0,
                )
                point_company_weight = point_mass / expected_company_count
                reliable_company_weight = company_reliability / expected_company_count
                deviation += point_company_weight * (
                    float(company_day_rows[company_key][day]["signals"][key]) - SIGNAL_BASELINES[key]
                )
                reliability_mass += reliable_company_weight
                if key == "optimism":
                    company_effective[company_key] += day_weight * point_company_weight
                    company_reliability_effective[company_key] += day_weight * reliable_company_weight
                    company_available_day_mass[company_key] += day_weight
                    emotion_row_by_platform = company_day_rows[company_key][day]["emotionRows"]
                    for platform_row in emotion_row_by_platform:
                        leaf_weight = (
                            day_weight
                            * (1.0 / expected_company_count)
                            * float(platform_row["weight"])
                        )
                        leaf_samples.append((leaf_weight, int(platform_row["sampleCount"])))
            values[key] = _clamp(SIGNAL_BASELINES[key] + deviation)
            reliabilities[key] = min(1.0, reliability_mass)
            if available:
                evidence_signals[key] = evidence_signals.get(key, 0.0) + day_weight * evidence_value
            else:
                evidence_signals[key] = evidence_signals.get(key, 0.0) + day_weight * SIGNAL_BASELINES[key]

            # Conditional evidence may be 50/50 when one company is missing, but
            # final influence remains at most one expected seat (normally 1/3).
            if conditional_weight > company_influence_cap + 1e-12:
                flags.add("conditional_company_concentration_high")

        day_signal_values[day] = values
        day_signal_reliability[day] = reliabilities

        volume_available = [
            key for key in company_keys
            if company_day_rows[key][day]["volume"] is not None
        ]
        volume_value = (
            sum(float(company_day_rows[key][day]["volume"]) for key in volume_available)
            / len(volume_available)
            if volume_available else None
        )
        daily_diagnostics.append({
            "date": day,
            "weight": round(day_weight, 6),
            "status": (
                "observed" if observed_company_keys
                else "verified_zero" if verified_zero_keys
                else "unobserved"
            ),
            "observedCompanyCount": len(observed_company_keys),
            "verifiedZeroCompanyCount": len(verified_zero_keys),
            "unobservedCompanyCount": expected_company_count - len(observed_company_keys) - len(verified_zero_keys),
            "companyCoverage": round(len(observed_company_keys) / expected_company_count, 6),
                    "signalReliability": {
                        key: round(reliabilities[key], 6) for key in SIGNAL_KEYS
                    },
                    "signalPointCoverage": {
                        key: round(sum(
                            float(company_day_rows[company_key][day]["signalPointMass"].get(key, 0.0))
                            for company_key in company_keys
                        ) / expected_company_count, 6)
                        for key in SIGNAL_KEYS
                    },
            "volume": round(volume_value, 6) if volume_value is not None else None,
            "volumeCompanyCoverage": round(len(volume_available) / expected_company_count, 6),
        })

    for key in SIGNAL_KEYS:
        final_signals[key] = _round1(sum(
            day_weight * day_signal_values[day][key]
            for day, day_weight in zip(days, weights)
        ))
        signal_reliability[key] = sum(
            day_weight * day_signal_reliability[day][key]
            for day, day_weight in zip(days, weights)
        )
        evidence_signals[key] = _round1(evidence_signals.get(key, SIGNAL_BASELINES[key]))

    final_signals["heat"] = _round1(
        final_signals["fomo"] * 0.30
        + final_signals["newcomer"] * 0.15
        + final_signals["intensity"] * 0.25
        + final_signals["attention"] * 0.30
    )
    evidence_signals["heat"] = _round1(
        evidence_signals["fomo"] * 0.30
        + evidence_signals["newcomer"] * 0.15
        + evidence_signals["intensity"] * 0.25
        + evidence_signals["attention"] * 0.30
    )

    # Breadth is also company-equal.  Missing companies are exactly neutral.
    company_heats: Dict[str, float] = {}
    for company_key in company_keys:
        company_signals: Dict[str, float] = {}
        for key in SIGNAL_KEYS:
            company_signals[key] = sum(
                day_weight * (
                    SIGNAL_BASELINES[key]
                    + float(company_day_rows[company_key][day]["signalPointMass"].get(key, 0.0))
                    * (
                        float(company_day_rows[company_key][day]["signals"].get(key, SIGNAL_BASELINES[key]))
                        - SIGNAL_BASELINES[key]
                    )
                )
                for day, day_weight in zip(days, weights)
            )
        company_heats[company_key] = (
            company_signals["fomo"] * 0.30
            + company_signals["newcomer"] * 0.15
            + company_signals["intensity"] * 0.25
            + company_signals["attention"] * 0.30
        )
    hot = sum(1 for value in company_heats.values() if value > 50.0)
    cold = sum(1 for value in company_heats.values() if value < 50.0)
    breadth = _round1(50.0 + 50.0 * (hot - cold) / expected_company_count)

    weighted_volume_lower_bound = 0.0
    volume_coverage = 0.0
    for row in daily_diagnostics:
        if row["volume"] is None:
            continue
        coverage = float(row["volumeCompanyCoverage"])
        weighted_volume_lower_bound += float(row["weight"]) * coverage * float(row["volume"])
        volume_coverage += float(row["weight"]) * coverage
    coverage_adjusted_volume = (
        weighted_volume_lower_bound / volume_coverage if volume_coverage > 0 else None
    )

    effective_sample = _kish_effective_sample(leaf_samples)
    overall_reliability = sum(signal_reliability[key] for key in SIGNAL_KEYS) / len(SIGNAL_KEYS)
    if overall_reliability < 0.60:
        flags.add("low_reliability")
    if effective_sample < 20:
        flags.add("low_effective_sample")
    if any(row["status"] == "unobserved" for row in daily_diagnostics):
        flags.add("unobserved_day")

    company_weights = []
    for company_key in company_keys:
        effective = company_effective.get(company_key, 0.0)
        if effective > company_influence_cap + 1e-9:
            flags.add("company_influence_cap_breached")
        company_weights.append({
            "key": company_key,
            "targetWeight": round(1.0 / expected_company_count, 6),
            "availableDayWeight": round(company_available_day_mass.get(company_key, 0.0), 6),
            "effectiveWeight": round(effective, 6),
            "reliabilityWeight": round(company_reliability_effective.get(company_key, 0.0), 6),
            "neutralisedWeight": round(max(0.0, 1.0 / expected_company_count - effective), 6),
        })
    missing_company_slots = expected_company_count - len(company_keys)
    if missing_company_slots:
        company_weights.append({
            "key": "__missing__",
            "slotCount": missing_company_slots,
            "targetWeight": round(missing_company_slots / expected_company_count, 6),
            "availableDayWeight": 0.0,
            "effectiveWeight": 0.0,
            "reliabilityWeight": 0.0,
            "neutralisedWeight": round(missing_company_slots / expected_company_count, 6),
        })

    effective_weights = [float(row["effectiveWeight"]) for row in company_weights if row["key"] != "__missing__"]
    effective_total = sum(effective_weights)
    evidence_shares = [weight / effective_total for weight in effective_weights] if effective_total > 0 else []
    concentration = {
        "companyInfluenceCap": company_influence_cap,
        "maxFinalCompanyWeight": round(max(effective_weights, default=0.0), 6),
        "maxEvidenceShare": round(max(evidence_shares, default=0.0), 6),
        "effectiveCompanyHHI": round(sum(share * share for share in evidence_shares), 6),
        "capBreached": max(effective_weights, default=0.0) > company_influence_cap + 1e-9,
    }

    platform_weights = {
        company_key: {
            day: company_day_rows[company_key][day]["platforms"]
            for day in days
        }
        for company_key in company_keys
    }
    return {
        "available": effective_sample > 0,
        "modelVersion": MODEL_VERSION,
        "signals": final_signals,
        "evidenceSignals": evidence_signals,
        "scores": score_personas(final_signals, breadth),
        "breadth": breadth,
        "metrics": {
            "weightedVolumeLowerBound": round(weighted_volume_lower_bound, 6),
            "coverageAdjustedVolume": round(coverage_adjusted_volume, 6) if coverage_adjusted_volume is not None else None,
            "volumeCoverage": round(volume_coverage, 6),
            "effectiveSample": round(effective_sample, 3),
        },
        "diagnostics": {
            "method": "platform_to_company_to_sector_per_day_then_fixed_day_weights",
            "expectedCompanyCount": expected_company_count,
            "providedCompanyCount": len(company_keys),
            "companyWeights": company_weights,
            "platformWeights": platform_weights,
            "effectiveDayWeights": daily_diagnostics,
            "concentration": concentration,
            "effectiveSample": round(effective_sample, 3),
            "reliability": round(overall_reliability, 6),
            "reliabilityPercent": round(overall_reliability * 100.0, 1),
            "signalReliability": {
                key: round(signal_reliability[key], 6) for key in SIGNAL_KEYS
            },
            "statusSemantics": {
                "verified_zero": "collector verified a complete stream with zero qualifying posts",
                "unobserved": "collector cannot determine whether qualifying posts existed",
            },
            "flags": sorted(flags),
        },
    }


def _signal_from_rate(rate: float, kind: str) -> float:
    if kind == "fomo":
        return _round1(50.0 + 30.0 * math.tanh((rate - 0.06) / 0.07))
    if kind == "newcomer":
        return _round1(50.0 + 28.0 * math.tanh((rate - 0.02) / 0.035))
    if kind == "intensity":
        return _round1(50.0 + 25.0 * math.tanh((rate - 0.18) / 0.16))
    raise ValueError(kind)


def _signals_from_raw(raw: Mapping[str, object], volume: float | None) -> Dict[str, float]:
    net = _safe_float(raw.get("net"))
    fomo = max(0.0, _safe_float(raw.get("fomo")))
    panic = max(0.0, _safe_float(raw.get("panic")))
    newcomer = max(0.0, _safe_float(raw.get("newcomer")))
    intense = max(0.0, _safe_float(raw.get("intense")))
    signals = {
        "optimism": _round1(50.0 + 45.0 * net),
        "fomo": _signal_from_rate(fomo, "fomo"),
        "panic": _round1(100.0 * (1.0 - math.exp(-panic / 0.12))),
        "newcomer": _signal_from_rate(newcomer, "newcomer"),
        "intensity": _signal_from_rate(intense, "intensity"),
    }
    if volume is not None:
        activity = 50.0 + 30.0 * math.tanh(math.log((max(volume, 0.0) + 2.0) / 10.0) / 1.3)
        signals["attention"] = _round1(activity)
    return signals


def companies_from_source_analyses(
    source_rows: Sequence[Mapping[str, object]],
    trading_days: Sequence[object],
) -> List[Dict[str, object]]:
    """Pure adapter for current ``source_analysis``-shaped rows.

    Each input row must identify ``companyKey`` and ``platform`` and either place
    the source analysis under ``analysis`` or expose its fields directly.  A
    caller may provide ``verifiedZeroDays`` only when pagination/completeness was
    positively verified.  Merely absent ``dailyRaw`` remains ``unobserved``.

    If a source row contains ``dailySignals`` (``day -> canonical observation``),
    those already platform-calibrated values take precedence for that day.  This
    lets the main pipeline calibrate Eastmoney/Sina/THS within each platform/day
    before company-first aggregation, without feeding their unequal page sizes
    directly into attention.
    """

    days = [_normalise_day(day) for day in trading_days]
    grouped: MutableMapping[str, List[Dict[str, object]]] = defaultdict(list)
    for source in source_rows:
        company_key = str(source.get("companyKey") or source.get("company") or "").strip()
        if not company_key:
            raise ValueError("every source row needs companyKey")
        platform = str(source.get("platform") or "").strip().lower()
        if not platform:
            raise ValueError("every source row needs platform")
        analysis_value = source.get("analysis")
        analysis = analysis_value if isinstance(analysis_value, Mapping) else source
        daily_raw_value = analysis.get("dailyRaw")
        daily_raw = daily_raw_value if isinstance(daily_raw_value, Sequence) else []
        raw_by_day = {
            str(item.get("date")): item
            for item in daily_raw
            if isinstance(item, Mapping) and item.get("date")
        }
        daily_signals_value = source.get("dailySignals")
        daily_signals_by_day = {
            _normalise_day(day_key): item
            for day_key, item in daily_signals_value.items()
            if isinstance(item, Mapping)
        } if isinstance(daily_signals_value, Mapping) else {}
        activity_value = analysis.get("activity")
        activity = activity_value if isinstance(activity_value, Mapping) else {}
        daily_activity_value = activity.get("dailyPostCounts")
        daily_activity = daily_activity_value if isinstance(daily_activity_value, Sequence) else []
        activity_by_day = {
            str(item.get("date")): item
            for item in daily_activity
            if isinstance(item, Mapping) and item.get("date")
        }
        verified_zero_value = source.get("verifiedZeroDays", [])
        if isinstance(verified_zero_value, (str, date)):
            verified_zero_value = [verified_zero_value]
        verified_zero_days = {
            _normalise_day(day) for day in verified_zero_value
        } if isinstance(verified_zero_value, Sequence) else set()
        platform_days: Dict[str, Dict[str, object]] = {}
        for day in days:
            raw_row = raw_by_day.get(day)
            activity_row = activity_by_day.get(day)
            calibrated_row = daily_signals_by_day.get(day)
            if isinstance(calibrated_row, Mapping):
                calibrated_signals_value = calibrated_row.get("signals")
                calibrated_signals = (
                    calibrated_signals_value
                    if isinstance(calibrated_signals_value, Mapping)
                    else calibrated_row
                )
                status = _normalise_status(calibrated_row)
                volume = None
                for volume_key in ("volume", "equivalentPosts", "postCount"):
                    if calibrated_row.get(volume_key) is not None:
                        volume = max(0.0, _safe_float(calibrated_row.get(volume_key)))
                        break
                if status == "verified_zero" and volume is None:
                    volume = 0.0
                fallback_count = _safe_float(raw_row.get("sampleCount")) if isinstance(raw_row, Mapping) else 0.0
                platform_days[day] = {
                    "status": status,
                    "sampleCount": max(0, int(_safe_float(calibrated_row.get("sampleCount"), fallback_count))),
                    "signals": {
                        key: _clamp(_safe_float(calibrated_signals.get(key), SIGNAL_BASELINES[key]))
                        for key in SIGNAL_KEYS
                        if calibrated_signals.get(key) is not None
                    },
                    "volume": volume,
                }
            elif isinstance(raw_row, Mapping) and bool(raw_row.get("available")):
                metrics = raw_row.get("metrics") if isinstance(raw_row.get("metrics"), Mapping) else {}
                volume = None
                if isinstance(activity_row, Mapping):
                    volume = _safe_float(
                        activity_row.get("equivalentPosts"),
                        _safe_float(activity_row.get("postCount")),
                    )
                platform_days[day] = {
                    "status": "observed",
                    "sampleCount": max(0, int(_safe_float(raw_row.get("sampleCount")))),
                    "signals": _signals_from_raw(metrics, volume),
                    "volume": volume,
                }
            elif day in verified_zero_days:
                platform_days[day] = {"status": "verified_zero", "sampleCount": 0, "volume": 0.0}
            else:
                platform_days[day] = {"status": "unobserved", "sampleCount": 0}
        grouped[company_key].append({"key": platform, "days": platform_days})
    return [
        {"key": company_key, "platforms": sorted(platforms, key=lambda item: str(item["key"]))}
        for company_key, platforms in sorted(grouped.items())
    ]


def aggregate_source_analyses_company_first(
    source_rows: Sequence[Mapping[str, object]],
    trading_days: Sequence[object],
    **kwargs: object,
) -> Dict[str, object]:
    """Convenience composition of the current-analysis adapter and core model."""

    companies = companies_from_source_analyses(source_rows, trading_days)
    return aggregate_sector_company_first(companies, trading_days, **kwargs)


__all__ = [
    "COMPANY_INFLUENCE_CAP",
    "DEFAULT_DAY_WEIGHTS",
    "EXPECTED_COMPANY_COUNT",
    "MODEL_VERSION",
    "THS_EMOTION_CAP",
    "aggregate_sector_company_first",
    "aggregate_source_analyses_company_first",
    "companies_from_source_analyses",
    "score_personas",
]
