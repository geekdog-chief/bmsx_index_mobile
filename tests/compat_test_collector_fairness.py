from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import update_index as core
import collectors as col


CN_TZ = timezone(timedelta(hours=8))


def em_row(post_id: str, published: str, author: str) -> dict[str, object]:
    return {
        "post_id": post_id,
        "post_type": 0,
        "post_source_id": "",
        "post_title": f"测试社区帖子 {post_id}",
        "post_publish_time": published,
        "post_comment_count": 1,
        "post_click_count": 2,
        "user_id": author,
        "user_nickname": f"普通用户{author}",
    }


def sina_row(tid: str, published: str, author: str) -> dict[str, object]:
    return {
        "tid": tid,
        "title": f"测试社区帖子 {tid}",
        "timestamp": published,
        "reply": 1,
        "views": 2,
        "like": 0,
        "user": {"uid": author, "nick": f"普通用户{author}", "verified": False},
    }


def ths_feed_row(
    post_id: str,
    author: str,
    *,
    title: str = "这是普通用户的有效社区发言",
    comments: list[dict[str, object]] | None = None,
    author_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "info": {
            "id": post_id,
            "ctime": "2026-08-06 10:00:00",
            "state": "passAudit",
            "type": "post",
            "community_biz_type": 1,
        },
        "author": {"id": author, "name": author, **(author_extra or {})},
        "stat": {"comment_num": len(comments or []), "forward_num": 0, "like_num": 0},
        "title": {"content": title},
        "abstract": {"content": ""},
        "comment": {"comments": comments or []},
    }


def taoguba_row(
    row_id: str,
    author: str,
    *,
    action_date: str = "2026-08-06 10:00:00",
    auth: int = 0,
    user_title: dict[str, object] | None = None,
    row_type: str = "T",
) -> dict[str, object]:
    return {
        "rID": row_id,
        "actionDate": action_date,
        "userID": author,
        "userName": author,
        "auth": auth,
        "userTitle": user_title or {},
        "deleteFlag": "N",
        "rType": row_type,
        "subject": f"主题 {row_id}",
        "body": f"这是普通用户有效讨论内容 {row_id}",
        "replyNum": 1,
        "viewNum": 2,
        "usefulNum": 0,
    }


class CollectorFairnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scoring_days_patch = patch.object(
        core, "scoring_trading_days",
            side_effect=lambda as_of=core.NOW: [
                date(2026, 8, 6), date(2026, 8, 5), date(2026, 8, 4)
            ],
        )
        self.sleep_patch = patch.object(core.time, "sleep", side_effect=lambda _seconds: None)
        self.scoring_days_patch.start()
        self.sleep_patch.start()
        self.addCleanup(self.scoring_days_patch.stop)
        self.addCleanup(self.sleep_patch.stop)

    def test_eastmoney_pages_until_third_trading_day_and_deduplicates(self) -> None:
        page_one = [
            em_row(f"p1-{index}", "2026-08-06 10:00:00", "repeat" if index < 3 else f"u{index}")
            for index in range(100)
        ]
        page_two = [page_one[20]] + [
            em_row(f"p2-a-{index}", "2026-08-05 10:00:00", "repeat")
            for index in range(3)
        ] + [
            em_row(f"p2-b-{index}", "2026-08-04 10:00:00", "repeat")
            for index in range(3)
        ]
        requested_pages: list[int] = []

        def fake_request_json(url: str, platform: str, **_kwargs) -> dict[str, object]:
            self.assertEqual(platform, "eastmoney")
            page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["p"][0])
            requested_pages.append(page)
            return {"me": "操作成功", "count": 500, "re": page_one if page == 1 else page_two}

        with patch.object(col, "request_json", side_effect=fake_request_json):
            result = core.fetch_eastmoney(
                {"key": "eastmoney:000001", "code": "000001", "name": "测试股吧"}
            )

        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual(result["pageCount"], 2)
        self.assertEqual(result["rawRows"], 107)
        self.assertIs(result["capHit"], False)
        self.assertIs(result["chronological"], True)
        self.assertIs(result["thirdTradingDayReached"], True)
        self.assertEqual(result["coverageStatus"], "third_trading_day_reached")
        self.assertEqual(result["coverageDays"], ["2026-08-06", "2026-08-05", "2026-08-04"])
        self.assertIs(result["freshFetchedNow"], True)
        self.assertEqual(len({post["id"] for post in result["posts"]}), len(result["posts"]))
        repeat_posts = [post for post in result["posts"] if post["authorKey"] == "repeat"]
        self.assertEqual(len(repeat_posts), 9)

    def test_eastmoney_marks_page_cap_instead_of_claiming_complete(self) -> None:
        def fake_request_json(url: str, _platform: str, **_kwargs) -> dict[str, object]:
            page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["p"][0])
            rows = [
                em_row(f"{page}-{index}", "2026-08-06 10:00:00", f"u-{page}-{index}")
                for index in range(100)
            ]
            return {"me": "操作成功", "count": 1000, "re": rows}

        with patch.object(col, "request_json", side_effect=fake_request_json):
            result = core.fetch_eastmoney(
                {"key": "eastmoney:000002", "code": "000002", "name": "测试股吧"}
            )

        self.assertEqual(result["pageCount"], 3)
        self.assertEqual(result["rawRows"], 300)
        self.assertIs(result["capHit"], True)
        self.assertIs(result["thirdTradingDayReached"], False)
        self.assertEqual(result["coverageStatus"], "page_cap_reached_before_third_trading_day")

    def test_sina_uses_cursor_pages_and_records_coverage(self) -> None:
        page_one = [
            sina_row(f"s1-{index}", "2026-08-06 10:00:00", "repeat" if index < 3 else f"u{index}")
            for index in range(50)
        ]
        page_two = [
            sina_row(f"s2-a-{index}", "2026-08-05 10:00:00", "repeat")
            for index in range(3)
        ] + [
            sina_row(f"s2-b-{index}", "2026-08-04 10:00:00", "repeat")
            for index in range(3)
        ]
        queries: list[dict[str, list[str]]] = []

        def fake_request_json(url: str, platform: str, **_kwargs) -> dict[str, object]:
            self.assertEqual(platform, "sina")
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            queries.append(query)
            rows = page_two if "relate_tid" in query else page_one
            return {"data": {"threads": rows}}

        with (
            patch.object(col, "discover_sina_bid", return_value="42"),
            patch.object(col, "request_json", side_effect=fake_request_json),
        ):
            result = core.fetch_sina(
                {"key": "sina:000001", "code": "000001", "name": "测试"}
            )

        self.assertEqual(len(queries), 2)
        self.assertNotIn("relate_tid", queries[0])
        self.assertEqual(queries[1]["relate_tid"], ["s1-49"])
        self.assertEqual(result["pageCount"], 2)
        self.assertEqual(result["rawRows"], 56)
        self.assertIs(result["capHit"], False)
        self.assertIs(result["thirdTradingDayReached"], True)
        self.assertEqual(result["paginationMode"], "cursor")
        repeat_posts = [post for post in result["posts"] if post["authorKey"] == "repeat"]
        self.assertEqual(len(repeat_posts), 9)

    def test_ths_marks_hot_feed_and_comment_time_inheritance(self) -> None:
        comments = [
            {
                "id": f"comment-{index}",
                "tag": "ordinary",
                "uid": f"commenter-{index}",
                "nickname": f"普通评论者{index}",
                "content": f"这是有效评论内容 {index}",
                **({"ctime": "2026-08-06 11:00:00"} if index == 0 else {}),
            }
            for index in range(4)
        ]
        payload = {
            "status_code": 0,
            "data": {
                "feed": [{
                    "info": {
                        "id": "parent-1",
                        "ctime": "2026-08-06 10:00:00",
                        "state": "passAudit",
                        "type": "post",
                        "community_biz_type": 1,
                    },
                    "author": {"id": "poster", "name": "普通发帖者"},
                    "stat": {"comment_num": 4, "forward_num": 0, "like_num": 1},
                    "title": {"content": "这是有效母帖标题"},
                    "abstract": {"content": "这是有效母帖摘要"},
                    "comment": {"comments": comments},
                }]
            },
        }
        with (
            patch.object(
        col, "discover_ths_forum",
                return_value={"forumCode": "000001", "marketId": 33, "forumName": "测试论坛"},
            ),
            patch.object(col, "ths_get_json", return_value=payload),
        ):
            result = core.fetch_ths({"key": "ths:test", "code": "000001", "name": "测试"})

        self.assertIs(result["chronological"], False)
        self.assertEqual(result["feedType"], "concept_plus_related_scene_hot_feed")
        self.assertEqual(result["coverageStatus"], "non_chronological_hot_feed")
        comment_posts = [post for post in result["posts"] if post["kind"] == "comment"]
        self.assertEqual(len(comment_posts), 4)
        self.assertIs(comment_posts[0]["timeInherited"], False)
        self.assertTrue(all(post["parentPostId"] == "parent-1" for post in comment_posts))
        self.assertEqual(sum(bool(post["timeInherited"]) for post in comment_posts), 3)

    def test_grassroots_weight_filters_certified_and_is_direction_symmetric(self) -> None:
        colloquial_comment = {
            "title": "老师我今天被套了怎么办？",
            "kind": "comment",
            "originalLength": 14,
        }
        certified = {**colloquial_comment, "certified": True}
        long_bullish = {
            "title": "产业链 估值 研究框架 研报 投资策略 看多",
            "kind": "post",
            "originalLength": 700,
        }
        long_bearish = {
            "title": "产业链 估值 研究框架 研报 投资策略 看空",
            "kind": "post",
            "originalLength": 700,
        }

        self.assertEqual(core.grassroots_style_weight(colloquial_comment), 1.0)
        self.assertEqual(core.grassroots_style_weight(certified), 0.0)
        self.assertEqual(
            core.grassroots_style_weight(long_bullish),
            core.grassroots_style_weight(long_bearish),
        )
        self.assertLess(core.grassroots_style_weight(long_bullish), 0.1)

    def test_ths_related_scene_is_merged_and_ordinary_author_is_capped_daily(self) -> None:
        repeated_comment = {
            "id": "same-comment",
            "tag": "ordinary",
            "uid": "same-commenter",
            "nickname": "普通评论者",
            "content": "我今天还能上车吗？",
        }
        concept_rows = [
            ths_feed_row("concept-1", "same-author", comments=[repeated_comment]),
            ths_feed_row("concept-2", "same-author"),
            ths_feed_row("certified-1", "认证作者", author_extra={"identity_tag": "财经"}),
            ths_feed_row("medal-1", "勋章作者", author_extra={"medals": ["V"]}),
            ths_feed_row("official-1", "某基金官方账号"),
        ]
        scene_rows = [
            ths_feed_row("scene-1", "scene-author", comments=[repeated_comment]),
        ]
        calls: list[tuple[str, int]] = []

        def fake_hot_pages(code: str, market_id: int):
            calls.append((code, market_id))
            rows = concept_rows if code == "885959" else scene_rows
            return rows, 1, False, f"/hot_feed?code={code}"

        with (
            patch.object(
        col, "discover_ths_forum",
                return_value={
                    "forumCode": "885959",
                    "marketId": 48,
                    "forumName": "PCB概念",
                    "relatedScenes": [{
                        "forumCode": "nmcya2",
                        "marketId": 100002,
                        "forumName": "PCB社区",
                    }],
                    "discoveryStatus": "metadata_with_related_scenes",
                },
            ),
            patch.object(col, "fetch_ths_hot_pages", side_effect=fake_hot_pages),
        ):
            result = core.fetch_ths(
                {"key": "ths:sector:pcb:885959", "code": "885959", "name": "PCB概念"}
            )

        self.assertEqual(calls, [("885959", 48), ("nmcya2", 100002)])
        self.assertEqual(result["key"], "ths:sector:pcb:885959")
        self.assertEqual(result["platform"], "ths")
        self.assertEqual(result["pageCount"], 2)
        self.assertEqual(len(result["relatedScenes"]), 2)
        self.assertEqual(result["ordinaryPostCount"], 2)
        self.assertEqual(result["ordinaryCommentCount"], 1)
        self.assertEqual(result["relatedScenePostCount"], 1)
        self.assertEqual(
            len([post for post in result["posts"] if post["authorKey"] == "same-author"]),
            1,
        )
        self.assertFalse(any(post["authorKey"] in {"认证作者", "勋章作者"} for post in result["posts"]))

    def test_ths_discovers_related_scene_from_public_forum_metadata(self) -> None:
        payload = {
            "data": {
                "function_configs": {
                    "related_scenes": [{
                        "scene_code": "nmcya2",
                        "scene_market": 100002,
                        "scene_name": "PCB社区",
                        "talk_num": 321,
                    }]
                }
            }
        }

        with (
            patch.dict(col.THS_CONFIG, {}, clear=True),
            patch.object(col, "ths_get_json", return_value=payload) as request,
        ):
            config = col.discover_ths_forum({
                "key": "ths:sector:pcb:885959:test-discovery",
                "code": "885959",
                "marketId": 48,
                "name": "PCB概念",
                "preferScene": True,
            })

        path = request.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        self.assertEqual(query, {"code": ["885959"], "marketId": ["48"], "source": ["stock"]})
        self.assertEqual(config["discoveryStatus"], "metadata_with_related_scenes")
        self.assertIs(config["directFallback"], False)
        self.assertEqual(config["relatedScenes"], [{
            "forumCode": "nmcya2",
            "marketId": 100002,
            "forumName": "PCB社区",
            "talkCount": 321,
        }])

    def test_taoguba_parses_public_coolattr_filters_certified_and_marks_page_cap(self) -> None:
        rows = [
            taoguba_row("cert-auth", "认证用户", auth=1),
            taoguba_row("cert-title", "带头衔用户", user_title={"title": "达人"}),
            taoguba_row("official-name", "某证券官方账号"),
            taoguba_row("same-1", "same-author"),
            taoguba_row("same-2", "same-author"),
            taoguba_row("same-3", "same-author"),
            taoguba_row("same-prev-1", "same-author", action_date="2026-08-05 10:00:00"),
            taoguba_row("same-prev-2", "same-author", action_date="2026-08-05 11:00:00"),
            taoguba_row("same-prev-3", "same-author", action_date="2026-08-05 12:00:00"),
            taoguba_row("reply-1", "reply-author", row_type="R"),
        ]
        rows.extend(
            taoguba_row(f"ordinary-{index}", f"ordinary-author-{index}")
            for index in range(10)
        )
        self.assertEqual(len(rows), col.TAOGUBA_PAGE_SIZE)
        page = f"<script>var coolAttr={json.dumps(rows, ensure_ascii=False)}; var tempFeedID='x';</script>"

        with patch.object(col, "request_text", return_value=page) as request:
            result = core.fetch_taoguba(
                {"key": "taoguba:002916", "code": "002916", "name": "深南电路"}
            )

        request.assert_called_once_with(
            "https://www.tgb.cn/quotes/sz002916", "taoguba", attempts=2, timeout=12
        )
        self.assertEqual(result["rawRows"], 20)
        self.assertEqual(result["pageCount"], 1)
        self.assertIs(result["capHit"], True)
        self.assertEqual(result["paginationMode"], "anonymous_initial_page_only")
        self.assertEqual(result["coverageStatus"], "anonymous_page_cap_lower_bound")
        self.assertIs(result["volumeIsLowerBound"], True)
        self.assertEqual(result["excludedCertifiedCount"], 3)
        same_author = [post for post in result["posts"] if post["authorKey"] == "same-author"]
        self.assertEqual(len(same_author), 4)
        self.assertEqual(
            sorted(post["publishTime"][:10] for post in same_author),
            ["2026-08-05", "2026-08-05", "2026-08-06", "2026-08-06"],
        )
        reply = next(post for post in result["posts"] if post["id"] == "taoguba:reply-1")
        self.assertEqual(reply["kind"], "reply")
        self.assertNotIn("主题 reply-1", reply["title"])

    def test_score_fair_group_exposes_four_fixed_channel_seats(self) -> None:
        days = [date(2026, 8, 6), date(2026, 8, 5), date(2026, 8, 4)]
        platforms = ("eastmoney", "sina", "ths", "taoguba")
        results = {
            platform: {"platform": platform, "code": f"{platform}-unit"}
            for platform in platforms
        }
        analyses = {platform: {"sampleCount": 24} for platform in platforms}
        signals = {
            "optimism": 60.0,
            "fomo": 60.0,
            "panic": 10.0,
            "newcomer": 60.0,
            "intensity": 60.0,
            "attention": 90.0,
        }
        daily_rows = {
            platform: {
                day.isoformat(): {
                    "status": "observed",
                    "sampleCount": 24,
                    "signals": signals,
                }
                for day in days
            }
            for platform in platforms
        }
        unit_for_source = {platform: f"{platform}-unit" for platform in platforms}
        channel_specs = {
            platform: {
                "mode": "sector" if platform == "ths" else "company",
                "expectedUnits": [f"{platform}-unit"],
                "allowAttention": platform != "ths",
            }
            for platform in platforms
        }

        result = core.score_fair_group(
            results,
            list(platforms),
            analyses,
            daily_rows,
            unit_for_source,
            unit_for_source,
            days,
            channel_specs,
        )
        weights = result["diagnostics"]["channelWeights"]
        self.assertEqual([row["targetWeight"] for row in weights], [0.25] * 4)
        self.assertEqual(result["diagnostics"]["fixedChannelTargetWeight"], 0.25)
        self.assertIs(result["diagnostics"]["channelWeightsNeverRedistributed"], True)
        ths = next(row for row in result["diagnostics"]["platformSummary"] if row["key"] == "ths")
        self.assertEqual(ths["attentionWeight"], 0.0)
        self.assertEqual(result["diagnostics"]["channelSignals"]["ths"]["attention"], 50.0)

    def test_legacy_cache_is_enriched_without_claiming_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "forum_cache.json"
            cache_path.write_text(
                json.dumps({
                    "ths:legacy": {
                        "key": "ths:legacy",
                        "platform": "ths",
                        "fetchedAt": "2026-08-06T10:00:00+08:00",
                        "posts": [{
                            "id": "ths:legacy-comment",
                            "title": "这是历史评论内容",
                            "publishTime": "2026-08-06T09:30:00+08:00",
                            "kind": "comment",
                        }],
                    }
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                patch.object(
        core, "FORUM_CACHE_PATH", cache_path),
                patch.object(
        core, "NOW", datetime(2026, 8, 6, 10, 5, tzinfo=CN_TZ)),
            ):
                restored = core.load_forum_cache()["ths:legacy"]

        self.assertEqual(restored["rawRows"], 1)
        self.assertIs(restored["rawRowsEstimated"], True)
        self.assertEqual(restored["pageCount"], 1)
        self.assertIsNone(restored["capHit"])
        self.assertIs(restored["chronological"], False)
        self.assertEqual(restored["coverageDays"], ["2026-08-06"])
        self.assertIs(restored["freshFetchedNow"], False)
        self.assertEqual(restored["cacheAge"], 300)
        self.assertEqual(restored["coverageStatus"], "legacy_cache_unknown")
        self.assertIs(restored["posts"][0]["timeInherited"], True)


if __name__ == "__main__":
    unittest.main()
