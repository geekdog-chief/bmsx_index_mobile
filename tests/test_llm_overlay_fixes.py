# -*- coding: utf-8 -*-
"""2026-11-25 修复项针对性测试：P1-4 主键错配 / P2-1 截断 / P2-2 校验 / P1-2 时间戳。

不依赖 Ollama 与网络。运行：python -m pytest tests/test_llm_overlay_fixes.py -q
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import update_index as ui
import update_index_llm as uil
import llm_overlay as lo


def make_env(tmpdir, posts, scores):
    """造一份 forum_cache.json + llm_scores_ollama.json，并把 uil 的指针指过去。"""
    cache_path = Path(tmpdir) / "forum_cache.json"
    store_path = Path(tmpdir) / "llm_scores_ollama.json"
    cache_path.write_text(json.dumps({
        "src1": {"platform": "eastmoney", "fetchedAt": "2026-11-24T15:00:00+08:00",
                 "posts": posts}}, ensure_ascii=False), encoding="utf-8")
    store_path.write_text(json.dumps(scores, ensure_ascii=False), encoding="utf-8")
    uil.CACHE, uil.STORE = cache_path, store_path
    return cache_path, store_path


class TestP14TitleKeying(unittest.TestCase):
    """同标题不同 id 不得互相覆盖；歧义标题走词典兜底。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        posts = [
            {"id": "p1", "title": "明天大涨", "source": "东财A吧"},
            {"id": "p2", "title": "明天大涨", "source": "东财B吧"},
            {"id": "p3", "title": "独木标题", "source": "东财A吧"},
        ]
        scores = [
            {"id": "p1", "direction": 3, "fomo": 0, "panic": 0, "newcomer": 0,
             "sarcasm": 0, "ai_marketing": 0, "persona": "retail", "reason": "看多"},
            {"id": "p2", "direction": -3, "fomo": 0, "panic": 1, "newcomer": 0,
             "sarcasm": 0, "ai_marketing": 0, "persona": "retail", "reason": "看空"},
            {"id": "p3", "direction": 1, "fomo": 1, "panic": 0, "newcomer": 0,
             "sarcasm": 0, "ai_marketing": 0, "persona": "baoma", "reason": "轻多"},
        ]
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_title_different_source_not_mixed(self):
        # p1 强烈看多 +3 → 映射 ×2 = +6；p2 强烈看空 -3 → 恒等 -3。方向必须各归各
        self.assertEqual(uil.llm_sentiment_score("明天大涨", source="东财A吧"), 6)
        self.assertEqual(uil.llm_sentiment_score("明天大涨", source="东财B吧"), -3)

    def test_contains_follows_same_post(self):
        # p1 无 panic，p2 有 panic：同标题下 fomo/panic 必须跟对帖子
        self.assertFalse(uil.llm_contains("明天大涨", ui.PANIC_WORDS))   # p1 先解析入栈
        uil.llm_sentiment_score("明天大涨", source="东财B吧")
        self.assertTrue(uil.llm_contains("明天大涨", ui.PANIC_WORDS))    # p2

    def test_unique_title_resolved_without_source(self):
        self.assertEqual(uil.llm_sentiment_score("独木标题"), 2)

    def test_ambiguous_title_falls_back_to_dict(self):
        # 同标题对应多个 id 且 source 不明 → 词典兜底，绝不能错配羊驼分
        got = uil.llm_sentiment_score("明天大涨", source="未知吧")
        self.assertEqual(got, uil._orig_sentiment("明天大涨"))

    def test_ai_marketing_zeroed(self):
        posts = [{"id": "m1", "title": "进群看分析", "source": "东财A吧"}]
        scores = [{"id": "m1", "direction": 3, "fomo": 0, "panic": 0, "newcomer": 0,
                   "sarcasm": 0, "ai_marketing": 1, "persona": "none", "reason": "营销"}]
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()
        self.assertEqual(uil.llm_sentiment_score("进群看分析"), 0)


class TestP21Truncation(unittest.TestCase):
    """常态传全文；>2000 字截断并带 truncated 标记。"""

    def test_full_text_passed(self):
        self.tmp = tempfile.TemporaryDirectory()
        long_title = "中" * 1500  # 原实现 [:200] 会切掉
        cache_path = Path(self.tmp.name) / "forum_cache.json"
        cache_path.write_text(json.dumps({
            "s": {"platform": "sina", "fetchedAt": "2026-11-24T15:00:00+08:00",
                  "posts": [{"id": "x1", "title": long_title, "source": "新浪吧"}]}
        }, ensure_ascii=False), encoding="utf-8")
        old_cache = lo.CACHE
        lo.CACHE = cache_path
        try:
            posts = lo.collect_posts()
        finally:
            lo.CACHE = old_cache
        self.assertEqual(posts[0]["title"], long_title)
        self.assertNotIn("truncated", posts[0])

    def test_overlong_marked(self):
        self.tmp = tempfile.TemporaryDirectory()
        huge = "中" * 2500
        cache_path = Path(self.tmp.name) / "forum_cache.json"
        cache_path.write_text(json.dumps({
            "s": {"platform": "sina", "fetchedAt": "2026-11-24T15:00:00+08:00",
                  "posts": [{"id": "x2", "title": huge, "source": "新浪吧"}]}
        }, ensure_ascii=False), encoding="utf-8")
        old_cache = lo.CACHE
        lo.CACHE = cache_path
        try:
            posts = lo.collect_posts()
        finally:
            lo.CACHE = old_cache
        self.assertEqual(posts[0]["title"], huge[:2000])
        self.assertEqual(posts[0].get("truncated"), 1)
        self.tmp.cleanup()


class TestP22Validation(unittest.TestCase):
    """非法 direction/persona、重复 id、域外 id、缺项：全部不得入库。"""

    def base(self, **kw):
        it = {"id": "a1", "direction": 1, "persona": "retail"}
        it.update(kw)
        return it

    def test_direction_range(self):
        for bad in (4, -4, 99):
            with self.assertRaises(ValueError):
                lo.norm(self.base(direction=bad))
        for ok in (-3, 0, 3):
            self.assertEqual(lo.norm(self.base(direction=ok))["direction"], ok)

    def test_persona_choices(self):
        with self.assertRaises(ValueError):
            lo.norm(self.base(persona="bull"))
        self.assertEqual(lo.norm(self.base(persona="Baoma"))["persona"], "baoma")

    def test_bool_direction_rejected(self):
        with self.assertRaises(ValueError):
            lo.norm(self.base(direction=True))

    def test_validate_batch_dup_and_foreign_and_missing(self):
        batch = [{"id": "a1"}, {"id": "a2"}]
        ok = [self.base(), self.base(id="a2")]
        self.assertEqual(len(lo.validate_batch(ok, batch)), 2)
        with self.assertRaises(ValueError):  # 重复 id
            lo.validate_batch([self.base(), self.base()], [{"id": "a1"}])
        with self.assertRaises(ValueError):  # 输入之外的 id
            lo.validate_batch([self.base(id="zz")], [{"id": "a1"}])
        with self.assertRaises(ValueError):  # 缺项
            lo.validate_batch([self.base()], batch)


class TestEmotionFields(unittest.TestCase):
    """2026-11-25 羊驼情绪接管：fear/greed 严格校验 + 聚合口径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def base(self, **kw):
        it = {"id": "a1", "direction": 1, "fear": 0, "greed": 0, "persona": "retail"}
        it.update(kw)
        return it

    def test_norm_accepts_valid(self):
        for v in (0, 1, 2, 3):
            e = lo.norm(self.base(fear=v, greed=3 - v))
            self.assertEqual((e["fear"], e["greed"]), (v, 3 - v))

    def test_norm_accepts_int_string(self):
        e = lo.norm(self.base(fear="2"))
        self.assertEqual(e["fear"], 2)

    def test_norm_rejects_out_of_range(self):
        for bad in (-1, 4, 99):
            with self.assertRaises(ValueError):
                lo.norm(self.base(fear=bad))

    def test_norm_rejects_bool_and_float(self):
        with self.assertRaises(ValueError):
            lo.norm(self.base(fear=True))
        with self.assertRaises(ValueError):
            lo.norm(self.base(greed=1.8))  # 浮点不许偷偷截断成 1
        with self.assertRaises(ValueError):
            lo.norm(self.base(fear=None))
        with self.assertRaises(ValueError):
            lo.norm(self.base(greed="很贪婪"))

    def test_old_entries_default_zero(self):
        it = {"id": "old1", "direction": 1, "persona": "retail"}  # 无 fear/greed
        e = lo.norm(it)
        self.assertEqual((e["fear"], e["greed"]), (0, 0))

    def test_aggregation_skips_old_and_marketing(self):
        posts = [
            {"id": "e1", "title": "t", "source": "s"},
            {"id": "e2", "title": "t2", "source": "s2"},
            {"id": "old", "title": "t3", "source": "s3"},
        ]
        scores = [
            {"id": "e1", "direction": -1, "fear": 2, "greed": 0, "ai_marketing": 0},
            {"id": "e2", "direction": 0, "fear": 3, "greed": 3, "ai_marketing": 1},
            {"id": "old", "direction": 0},
        ]
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_emotion_dummy = None
        uil.load_llm_scores()
        snap = {"signals": {}}
        uil.compute_llm_emotion(snap)
        f, g = snap["signals"]["llmFear"], snap["signals"]["llmGreed"]
        self.assertAlmostEqual(f, round(((2 + 1.5 * 3) / 4) / 3 * 100, 1))
        self.assertAlmostEqual(g, round(((0 + 1.5 * 3) / 4) / 3 * 100, 1))

    def test_aggregation_no_data_writes_nothing(self):
        posts = [{"id": "o1", "title": "t", "source": "s"}]
        scores = [{"id": "o1", "direction": 0}]
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()
        snap = {"signals": {}}
        uil.compute_llm_emotion(snap)
        self.assertNotIn("llmFear", snap["signals"])


class TestP12FreshNow(unittest.TestCase):
    """apply_overlays 每轮刷新 ui.NOW；fetch_source 包装层显式传当前时间。"""

    def test_now_refreshed(self):
        stale = ui.NOW
        uil.apply_overlays()
        self.assertGreaterEqual(ui.NOW, stale)

    def test_fetch_source_passes_as_of(self):
        seen = {}
        def fake_fetcher(spec, as_of):
            seen["as_of"] = as_of
            return {}
        # 用桩替换原函数，验证 None 的 as_of 被替换为当前时间而非模块常量
        uil._orig_fetch_source = lambda spec, as_of=None: seen.setdefault("as_of", as_of) or {}
        uil._fresh_fetch_source({}, as_of=None)
        from datetime import datetime
        self.assertIsInstance(seen["as_of"], datetime)


if __name__ == "__main__":
    unittest.main()
