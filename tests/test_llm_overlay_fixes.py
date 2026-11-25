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


class TestLlmaudit(unittest.TestCase):
    """羊驼抽检：确定性选取、类别覆盖、字段完整。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_sample_structure_and_determinism(self):
        import datetime as _dt
        today = _dt.date.today().isoformat()
        posts, scores = [], []
        for i in range(30):
            posts.append({"id": f"s{i}", "title": f"标题{i}号", "source": "东财",
                          "publishTime": f"{today}T10:00:00"})
            scores.append({"id": f"s{i}", "direction": (i % 7) - 3, "fear": i % 4,
                           "greed": (i * 2) % 4, "ai_marketing": 1 if i % 9 == 0 else 0,
                           "persona": "retail", "reason": "理由"})
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()
        a, b = {"signals": {}}, {"signals": {}}
        uil.collect_llm_sample(a)
        uil.collect_llm_sample(b)
        rows = a["llmSample"]
        self.assertGreater(len(rows), 0)
        self.assertLessEqual(len(rows), 24)
        for r in rows:  # 字段齐全
            for k in ("title", "platform", "direction", "fear", "greed", "persona", "marketing"):
                self.assertIn(k, r)
        self.assertEqual(a["llmSample"], b["llmSample"])  # 确定性
        self.assertTrue(any(r["marketing"] for r in rows))  # 营销类别被覆盖

    def test_no_candidates_no_key(self):
        make_env(self.tmp.name, [], [])
        uil.load_llm_scores()
        snap = {"signals": {}}
        uil.collect_llm_sample(snap)
        self.assertNotIn("llmSample", snap)


class TestEmotionWindow(unittest.TestCase):
    """15天滑动窗口：窗口内统计、窗口外剔除、样本不足逐级兜底。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.today = __import__("datetime").date.today().isoformat()
        self.old_day = "2020-01-01"

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_excludes_old_posts(self):
        posts, scores = [], []
        for i in range(3):  # 白酒：3条窗口内新帖，全贪婪
            posts.append({"id": f"new{i}", "title": "新", "source": "s",
                          "publishTime": f"{self.today}T10:00:00", "sourceCode": "600519"})
            scores.append({"id": f"new{i}", "direction": 3, "fear": 0, "greed": 3})
        posts.append({"id": "old1", "title": "旧", "source": "s",
                      "publishTime": f"{self.old_day}T10:00:00", "sourceCode": "600519"})
        scores.append({"id": "old1", "direction": -3, "fear": 3, "greed": 0})  # 老帖恐慌，不应进窗口
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()
        # 窗口过滤本身：15天窗口只收新帖
        cache = __import__("json").loads(uil.CACHE.read_text(encoding="utf-8"))
        windowed = uil._recent_posts(cache)[uil.EMOTION_WINDOW_DAYS]
        ids = {str(x.get("id")) for x in windowed}
        self.assertIn("new0", ids)
        self.assertNotIn("old1", ids)
        # 板块函数15天窗口：老帖恐慌不进账，白酒应偏贪婪侧
        snap = {"sectorIndices": [{"name": "白酒", "heat": 50.0, "heatRank": 1}]}
        uil.compute_llm_sector_emotion(snap)
        self.assertGreater(snap["sectorIndices"][0]["emotion"], 50.0)

    def test_sector_fallback_to_old_posts(self):
        """冷门板块兜底：15天窗口内无帖，向前追溯至老帖也要给出读数。"""
        posts, scores = [], []
        for i in range(3):  # 券商保险：只有2020年的老帖，恐慌
            posts.append({"id": f"oldz{i}", "title": "旧券", "source": "s",
                          "publishTime": f"{self.old_day}T10:00:00", "sourceCode": "300059"})
            scores.append({"id": f"oldz{i}", "direction": -3, "fear": 3, "greed": 0})
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()
        snap = {"sectorIndices": [{"name": "券商保险", "heat": 50.0, "heatRank": 1}]}
        uil.compute_llm_sector_emotion(snap)
        self.assertEqual(snap["sectorIndices"][0]["emotion"], 0.0)  # 追溯到老帖，全恐慌=0


class TestSectorEmotion(unittest.TestCase):
    """板块情绪注入：sourceCode 映射、最小样本门槛、混合权重、重排名。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_blend_and_rerank(self):
        # 两个板块：券商保险(300059×3帖恐慌) vs 白酒(600519×3帖狂热)
        posts = []
        scores = []
        import datetime as _dt
        today = _dt.date.today().isoformat()
        for i in range(3):
            posts.append({"id": f"zq{i}", "title": f"券{i}", "sourceCode": "300059", "source": "s",
                          "publishTime": f"{today}T10:00:00"})
            scores.append({"id": f"zq{i}", "direction": -3, "fear": 3, "greed": 0})
        for i in range(3):
            posts.append({"id": f"bj{i}", "title": f"酒{i}", "sourceCode": "600519", "source": "s",
                          "publishTime": f"{today}T10:00:00"})
            scores.append({"id": f"bj{i}", "direction": 3, "fear": 0, "greed": 3})
        # 样本不足的板块（1 帖）：不参与情绪
        posts.append({"id": "dc0", "title": "电池", "sourceCode": "300750", "source": "s",
                      "publishTime": f"{today}T10:00:00"})
        scores.append({"id": "dc0", "direction": 1, "fear": 3, "greed": 0})
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()
        snap = {"sectorIndices": [
            {"name": "白酒", "heat": 50.0, "heatRank": 1},
            {"name": "券商保险", "heat": 60.0, "heatRank": 2},
            {"name": "新能源整车", "heat": 55.0, "heatRank": None},
        ]}
        uil.compute_llm_sector_emotion(snap)
        secs = {x["name"]: x for x in snap["sectorIndices"]}
        # 券商保险：情绪 = 50+(0-100)/2 = 0 → heat = 0.8*60+0.2*0 = 48
        self.assertEqual(secs["券商保险"]["emotion"], 0.0)
        self.assertAlmostEqual(secs["券商保险"]["heat"], 48.0)
        self.assertEqual(secs["券商保险"]["activityHeat"], 60.0)
        # 白酒：情绪=100 → heat = 0.8*50+0.2*100 = 60，反超券商保险拿第 1
        self.assertEqual(secs["白酒"]["emotion"], 100.0)
        self.assertAlmostEqual(secs["白酒"]["heat"], 60.0)
        self.assertEqual(secs["白酒"]["heatRank"], 1)
        self.assertEqual(secs["券商保险"]["heatRank"], 2)
        # 无名次板块不赋名次；样本不足板块 emotion 为 None 且 heat 不动
        self.assertIsNone(secs["新能源整车"]["heatRank"])
        self.assertIsNone(secs["新能源整车"]["emotion"])
        self.assertEqual(secs["新能源整车"]["heat"], 55.0)
        self.assertEqual(secs["新能源整车"]["heat"], 55.0)


class TestStudyLog(unittest.TestCase):
    """研究瘦日志：日期、恐惧/贪婪、板块情绪写入 CSV 行。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_line(self):
        import datetime as _dt
        today = _dt.date.today().isoformat()
        posts, scores = [], []
        for i in range(3):  # 白酒满3条样本，板块情绪有值
            posts.append({"id": f"x{i}", "title": "t", "source": "s", "sourceCode": "600519",
                          "publishTime": f"{today}T10:00:00"})
            scores.append({"id": f"x{i}", "direction": 3, "fear": 0, "greed": 3})
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()
        old_log = uil.STUDY_LOG
        uil.STUDY_LOG = Path(self.tmp.name) / "study_emotion.csv"
        try:
            snap = {"tradingDays": ["2026-11-25"], "signals": {},
                    "sectorIndices": [{"name": "白酒", "heat": 50.0, "heatRank": 1}]}
            uil.compute_llm_sector_emotion(snap)
            uil.compute_llm_emotion(snap)
            uil.append_study_log(snap)
            text = uil.STUDY_LOG.read_text(encoding="utf-8")
            lines = text.strip().splitlines()
            self.assertEqual(len(lines), 2)  # 表头 + 1 行
            self.assertTrue(lines[1].startswith("2026-11-25,"))
            self.assertIn("白酒", lines[1])
            # upsert：同日再写只留一行，日期更新为新值
            snap2 = {"tradingDays": ["2026-11-25"], "signals": {"llmFear": 10.0, "llmGreed": 90.0},
                     "sectorIndices": []}
            uil.append_study_log(snap2)
            lines2 = uil.STUDY_LOG.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines2), 2)
            self.assertIn("10.0,90.0", lines2[1])
            # 隔日追加成第二行
            snap3 = {"tradingDays": ["2026-11-26"], "signals": {"llmFear": 20.0, "llmGreed": 30.0},
                     "sectorIndices": []}
            uil.append_study_log(snap3)
            lines3 = uil.STUDY_LOG.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines3), 3)
        finally:
            uil.STUDY_LOG = old_log


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
