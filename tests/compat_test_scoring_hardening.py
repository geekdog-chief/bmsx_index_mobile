from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

import update_index as core
from history_engine import CN_TZ, ESTIMATED, MEASURED


AS_OF = datetime(2026, 8, 5, 23, 59, 59, tzinfo=CN_TZ)


def make_post(
    post_id: str,
    title: str,
    published: datetime,
    *,
    author: str | None = None,
    source_key: str = "eastmoney:test",
    source: str = "测试标的吧",
    comments: int = 0,
) -> dict[str, object]:
    return {
        "id": f"eastmoney:{post_id}",
        "title": title,
        "published": published,
        "publishTime": published.isoformat(),
        "comments": comments,
        "likes": 0,
        "clicks": 0,
        "authorKey": author or f"author-{post_id}",
        "sourceKey": source_key,
        "sourceCode": "test",
        "source": source,
        "platform": "eastmoney",
        "kind": "post",
    }


class EstimatedStableSamplingTests(unittest.TestCase):
    def same_day_posts(self, count: int = 12) -> list[dict[str, object]]:
        return [
            make_post(
                str(index),
                "坚定看多马上上涨" if index % 2 else "担心下跌注意风险",
                AS_OF - timedelta(hours=2, minutes=index),
            )
            for index in range(count)
        ]

    def test_estimated_caps_each_source_published_day_at_eight(self) -> None:
        result = core.source_analysis(self.same_day_posts(), AS_OF, ESTIMATED)
        self.assertIsNotNone(result)
        self.assertEqual(result["sampleCount"], 8)
        self.assertEqual(result["samplingPolicy"]["capPerSourcePublishedDay"], 8)
        self.assertEqual(result["samplingPolicy"]["sourceDayBucketsCapped"], 1)

    def test_estimated_heat_volume_uses_full_author_limited_sample(self) -> None:
        result = core.source_analysis(self.same_day_posts(), AS_OF, ESTIMATED)
        self.assertEqual(result["sampleCount"], 8)
        self.assertEqual(result["activityPostCount"], 12)
        self.assertEqual(result["attentionBaseline"]["weightedDailyPosts3d"], 12.0)
        self.assertEqual(result["activity"]["recent24Equivalent"], 12.0)
        self.assertEqual(result["activity"]["weightedDailyPosts3d"], 12.0)
        self.assertEqual(result["activity"]["uniqueAuthorRatio24"], 1.0)

    def test_estimated_selection_is_order_independent_and_not_sentiment_driven(self) -> None:
        posts = self.same_day_posts()
        first = core.source_analysis(posts, AS_OF, ESTIMATED)
        reversed_result = core.source_analysis(list(reversed(posts)), AS_OF, ESTIMATED)
        rewritten = [{**item, "title": "完全中性的公司讨论文本"} for item in posts]
        rewritten_result = core.source_analysis(rewritten, AS_OF, ESTIMATED)
        first_ids = {post["id"] for post in first["posts"]}
        self.assertEqual(first_ids, {post["id"] for post in reversed_result["posts"]})
        self.assertEqual(first_ids, {post["id"] for post in rewritten_result["posts"]})
        self.assertIn("sha256", first["samplingPolicy"]["selection"])

    def test_author_limit_runs_before_stable_source_day_cap(self) -> None:
        posts = []
        for index in range(6):
            posts.append(make_post(f"same-{index}", "坚定看多马上上涨", AS_OF - timedelta(minutes=index), author="same"))
        for index in range(10):
            posts.append(make_post(f"other-{index}", "普通社区讨论文本", AS_OF - timedelta(minutes=20 + index)))
        result = core.source_analysis(posts, AS_OF, ESTIMATED)
        policy = result["samplingPolicy"]
        self.assertEqual(policy["postsAfterAuthorLimit"], 13)
        self.assertEqual(policy["authorOverflowRemoved"], 3)
        self.assertEqual(policy["postsSelected"], 8)

    def test_measured_path_keeps_all_author_limited_posts_and_has_no_stable_cap(self) -> None:
        result = core.source_analysis(self.same_day_posts(), AS_OF, MEASURED)
        self.assertIsNotNone(result)
        self.assertEqual(result["sampleCount"], 12)
        self.assertEqual(result["samplingPolicy"]["mode"], MEASURED)
        self.assertIsNone(result["samplingPolicy"]["capPerSourcePublishedDay"])
        self.assertEqual(result["samplingPolicy"]["sourceDayBucketsCapped"], 0)


class ActivityCalibrationTests(unittest.TestCase):
    def test_activity_score_blends_velocity_with_independent_author_breadth(self) -> None:
        signals = {key: 50.0 for key in core.SIGNAL_KEYS}
        analysis = {
            "signals": {**signals, "heat": 50.0},
            "activity": {
                "logPostingVelocity": 9.0,
                "uniqueAuthorRatio24": 0.0,
            },
        }
        calibration = {
            "eastmoney": {
                **{key: [float(value) for value in range(10)] for key in core.SIGNAL_KEYS},
                "activityVelocity": [float(value) for value in range(10)],
                "activityBreadth": [value / 10 for value in range(10)],
            }
        }

        result = core.calibrate_source_analysis(analysis, "eastmoney", calibration)
        activity = result["activity"]

        self.assertGreater(activity["velocityScore"], activity["relativeScore"])
        self.assertGreater(activity["relativeScore"], activity["breadthScore"])


class ThreeTradingDayScoringTests(unittest.TestCase):
    MONDAY = datetime(2026, 8, 10, 23, 59, 59, tzinfo=CN_TZ)

    @staticmethod
    def at(day: int, hour: int = 10, minute: int = 0) -> datetime:
        return datetime(2026, 8, day, hour, minute, tzinfo=CN_TZ)

    def test_monday_crosses_weekend_and_uses_fixed_daily_weights(self) -> None:
        posts = [
            make_post("mon", "马上上车必涨", self.at(10)),
            make_post("fri", "公司基本面保持稳定", self.at(7)),
            make_post("thu", "公司基本面保持稳定", self.at(6)),
            make_post("sat", "周末马上上车必涨", self.at(8)),
            make_post("sun", "周日马上上车必涨", self.at(9)),
        ]
        result = core.source_analysis(posts, self.MONDAY, ESTIMATED)

        self.assertEqual(result["tradingWindow"]["tradingDays"], [
            "2026-08-10", "2026-08-07", "2026-08-06",
        ])
        self.assertEqual(result["tradingWindow"]["effectiveDayWeights"], {
            "2026-08-10": 0.5, "2026-08-07": 0.3, "2026-08-06": 0.2,
        })
        self.assertEqual(result["tradingWindow"]["nonTradingPostsExcluded"], 2)
        self.assertAlmostEqual(result["raw"]["fomo"], 0.5, places=6)
        self.assertEqual({post["id"] for post in result["posts"]}, {
            "eastmoney:mon", "eastmoney:fri", "eastmoney:thu",
        })

    def test_missing_trading_day_renormalizes_real_days_without_neutral_injection(self) -> None:
        posts = [
            make_post("mon", "马上上车必涨", self.at(10)),
            make_post("thu", "公司基本面保持稳定", self.at(6)),
        ]
        result = core.source_analysis(posts, self.MONDAY, ESTIMATED)

        self.assertEqual(result["tradingWindow"]["availableTradingDayCount"], 2)
        self.assertEqual(result["tradingWindow"]["missingTradingDaysExcluded"], 1)
        weights = result["tradingWindow"]["effectiveDayWeights"]
        self.assertAlmostEqual(weights["2026-08-10"], 5 / 7, places=6)
        self.assertAlmostEqual(weights["2026-08-06"], 2 / 7, places=6)
        self.assertAlmostEqual(result["raw"]["fomo"], 5 / 7, places=6)

    def test_signal_rates_are_aggregated_within_day_before_day_weighting(self) -> None:
        posts = [make_post("today", "马上上车必涨", self.at(10))]
        posts.extend(
            make_post(f"fri-{index}", "公司基本面保持稳定", self.at(7, minute=index))
            for index in range(5)
        )
        posts.extend(
            make_post(f"thu-{index}", "公司基本面保持稳定", self.at(6, minute=index))
            for index in range(5)
        )
        result = core.source_analysis(posts, self.MONDAY, ESTIMATED)

        # 若先混合全部帖子会远低于 50%；按日先算命中率后应精确保留今日 50%。
        self.assertAlmostEqual(result["raw"]["fomo"], 0.5, places=6)

    def test_exchange_holiday_posts_are_excluded(self) -> None:
        as_of = datetime(2026, 10, 8, 23, 59, 59, tzinfo=CN_TZ)
        posts = [
            make_post("oct8", "公司基本面保持稳定", datetime(2026, 10, 8, 10, tzinfo=CN_TZ)),
            make_post("sep30", "公司基本面保持稳定", datetime(2026, 9, 30, 10, tzinfo=CN_TZ)),
            make_post("sep29", "公司基本面保持稳定", datetime(2026, 9, 29, 10, tzinfo=CN_TZ)),
            make_post("holiday", "休市期间马上上车必涨", datetime(2026, 10, 1, 10, tzinfo=CN_TZ)),
        ]
        result = core.source_analysis(posts, as_of, ESTIMATED)

        self.assertEqual(result["tradingWindow"]["tradingDays"], [
            "2026-10-08", "2026-09-30", "2026-09-29",
        ])
        self.assertEqual(result["tradingWindow"]["nonTradingPostsExcluded"], 1)
        self.assertEqual(result["raw"]["fomo"], 0.0)

    def test_non_trading_update_belongs_to_latest_market_session(self) -> None:
        sunday = datetime(2026, 8, 9, 12, tzinfo=CN_TZ)
        posts = [
            make_post("fri", "公司基本面保持稳定", self.at(7)),
            make_post("sun", "周日马上上车必涨", self.at(9)),
        ]
        result = core.source_analysis(posts, sunday, MEASURED)

        self.assertEqual(core.scoring_market_day(sunday).isoformat(), "2026-08-07")
        self.assertEqual(result["tradingWindow"]["marketDate"], "2026-08-07")
        self.assertEqual([post["id"] for post in result["posts"]], ["eastmoney:fri"])

    def test_intraday_update_waits_for_completed_session(self) -> None:
        morning = datetime(2026, 8, 10, 10, 30, tzinfo=CN_TZ)
        after_close = datetime(2026, 8, 10, 15, 10, tzinfo=CN_TZ)

        self.assertEqual(core.scoring_market_day(morning).isoformat(), "2026-08-07")
        self.assertEqual(core.scoring_market_day(after_close).isoformat(), "2026-08-10")

    def test_existing_weekend_history_points_are_removed(self) -> None:
        old_history = [
            {"date": "2026-08-07", "sectors": []},
            {"date": "2026-08-08", "sectors": []},
            {"date": "2026-10-01", "sectors": []},
        ]
        with mock.patch.object(
            core,
            "load_json",
            side_effect=[{"version": core.VERSION}, old_history],
        ):
            prepared = core.prepare_history()

        self.assertEqual([point["date"] for point in prepared], ["2026-08-07"])

    def test_activity_uses_weighted_daily_post_counts(self) -> None:
        posts: list[dict[str, object]] = []
        for day, count in ((10, 10), (7, 4), (6, 2)):
            posts.extend(
                make_post(f"{day}-{index}", "今天先拿着看看再说", self.at(day, minute=index))
                for index in range(count)
            )
        metrics = core.source_activity_metrics(posts, self.MONDAY, MEASURED)

        self.assertAlmostEqual(metrics["weightedDailyPosts3d"], 6.6, places=6)
        self.assertAlmostEqual(metrics["dailyEquivalent"], 6.6, places=6)
        self.assertAlmostEqual(metrics["postingVelocity"], 6.6, places=6)
        self.assertEqual(metrics["totalEquivalentPosts3d"], 16.0)

    def test_interactions_are_diagnostic_only_and_do_not_change_formal_text_score(self) -> None:
        posts = [
            make_post("hot", "马上上车必涨", self.at(10), comments=100),
            make_post("quiet", "今天先拿着看看再说", self.at(10, minute=1)),
        ]
        measured = core.source_analysis(posts, self.MONDAY, MEASURED)
        estimated = core.source_analysis(posts, self.MONDAY, ESTIMATED)

        self.assertAlmostEqual(measured["raw"]["fomo"], estimated["raw"]["fomo"])
        self.assertAlmostEqual(estimated["raw"]["fomo"], 0.5, places=6)
        self.assertEqual(measured["attentionBaseline"]["volumeWeight"], 1.0)
        self.assertEqual(measured["attentionBaseline"]["interactionWeight"], 0.0)
        self.assertFalse(measured["attentionBaseline"]["interactionUsedForScoring"])
        self.assertIsNotNone(measured["attentionBaseline"]["interactionScore"])


class ContextRegressionTests(unittest.TestCase):
    def test_known_agriculture_negative_contexts_are_not_positive(self) -> None:
        expected_negative = (
            "突破不了，别盲目加仓",
            "今天放量下杀，资金跑路",
            "如果连这都看不出资金在跑路，还炒什么股！",
            "这个板块没有龙头",
            "卖出当前标的买入别的标的",
            "不要加仓",
            "盲目加仓风险太大",
        )
        for text in expected_negative:
            with self.subTest(text=text):
                self.assertLess(core.sentiment_score(text), 0)
        self.assertTrue(core.contains_nonnegated("如果连这都看不出资金在跑路，还炒什么股！", core.PANIC_WORDS))

    def test_switching_uses_optional_topic_or_source_context(self) -> None:
        text = "卖出牧原股份买入温氏股份"
        self.assertLess(core.sentiment_score(text, topic="牧原股份"), 0)
        self.assertGreater(core.sentiment_score(text, topic="温氏股份"), 0)
        self.assertLess(core.sentiment_score(text, source="牧原股份吧"), 0)
        self.assertLess(
            core.sentiment_score("卖出牧原买入科技是大道，该反弹了", source="牧原股份吧"),
            0,
        )

    def test_two_way_conditional_plan_is_near_neutral(self) -> None:
        for text in (
            "突破就加仓，跌破就止损",
            "若突破就买入，否则跌破就卖出",
            "如果站上压力位就做多，下跌破位则清仓",
            "上方压力58，下方支撑28。若突破58可重仓做多，若下探至28",
        ):
            with self.subTest(text=text):
                self.assertLessEqual(abs(core.sentiment_score(text)), 1)


class PersonaEvidenceDiagnosticsTests(unittest.TestCase):
    def test_classifier_is_exclusive_and_masks_evidence_words(self) -> None:
        label, reason, masked = core.classify_persona_evidence("孩子的学费是闲钱，新手请问能买吗？")
        self.assertEqual((label, reason), ("baoma", "explicit_household"))
        self.assertNotIn("孩子", masked)
        self.assertNotIn("学费", masked)
        self.assertEqual(core.classify_persona_evidence("新手请问这个位置能买吗？")[0], "novice")
        self.assertEqual(core.classify_persona_evidence("突破压力位后加仓")[0], "retail")
        self.assertIsNone(core.classify_persona_evidence("公司发布年度报告")[0])

    def test_diagnostics_have_evidence_counts_and_only_shrinking_candidates(self) -> None:
        posts = [
            make_post("b", "孩子学费要紧，我只用闲钱，短期上涨也不追", AS_OF - timedelta(hours=1)),
            make_post("n", "新手请问这只股票暴跌了还能买吗？", AS_OF - timedelta(hours=2)),
            make_post("r", "突破压力位后加仓，但也注意风险", AS_OF - timedelta(hours=3)),
        ]
        grouped = core.analyze_group(posts, as_of=AS_OF, record_type=ESTIMATED)
        evidence = grouped["diagnostics"]["personaEvidence"]
        self.assertTrue(evidence["formalScoresUnchanged"])
        self.assertEqual(evidence["evidenceCounts"], {"baoma": 1, "novice": 1, "retail": 1})
        baseline = grouped["signals"]["optimism"]
        for candidate in evidence["candidates"].values():
            self.assertTrue(candidate["available"])
            raw = candidate["maskedDirectionRaw"]
            shrunk = candidate["candidateShrunkDirection"]
            self.assertLessEqual(min(raw, baseline), shrunk)
            self.assertGreaterEqual(max(raw, baseline), shrunk)
            self.assertLessEqual(candidate["evidenceWeight"], 0.65)

    def test_experimental_diagnostics_do_not_change_formal_scores_or_signals(self) -> None:
        posts = [
            make_post("1", "新手请问上涨后还能买吗？", AS_OF - timedelta(hours=1)),
            make_post("2", "突破后加仓，继续看好", AS_OF - timedelta(hours=2)),
        ]
        normal = core.analyze_group(posts, as_of=AS_OF, record_type=MEASURED)
        with mock.patch.object(core, "persona_evidence_diagnostics", return_value={}):
            without_diagnostics = core.analyze_group(posts, as_of=AS_OF, record_type=MEASURED)
        self.assertEqual(normal["scores"], without_diagnostics["scores"])
        self.assertEqual(normal["signals"], without_diagnostics["signals"])


if __name__ == "__main__":
    unittest.main()
