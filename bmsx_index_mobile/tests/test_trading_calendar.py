import unittest
from datetime import date

from trading_calendar import (
    CALENDAR_MODE_OFFICIAL,
    CALENDAR_MODE_WEEKDAY_FALLBACK,
    calendar_mode,
    calendar_status,
    is_trading_day,
    latest_trading_day,
    previous_trading_days,
    trading_day_gap,
)


class TradingCalendarTests(unittest.TestCase):
    def test_official_holidays_2025(self):
        self.assertFalse(is_trading_day(date(2025, 1, 29)))  # 春节
        self.assertFalse(is_trading_day(date(2025, 10, 1)))  # 国庆
        self.assertTrue(is_trading_day(date(2025, 10, 9)))
        self.assertEqual(calendar_mode(date(2025, 6, 1)), CALENDAR_MODE_OFFICIAL)

    def test_official_holidays_2026(self):
        self.assertFalse(is_trading_day(date(2026, 2, 16)))  # 春节
        self.assertFalse(is_trading_day(date(2026, 5, 1)))  # 劳动节
        self.assertEqual(calendar_mode(date(2026, 3, 3)), CALENDAR_MODE_OFFICIAL)

    def test_weekends_never_trading(self):
        self.assertFalse(is_trading_day(date(2026, 11, 21)))  # 周六
        self.assertFalse(is_trading_day(date(2026, 11, 22)))  # 周日

    def test_unknown_year_weekday_fallback_exposes_mode(self):
        # 2027年无官方表：周末回退，calendarMode必须显式暴露
        self.assertFalse(is_trading_day(date(2027, 1, 2)))   # 周六
        self.assertTrue(is_trading_day(date(2027, 1, 4)))    # 周一
        self.assertEqual(calendar_mode(date(2027, 1, 4)), CALENDAR_MODE_WEEKDAY_FALLBACK)
        status = calendar_status(date(2027, 1, 4))
        self.assertEqual(status["calendarMode"], CALENDAR_MODE_WEEKDAY_FALLBACK)
        self.assertTrue(status["isTradingDay"])

    def test_previous_trading_days_order_and_count(self):
        days = previous_trading_days(date(2026, 11, 24), 3)
        self.assertEqual(len(days), 3)
        self.assertEqual(days[0], date(2026, 11, 24))
        self.assertTrue(all(days[i] > days[i + 1] for i in range(len(days) - 1)))

    def test_friday_to_monday_gap_is_one(self):
        self.assertEqual(trading_day_gap(date(2026, 11, 20), date(2026, 11, 23)), 1)

    def test_latest_trading_day_skips_holiday(self):
        self.assertEqual(latest_trading_day(date(2025, 10, 8)), date(2025, 9, 30))


if __name__ == "__main__":
    unittest.main()
