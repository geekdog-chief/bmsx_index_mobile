from __future__ import annotations

import unittest
from datetime import date, datetime

import trading_calendar


class TradingCalendarTests(unittest.TestCase):
    def test_weekends_are_always_closed(self) -> None:
        self.assertFalse(trading_calendar.is_trading_day(date(2026, 8, 8)))
        self.assertFalse(trading_calendar.is_trading_day(date(2027, 8, 8)))

    def test_2025_official_holiday_boundaries(self) -> None:
        closures = (
            "2025-01-01",
            "2025-01-28",
            "2025-02-04",
            "2025-04-04",
            "2025-05-01",
            "2025-05-05",
            "2025-06-02",
            "2025-10-01",
            "2025-10-08",
        )
        for day in closures:
            with self.subTest(day=day):
                self.assertFalse(trading_calendar.is_trading_day(day))

        reopenings = (
            "2025-01-02",
            "2025-02-05",
            "2025-04-07",
            "2025-05-06",
            "2025-06-03",
            "2025-10-09",
        )
        for day in reopenings:
            with self.subTest(day=day):
                self.assertTrue(trading_calendar.is_trading_day(day))

    def test_2025_mid_autumn_is_combined_with_october_closure(self) -> None:
        # There was no separate September closure in the SSE 2025 notice.
        for day in ("2025-09-15", "2025-09-16", "2025-09-17"):
            with self.subTest(day=day):
                self.assertTrue(trading_calendar.is_trading_day(day))

    def test_2026_official_holiday_boundaries(self) -> None:
        closures = (
            "2026-01-01",
            "2026-02-16",
            "2026-02-23",
            "2026-04-06",
            "2026-05-01",
            "2026-05-05",
            "2026-06-19",
            "2026-09-25",
            "2026-10-01",
            "2026-10-07",
        )
        for day in closures:
            with self.subTest(day=day):
                self.assertFalse(trading_calendar.is_trading_day(day))

        reopenings = (
            "2026-01-05",
            "2026-02-24",
            "2026-04-07",
            "2026-05-06",
            "2026-06-22",
            "2026-09-28",
            "2026-10-08",
        )
        for day in reopenings:
            with self.subTest(day=day):
                self.assertTrue(trading_calendar.is_trading_day(day))

    def test_latest_trading_day_skips_weekends_and_long_closures(self) -> None:
        self.assertEqual(
            trading_calendar.latest_trading_day("2026-02-23"),
            date(2026, 2, 13),
        )
        self.assertEqual(
            trading_calendar.latest_trading_day("2025-02-04"),
            date(2025, 1, 27),
        )
        self.assertEqual(
            trading_calendar.latest_trading_day("2026-08-08"),
            date(2026, 8, 7),
        )
        self.assertEqual(
            trading_calendar.latest_trading_day("2026-08-06"),
            date(2026, 8, 6),
        )

    def test_previous_trading_days_are_newest_first(self) -> None:
        self.assertEqual(
            trading_calendar.previous_trading_days("2026-02-24", 3),
            [date(2026, 2, 24), date(2026, 2, 13), date(2026, 2, 12)],
        )
        self.assertEqual(
            trading_calendar.previous_trading_days("2026-08-03", 3, include_current=False),
            [date(2026, 7, 31), date(2026, 7, 30), date(2026, 7, 29)],
        )
        self.assertEqual(
            trading_calendar.previous_trading_days("2026-08-08", 2),
            [date(2026, 8, 7), date(2026, 8, 6)],
        )
        self.assertEqual(trading_calendar.previous_trading_days("2026-08-06", 0), [])

    def test_datetime_and_iso_string_inputs_are_supported(self) -> None:
        self.assertTrue(trading_calendar.is_trading_day(datetime(2026, 8, 6, 15, 30)))
        self.assertTrue(trading_calendar.is_trading_day("2026-08-06"))

    def test_trading_day_gap_counts_sessions_not_calendar_days(self) -> None:
        self.assertEqual(trading_calendar.trading_day_gap("2026-08-07", "2026-08-10"), 1)
        self.assertEqual(trading_calendar.trading_day_gap("2026-02-13", "2026-02-24"), 1)
        self.assertEqual(trading_calendar.trading_day_gap("2026-08-06", "2026-08-10"), 2)
        self.assertEqual(trading_calendar.trading_day_gap("2026-08-10", "2026-08-10"), 0)

    def test_unknown_year_exposes_weekday_fallback_mode(self) -> None:
        self.assertEqual(
            trading_calendar.calendar_mode("2027-01-01"),
            trading_calendar.CALENDAR_MODE_WEEKDAY_FALLBACK,
        )
        self.assertEqual(
            trading_calendar.calendar_status("2027-01-01"),
            {
                "date": "2027-01-01",
                "isTradingDay": True,
                "calendarMode": "weekday_fallback",
            },
        )
        self.assertEqual(
            trading_calendar.calendar_status("2026-01-01")["calendarMode"],
            "official_sse",
        )

    def test_invalid_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            trading_calendar.previous_trading_days("2026-08-06", -1)
        with self.assertRaises(TypeError):
            trading_calendar.previous_trading_days("2026-08-06", 1.5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            trading_calendar.previous_trading_days("2026-08-06", True)
        with self.assertRaises(ValueError):
            trading_calendar.is_trading_day("2026-02-30")


if __name__ == "__main__":
    unittest.main()
