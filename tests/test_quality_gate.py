"""K1 质量门单元测试（ChatGPT 审计意见要求的覆盖清单）。"""
import unittest

from quality_gate import assess_source_quality, build_quality_report, format_console_lines


def src(posts=None, **kw):
    base = {"posts": [{"t": "x"}] if posts is None else posts,
            "platform": "eastmoney", "name": "测试吧", "coverageStatus": "third_trading_day_reached",
            "rawRows": 50, "coverageDays": ["2026-11-24"], "capHit": False}
    base.update(kw)
    return base


class TestStatusMapping(unittest.TestCase):
    def test_full_statuses(self):
        for st in ("third_trading_day_reached", "source_exhausted_before_third_trading_day", "third_day_reached"):
            r = assess_source_quality("k", src(coverageStatus=st))
            self.assertEqual(r["status"], "full", st)

    def test_partial_statuses(self):
        for st in ("page_cap_reached_before_third_trading_day",
                   "cursor_stalled_before_third_trading_day",
                   "anonymous_page_cap_lower_bound"):
            r = assess_source_quality("k", src(coverageStatus=st))
            self.assertEqual(r["status"], "partial", st)

    def test_unknown_statuses(self):
        for st in ("single_page_fallback_unknown", "legacy_cache_unknown", "unknown"):
            r = assess_source_quality("k", src(coverageStatus=st))
            self.assertEqual(r["status"], "unknown", st)

    def test_hot_feed_beats_cap_hit(self):
        r = assess_source_quality("k", src(coverageStatus="non_chronological_hot_feed", capHit=True))
        self.assertEqual(r["status"], "hot_feed")

    def test_empty_posts_priority(self):
        r = assess_source_quality("k", src(posts=[], coverageStatus="third_trading_day_reached"))
        self.assertEqual(r["status"], "empty")

    def test_cap_hit_true_partial(self):
        r = assess_source_quality("k", src(coverageStatus=None, capHit=True))
        self.assertEqual(r["status"], "partial")

    def test_cap_hit_conflict_full_downgrades(self):
        r = assess_source_quality("k", src(coverageStatus="third_trading_day_reached", capHit=True))
        self.assertEqual(r["status"], "partial")

    def test_unregistered_status_unknown(self):
        r = assess_source_quality("k", src(coverageStatus="some_future_status"))
        self.assertEqual(r["status"], "unknown")

    def test_missing_status_unknown(self):
        r = assess_source_quality("k", src(coverageStatus=None))
        self.assertEqual(r["status"], "unknown")


class TestCapHitTyping(unittest.TestCase):
    def test_missing(self):
        r = assess_source_quality("k", src(capHit=None, coverageStatus=None))
        self.assertIsNone(r["capHit"])
        self.assertEqual(r["status"], "unknown")

    def test_none_not_false(self):
        s = src(coverageStatus=None, capHit=None)
        r = assess_source_quality("k", s)
        self.assertNotEqual(r["status"], "full")

    def test_false(self):
        r = assess_source_quality("k", src(capHit=False))
        self.assertEqual(r["status"], "full")
        self.assertIs(r["capHit"], False)

    def test_string_false_not_truthy(self):
        # 字符串 "false" 不得被当成布尔处理 → 视为未知
        r = assess_source_quality("k", src(capHit="false", coverageStatus=None))
        self.assertIsNone(r["capHit"])
        self.assertEqual(r["status"], "unknown")


class TestLegacyAndGarbage(unittest.TestCase):
    def test_legacy_cache_all_missing(self):
        r = assess_source_quality("k", {"posts": [{"t": 1}]})
        self.assertEqual(r["status"], "unknown")

    def test_non_mapping_source(self):
        r = assess_source_quality("k", "garbage")
        self.assertEqual(r["status"], "unknown")

    def test_garbage_fields(self):
        # 已知 full 状态 + 非法 capHit（→None）：状态维持 full，capHit 记未知
        r = assess_source_quality("k", src(coverageDays="notalist", rawRows={"x": 1}, capHit=1))
        self.assertEqual(r["status"], "full")
        self.assertIsNone(r["capHit"])
        self.assertIsNone(r["coverageDays"])


class TestReport(unittest.TestCase):
    def test_summary_sums(self):
        results = {
            "a": src(),
            "b": src(coverageStatus="page_cap_reached_before_third_trading_day"),
            "c": src(coverageStatus="non_chronological_hot_feed", capHit=True),
            "d": {"posts": [{"t": 1}]},
            "e": src(posts=[]),
        }
        rep = build_quality_report(results)
        s = rep["summary"]
        self.assertEqual(s, {"fullCount": 1, "partialCount": 1, "hotFeedCount": 1,
                             "unknownCount": 1, "emptyCount": 1, "totalSources": 5})
        self.assertEqual(s["fullCount"] + s["partialCount"] + s["hotFeedCount"]
                         + s["unknownCount"] + s["emptyCount"], s["totalSources"])

    def test_traceability_fields(self):
        rep = build_quality_report({"a": src()})
        entry = rep["sources"]["a"]
        for f in ("key", "platform", "name", "status", "reason", "rawRows", "coverageDays", "capHit", "coverageStatus"):
            self.assertIn(f, entry)

    def test_every_source_traceable(self):
        results = {f"k{i}": src(coverageStatus="unknown") for i in range(7)}
        rep = build_quality_report(results)
        self.assertEqual(len(rep["sources"]), 7)

    def test_console_lines(self):
        rep = build_quality_report({"a": src(), "b": src(coverageStatus="page_cap_reached_before_third_trading_day")})
        lines = format_console_lines(rep)
        self.assertTrue(any("[数据质量]" in l and "full 1" in l for l in lines))
        self.assertTrue(any("b(partial)" in l for l in lines))

    def test_report_does_not_mutate_sources(self):
        s = src()
        before = dict(s)
        build_quality_report({"a": s})
        self.assertEqual(s, before)


if __name__ == "__main__":
    unittest.main()
