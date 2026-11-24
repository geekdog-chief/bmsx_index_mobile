"""A股交易日历：2025/2026上交所官方休市表；其他年份weekday回退并显式标记。

新一年官方安排公布后把年份加入 OFFICIAL_YEARS 并在 _OFFICIAL_HOLIDAY_RANGES
补区间即可，回退逻辑不用动。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Union

DateLike = Union[date, datetime, str]

CALENDAR_MODE_OFFICIAL = "official_sse"
CALENDAR_MODE_WEEKDAY_FALLBACK = "weekday_fallback"
OFFICIAL_YEARS = frozenset({2025, 2026})

_OFFICIAL_HOLIDAY_RANGES = {
    2025: (
        ("2025-01-01", "2025-01-01"),
        ("2025-01-28", "2025-02-04"),
        ("2025-04-04", "2025-04-06"),
        ("2025-05-01", "2025-05-05"),
        ("2025-05-31", "2025-06-02"),
        ("2025-10-01", "2025-10-08"),
    ),
    2026: (
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ),
}


def _coerce_date(value: DateLike) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError("calendar date must be a date, datetime, or YYYY-MM-DD string")


def _expand_holiday_ranges() -> frozenset:
    holidays = set()
    for ranges in _OFFICIAL_HOLIDAY_RANGES.values():
        for start_text, end_text in ranges:
            current = date.fromisoformat(start_text)
            end = date.fromisoformat(end_text)
            while current <= end:
                holidays.add(current)
                current += timedelta(days=1)
    return frozenset(holidays)


OFFICIAL_HOLIDAYS = _expand_holiday_ranges()


def calendar_mode(value: DateLike) -> str:
    """official_sse = 该年有官方休市表；weekday_fallback = 仅知周一到周五。"""
    day = _coerce_date(value)
    if day.year in OFFICIAL_YEARS:
        return CALENDAR_MODE_OFFICIAL
    return CALENDAR_MODE_WEEKDAY_FALLBACK


def is_trading_day(value: DateLike) -> bool:
    day = _coerce_date(value)
    if day.weekday() >= 5:
        return False
    if day.year in OFFICIAL_YEARS and day in OFFICIAL_HOLIDAYS:
        return False
    return True


def calendar_status(value: DateLike) -> dict:
    day = _coerce_date(value)
    return {
        "date": day.isoformat(),
        "isTradingDay": is_trading_day(day),
        "calendarMode": calendar_mode(day),
    }


def latest_trading_day(value: DateLike) -> date:
    current = _coerce_date(value)
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def previous_trading_days(value: DateLike, count: int, include_current: bool = True) -> list:
    """最近count个交易日，新的在前。"""
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError("count must be an integer")
    if count < 0:
        raise ValueError("count must be non-negative")
    if not isinstance(include_current, bool):
        raise TypeError("include_current must be a boolean")
    if count == 0:
        return []
    current = _coerce_date(value)
    if not include_current:
        current -= timedelta(days=1)
    result = []
    while len(result) < count:
        if is_trading_day(current):
            result.append(current)
        current -= timedelta(days=1)
    return result


def trading_day_gap(start: DateLike, end: DateLike) -> int:
    """从start到end经过的交易日间隔；周五到下周一算1。"""
    first = _coerce_date(start)
    last = _coerce_date(end)
    if last <= first:
        return 0
    current = first + timedelta(days=1)
    count = 0
    while current <= last:
        if is_trading_day(current):
            count += 1
        current += timedelta(days=1)
    return count
