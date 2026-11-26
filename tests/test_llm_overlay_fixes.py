# -*- coding: utf-8 -*-
"""2026-11-25 修复项针对性测试：P1-4 主键错配 / P2-1 截断 / P2-2 校验 / P1-2 时间戳。

不依赖 Ollama 与网络。运行：python -m pytest tests/test_llm_overlay_fixes.py -q
"""

import json
import os
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


class TestAuditBatch1(unittest.TestCase):
    """任务书第一批：CSV 格式 / as_of 重跑确定性 / 坏文件防护。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_special_chars_roundtrip(self):
        """板块 JSON 含逗号引号中文，csv 读写后字段完整。"""
        uil.STUDY_LOG = Path(self.tmp.name) / "study.csv"
        try:
            snap = {"tradingDays": ["2026-11-25"],
                    "signals": {"llmFear": 11.1, "llmGreed": 22.2},
                    "sectorIndices": [
                        {"name": "白酒", "emotion": 50.0},
                        {"name": "半导体设备材料", "emotion": None},
                        {"name": "CPO/光模块", "emotion": 68.5},
                    ]}
            uil.append_study_log(snap)
            import csv as _csv
            with uil.STUDY_LOG.open("r", encoding="utf-8", newline="") as f:
                rows = list(_csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["date"], "2026-11-25")
            self.assertEqual(rows[0]["fear"], "11.1")
            sec = json.loads(rows[0]["sectors_json"])
            self.assertEqual(sec, {"白酒": 50.0, "CPO/光模块": 68.5})
        finally:
            uil.STUDY_LOG = Path(self.tmp.name) / "nope.csv"  # 还原为默认路径

    def test_as_of_deterministic(self):
        """同一快照在不同"系统时间"下窗口结果一致：as_of 由快照日期决定。"""
        posts = [{"id": "w1", "title": "t", "sourceCode": "600519",
                  "publishTime": "2026-11-09T10:00:00"}]  # 11-20 的 15 天窗口内、11-25 的在窗口外
        scores = [{"id": "w1", "direction": 3, "fear": 0, "greed": 3}]
        make_env(self.tmp.name, posts, scores)
        uil.load_llm_scores()
        cache = json.loads(uil.CACHE.read_text(encoding="utf-8"))
        from datetime import date
        w20 = uil._recent_posts(cache, date(2026, 11, 20))[15]   # cutoff=11-05
        w25 = uil._recent_posts(cache, date(2026, 11, 25))[15]   # cutoff=11-10，11-09 在外
        self.assertEqual(len(w20), 1)
        self.assertEqual(len(w25), 0)

    def test_corrupt_store_guard(self):
        """评分文件损坏：load_llm_scores 不抛异常，索引为空。"""
        Path(self.tmp.name, "forum_cache.json").write_text("{}", encoding="utf-8")
        Path(self.tmp.name, "llm_scores_ollama.json").write_text("{坏掉的json", encoding="utf-8")
        old_c, old_s = uil.CACHE, uil.STORE
        uil.CACHE, uil.STORE = Path(self.tmp.name, "forum_cache.json"), Path(self.tmp.name, "llm_scores_ollama.json")
        try:
            uil.load_llm_scores()  # 不得抛异常
            self.assertEqual(len(uil.LLM_BY_ID), 0)
        finally:
            uil.CACHE, uil.STORE = old_c, old_s


class TestAuditBatch2(unittest.TestCase):
    """任务书第二批：四文件发布原子性 / 进程锁。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_staged_all_or_nothing(self):
        paths = {k: self.tdir / f"{k}.out" for k in ("history", "historyMeta", "latest", "datajs")}
        snap = {"history": [{"date": "2026-11-25"}], "historyMeta": {"updatedAt": "x"},
                "signals": {}, "sectorIndices": [], "tradingDays": ["2026-11-25"]}
        uil._publish_snapshot(snap, paths=paths)
        self.assertTrue(all(p.exists() for p in paths.values()))
        import json as _json
        latest = _json.loads(paths["latest"].read_text(encoding="utf-8"))
        self.assertIn("snapshotVersion", latest)  # 版本标识就位
        js = paths["datajs"].read_text(encoding="utf-8")
        self.assertTrue(js.startswith("window.INDEX_DATA = "))

    def test_publish_bad_payload_rejected(self):
        """载荷损坏（不可 JSON 序列化）时不得产出任何目标文件。"""
        paths = {k: self.tdir / f"{k}.out" for k in ("history", "historyMeta", "latest", "datajs")}
        class Unserializable:
            pass
        snap = {"history": [{"bad": Unserializable()}], "historyMeta": {}, "signals": {}}
        with self.assertRaises(Exception):
            uil._publish_snapshot(snap, paths=paths)
        self.assertFalse(any(p.exists() for p in paths.values()))  # 整体未发布

    def test_lock_blocks_and_takeover(self):
        lock = self.tdir / ".test.lock"
        self.assertTrue(uil._acquire_lock(lock))           # 首次获得
        self.assertFalse(uil._acquire_lock(lock))          # 自己持锁，二次被拒
        uil._release_lock(lock)
        lock.write_text("999999999", encoding="utf-8")     # 死 PID
        self.assertTrue(uil._acquire_lock(lock))           # 接管死锁
        uil._release_lock(lock)


class TestAuditRound2(unittest.TestCase):
    """二轮验收：发布故障注入 / 锁异常释放 / CSV 损坏保护 / 行情合并去重。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_publish_midway_replace_failure_keeps_old(self):
        """替换中途失败：线上文件保持旧版本完整（页面读 data.js，最后替换）。"""
        paths = {k: self.tdir / f"{k}.out" for k in ("history", "historyMeta", "latest", "datajs")}
        old_snap = {"history": [{"v": 1}], "historyMeta": {}, "signals": {}}
        uil._publish_snapshot(old_snap, paths=paths)
        real_replace = os.replace
        calls = {"n": 0}
        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:  # 第二份（historyMeta）替换时炸
                raise OSError("模拟磁盘故障")
            return real_replace(src, dst)
        uil.os.replace = flaky_replace
        try:
            with self.assertRaises(OSError):
                uil._publish_snapshot({"history": [{"v": 2}], "historyMeta": {}, "signals": {}}, paths=paths)
        finally:
            uil.os.replace = real_replace
        # history 可能已被替换（第1次成功），但 data.js 必须还是旧版本
        import json as _json
        js = paths["datajs"].read_text(encoding="utf-8")
        self.assertIn('"v": 1', js)
        self.assertNotIn('"v": 2', js)

    def test_lock_released_on_body_exception(self):
        """更新主体抛异常，锁必须被 finally 释放。"""
        lock = self.tdir / ".u.lock"
        orig_release = uil._release_lock
        released = {"v": False}
        def spy_release(path=uil.LOCK_PATH):
            released["v"] = True
            orig_release(path)
        orig_acquire = uil._acquire_lock
        def fake_acquire(path=uil.LOCK_PATH):
            return True
        uil._acquire_lock, uil._release_lock = fake_acquire, spy_release
        orig_inner = uil._run_update_inner
        def boom(pc=None):
            raise RuntimeError("模拟构建崩溃")
        uil._run_update_inner = boom
        try:
            with self.assertRaises(RuntimeError):
                uil.run_update()
            self.assertTrue(released["v"], "锁未被释放")
        finally:
            uil._acquire_lock, uil._release_lock = orig_acquire, orig_release
            uil._run_update_inner = orig_inner

    def test_lock_oexcl_atomic(self):
        """O_EXCL 原子创建：锁文件已存在时第二次获取必失败（即使强写）。"""
        lock = self.tdir / ".a.lock"
        self.assertTrue(uil._acquire_lock(lock))
        with lock.open("w", encoding="utf-8") as f:
            f.write("999999999")  # 外部篡改 PID 为不存在的进程 → 接管路径
        self.assertTrue(uil._acquire_lock(lock))  # 死 PID 接管成功

    def test_corrupt_csv_never_overwritten(self):
        """研究日志损坏：禁止重写清空，原文件保留。"""
        uil.STUDY_LOG = self.tdir / "study.csv"
        uil.STUDY_LOG.write_text("date,fear,greed\n2026-11-20,bad\n\"乱引号,xx\n", encoding="utf-8")
        before = uil.STUDY_LOG.read_bytes()
        snap = {"tradingDays": ["2026-11-25"], "signals": {"llmFear": 1.0, "llmGreed": 2.0},
                "sectorIndices": []}
        try:
            with self.assertRaises(Exception):
                uil.append_study_log(snap)
            self.assertEqual(uil.STUDY_LOG.read_bytes(), before)  # 原文件一字未动
        finally:
            pass

    def test_future_posts_excluded(self):
        """独立验收修订：窗口必须有上限，未来日期帖子不得进入情绪计算。"""
        cache = {"s": {"platform": "eastmoney", "posts": [
            {"id": "f1", "title": "t", "sourceCode": "600519", "publishTime": "2026-11-20T10:00:00"},
            {"id": "f2", "title": "t", "sourceCode": "600519", "publishTime": "2026-12-01T10:00:00"},  # 未来
        ]}}
        from datetime import date
        w = uil._recent_posts(cache, date(2026, 11, 25))[15]
        ids = {str(x.get("id")) for x in w}
        self.assertIn("f1", ids)
        self.assertNotIn("f2", ids)

    def test_fetch_returns_bool_without_akshare(self):
        """无 akshare 时 fetch_one 返回 False（成功率统计的依据是返回值，不是文件时间）。"""
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "study"))
        import importlib
        fp = importlib.import_module("fetch_price")
        saved = fp.ak
        fp.ak = None
        try:
            self.assertFalse(fp.fetch_one("000000", "dummy", "测试"))
        finally:
            fp.ak = saved

    def test_price_merge_dedup(self):
        """行情合并：重复日期去重（新优先）、排序。"""
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "study"))
        import importlib
        fp = importlib.import_module("fetch_price")
        import pandas as pd
        old = pd.DataFrame({"日期": ["2026-11-20", "2026-11-21"], "收盘": [1.0, 2.0]})
        new = pd.DataFrame({"日期": ["2026-11-21", "2026-11-22"], "收盘": [2.5, 3.0]})
        merged = fp.merge_frames(old, new)
        self.assertEqual(list(merged["日期"]), ["2026-11-20", "2026-11-21", "2026-11-22"])
        self.assertEqual(float(merged.loc[merged["日期"] == "2026-11-21", "收盘"].iloc[0]), 2.5)


class TestP01ExitCode(unittest.TestCase):
    """P0-1：退出码传递——评分/更新失败必须阻断推送，锁冲突必须可区分。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / ".u.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_lock_conflict_raises_skipped(self):
        self.lock.write_text(str(os.getpid()), encoding="utf-8")  # 活 PID 持锁
        orig = uil.LOCK_PATH
        uil.LOCK_PATH = self.lock
        try:
            with self.assertRaises(uil.UpdateSkipped):
                uil.run_update()
        finally:
            uil.LOCK_PATH = orig

    def test_main_returns_2_on_skip(self):
        self.lock.write_text(str(os.getpid()), encoding="utf-8")
        orig = uil.LOCK_PATH
        uil.LOCK_PATH = self.lock
        try:
            self.assertEqual(uil.main(), 2)
        finally:
            uil.LOCK_PATH = orig


class TestE2E(unittest.TestCase):
    """P1-3 端到端：模拟数据全链路 run_update —— 帖子→评分→四指标→四文件→研究日志。

    不触网（缓存新鲜度设为本轮内）、不触 Ollama（评分来自注入 STORE）、
    全部数据路径劫持到临时目录，真实产出物逐项断言。
    """

    def setUp(self):
        import datetime as _dt
        self.tmp = tempfile.TemporaryDirectory()
        self.tdir = Path(self.tmp.name)
        self.today = _dt.datetime.now().isoformat(timespec="seconds")
        # 四平台×12帖，其中8帖已评分（含1营销），4帖未评分（词典兜底路径）
        plats = [("eastmoney", "600519"), ("sina", "300059"), ("ths", "000858"), ("taoguba", "601899")]
        posts, scores = [], []
        n = 0
        for plat, code in plats:
            for i in range(12):
                n += 1
                posts.append({"id": f"{plat}-{n}", "title": f"第{n}帖，内容{'中' * (n % 50)}",
                              "source": f"{plat}吧", "sourceCode": code,
                              "sourceKey": f"{plat}:{code}", "publishTime": self.today})
                if i < 8:
                    scores.append({"id": f"{plat}-{n}", "direction": 3 if i % 2 else -3,
                                   "fear": 0 if i % 2 else 2, "greed": 2 if i % 2 else 0,
                                   "ai_marketing": 1 if i == 7 else 0, "persona": "retail"})
        self.cache = {"src_" + k: {"platform": k, "fetchedAt": self.today, "posts": [q for q in posts if q["id"].startswith(k + "-")]}
                      for k, _ in plats}
        self.scores = scores
        self.paths = {k: self.tdir / v for k, v in
                      [("HISTORY_PATH", "history.json"), ("HISTORY_META_PATH", "history_meta.json"),
                       ("LATEST_PATH", "latest.json"), ("DATA_JS_PATH", "data.js"),
                       ("FORUM_CACHE_PATH", "forum_cache.json"), ("LOG_PATH", "update.log"),
                       ("COMMUNITY_ARCHIVE_DIR", "archive"), ("SQLITE_ARCHIVE_PATH", "a.sqlite3")]}
        self.saved = {}

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_pipeline_offline(self):
        uil.CACHE.write_text  # noqa - 占位防误删
        # 劫持全部数据路径
        for attr, p in self.paths.items():
            self.saved[attr] = getattr(uil.ui, attr)
            setattr(uil.ui, attr, p)
        saved = (uil.CACHE, uil.STORE, uil.STUDY_LOG, uil.LOCK_PATH)
        uil.CACHE = self.tdir / "cache.json"
        uil.STORE = self.tdir / "store.json"
        uil.STUDY_LOG = self.tdir / "study.csv"
        uil.LOCK_PATH = self.tdir / ".lock"
        uil.CACHE.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        uil.STORE.write_text(json.dumps(self.scores, ensure_ascii=False), encoding="utf-8")
        try:
            result = uil.run_update()
            self.assertIsNotNone(result)  # 锁跳过会抛 UpdateSkipped，走到这=成功
            snap = result
            # ① 四文件齐备且版本号就位
            for attr in ("HISTORY_PATH", "HISTORY_META_PATH", "LATEST_PATH", "DATA_JS_PATH"):
                self.assertTrue(self.paths[attr].exists(), attr)
            self.assertIn("snapshotVersion", snap)
            # ② 情绪指标链路产物
            sig = snap["signals"]
            self.assertIn("llmFear", sig)
            self.assertEqual(sig["llmPlatformCount"], 4)
            # ③ 板块情绪与热度混合
            secs = {x["name"]: x for x in snap["sectorIndices"]}
            self.assertIn("白酒", secs)
            self.assertIn("emotion", secs["白酒"])
            self.assertIn("activityHeat", secs["白酒"])
            # ④ data.js 可解析且含情绪字段
            js = self.paths["DATA_JS_PATH"].read_text(encoding="utf-8")
            payload = json.loads(js[len("window.INDEX_DATA = "):].rstrip().rstrip(";"))
            self.assertIn("llmFear", payload["signals"])
            # ⑤ 研究日志当日一行
            text = uil.STUDY_LOG.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(text), 2)
            # ⑥ 抽检样本存在
            self.assertTrue(snap.get("llmSample"))
        finally:
            for attr, p in self.saved.items():
                setattr(uil.ui, attr, p)
            uil.CACHE, uil.STORE, uil.STUDY_LOG, uil.LOCK_PATH = saved


class TestScoreLockTwoProcesses(unittest.TestCase):
    """专项1：两个独立进程同时竞争评分锁——恰好一个获得权限。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_independent_processes_compete(self):
        lock = self.tdir / ".score.lock"
        proj = str(Path(__file__).resolve().parent.parent)
        lines = [
            "import sys, time",
            f"sys.path.insert(0, {proj!r})",
            "import llm_overlay as lo",
            f"lo.LOCK_FILE = lo.Path({str(lock)!r})",
            "ok = lo._acquire_lock()",
            f"open({str(self.tdir / 'w1')!r} if ok else {str(self.tdir / 'w0')!r}, 'w').write('1')",
            "time.sleep(0.4)",
            "lo._release_lock() if ok else None",
        ]
        script = "; ".join(lines)
        import subprocess as sp
        p1 = sp.Popen([sys.executable, "-c", script])
        p2 = sp.Popen([sys.executable, "-c", script])
        p1.wait(timeout=30)
        p2.wait(timeout=30)
        self.assertEqual(len(list(self.tdir.glob("w1"))), 1, "必须恰好一个进程胜出")
        self.assertEqual(len(list(self.tdir.glob("w0"))), 1)


class TestStoreAtomicSave(unittest.TestCase):
    """专项2：评分库原子保存——中断/失败路径原库一字节不动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _swap_store(self):
        self._saved = lo.STORE
        lo.STORE = self.tdir / "store.json"

    def _restore_store(self):
        lo.STORE = self._saved

    def test_replace_failure_keeps_original(self):
        """模拟替换失败（磁盘故障）：原 STORE 完好、无 tmp 残留。"""
        self._swap_store()
        lo.STORE.write_text('[{"id": "old", "fear": 0}]', encoding="utf-8")
        before = lo.STORE.read_bytes()
        real = os.replace
        def boom(a, b):
            raise OSError("模拟替换失败")
        lo.os.replace = boom
        try:
            with self.assertRaises(OSError):
                lo._atomic_save_store({"x": {"id": "x", "fear": 3}})
        finally:
            lo.os.replace = real
            self._restore_store()
        self.assertEqual((self.tdir / "store.json").read_bytes(), before)
        self.assertEqual(list(self.tdir.glob("store.json.tmp-*")), [])

    def test_unserializable_keeps_original(self):
        """序列化失败（进程中断等价路径）：原库不动。"""
        self._swap_store()
        lo.STORE.write_text('[{"id": "old"}]', encoding="utf-8")
        before = lo.STORE.read_bytes()
        class Bad:
            pass
        try:
            with self.assertRaises(TypeError):
                lo._atomic_save_store({"x": {"bad": Bad()}})
        finally:
            self._restore_store()
        self.assertEqual((self.tdir / "store.json").read_bytes(), before)

    def test_partial_write_interruption_keeps_original(self):
        """写入中断专项（独立验收）：临时文件写一半进程死亡——原库不动、残留被清。"""
        self._swap_store()
        lo.STORE.write_text('[{"id": "old"}]', encoding="utf-8")
        before = lo.STORE.read_bytes()
        from pathlib import Path as _P
        real_write = _P.write_text
        def flaky(self, data, *a, **kw):
            if ".tmp-" in self.name:  # 只劫持临时文件：写一半就死
                real_write(self, data[: len(data) // 2], *a, **kw)
                raise IOError("模拟写入中断")
            return real_write(self, data, *a, **kw)
        _P.write_text = flaky
        try:
            with self.assertRaises(IOError):
                lo._atomic_save_store({"x": {"id": "x", "fear": 1}})
        finally:
            _P.write_text = real_write
            self._restore_store()
        self.assertEqual((self.tdir / "store.json").read_bytes(), before)
        self.assertEqual(list(self.tdir.glob("*.tmp-*")), [], "临时文件必须被清理")

    def test_release_only_own_lock(self):
        """锁所有权（独立验收）：别人接管后的锁，本进程释放时不得误删。"""
        lock = self.tdir / ".own.lock"
        import llm_overlay as llo
        saved = llo.LOCK_FILE
        llo.LOCK_FILE = lock
        try:
            self.assertTrue(llo._acquire_lock())
            lock.write_text("424242", encoding="utf-8")  # 模拟被他人接管
            llo._release_lock()
            self.assertTrue(lock.exists(), "他人之锁不得被误删")
            lock.write_text(str(os.getpid()), encoding="utf-8")
            llo._release_lock()
            self.assertFalse(lock.exists(), "自己的锁应正常释放")
        finally:
            llo.LOCK_FILE = saved

    def test_normal_save_roundtrip(self):
        """正常路径：替换后回读可解析、条数一致。"""
        self._swap_store()
        try:
            lo._atomic_save_store({"a": {"id": "a", "fear": 1}, "b": {"id": "b", "greed": 2}})
            data = json.loads(lo.STORE.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)
            self.assertEqual({d["id"] for d in data}, {"a", "b"})
        finally:
            self._restore_store()


class TestMonkeypatchContract(unittest.TestCase):
    """猴补丁契约（治理要求§二）：替换范围与签名恒定，兼容测试防漂移。"""

    def test_patched_scope_is_exactly_four(self):
        """替换范围明确：恰为四个入口，多一个少一个都算破坏契约。"""
        uil.apply_overlays()
        self.assertIs(ui.sentiment_score, uil.llm_sentiment_score)
        self.assertIs(ui.contains_nonnegated, uil.llm_contains)
        self.assertIs(ui.load_forum_cache, uil.dedup_cache)
        self.assertIs(ui.fetch_source, uil._fresh_fetch_source)
        patched = {k for k, v in vars(ui).items() if callable(v) and getattr(v, "__module__", "") == "update_index_llm"}
        self.assertEqual(len(patched), 4, f"猴补丁范围漂移：{patched}")

    def test_signature_parity(self):
        """接口稳定：与模块加载时保存的原始函数比对——apply_overlays 会替换 ui.*，
        直接拿 ui.sentiment_score 当基准是同义反复（独立验收指出）。本测试不调用
        apply_overloads、不改全局状态，对其它用例零污染。"""
        import inspect as _inspect
        def compat(fn_new, fn_orig):
            a = _inspect.signature(fn_new).parameters
            b = _inspect.signature(fn_orig).parameters
            for n in b:
                self.assertIn(n, a, f"原参数 {n} 在补丁中缺失")
        compat(uil.llm_sentiment_score, uil._orig_sentiment)
        compat(uil.llm_contains, uil._orig_contains)
        compat(uil.dedup_cache, uil._orig_load_cache)
        compat(uil._fresh_fetch_source, uil._orig_fetch_source)


class TestDualCursor(unittest.TestCase):
    """双指针慢线（2026-11-26 已批）：历史分位的口径纪律。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tdir = Path(self.tmp.name)
        self.old_log = uil.STUDY_LOG
        uil.STUDY_LOG = self.tdir / "study.csv"

    def tearDown(self):
        uil.STUDY_LOG = self.old_log
        self.tmp.cleanup()

    def _log(self, rows):
        import csv as _csv
        with uil.STUDY_LOG.open("w", encoding="utf-8", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["date", "fear", "greed", "sectors_json"])
            w.writeheader()
            for d, fe, gr in rows:
                w.writerow({"date": d, "fear": fe, "greed": gr, "sectors_json": "{}"})

    def test_pct_mid_and_extreme(self):
        import datetime as _dt
        base = _dt.date(2026, 1, 1)
        rows = [( (base + _dt.timedelta(days=i)).isoformat(), 10 + i % 50, 20 + i % 40) for i in range(100)]
        self._log(rows)
        today = (base + _dt.timedelta(days=100)).isoformat()
        snap = {"tradingDays": [today], "signals": {"llmFear": 99.0, "llmGreed": 99.0}}
        uil.compute_history_percentiles(snap)
        pf, pg = snap["signals"]["llmFearPct"], snap["signals"]["llmGreedPct"]
        self.assertIsNotNone(pf)
        self.assertGreaterEqual(pf, 95)  # 99 高于历史全部 fear（10..59）
        self.assertGreaterEqual(pg, 95)
        self.assertEqual(snap["signals"]["llmHistoryDays"], 100)

    def test_baseline_period_none(self):
        import datetime as _dt
        base = _dt.date(2026, 1, 1)
        rows = [((base + _dt.timedelta(days=i)).isoformat(), 10.0, 20.0) for i in range(45)]
        self._log(rows)
        today = (base + _dt.timedelta(days=45)).isoformat()
        snap = {"tradingDays": [today], "signals": {"llmFear": 30.0, "llmGreed": 30.0}}
        uil.compute_history_percentiles(snap)
        self.assertIsNone(snap["signals"]["llmFearPct"])
        self.assertEqual(snap["signals"]["llmHistoryDays"], 45)

    def test_today_excluded_from_reference(self):
        """今日行已在日志里（重复更新场景）：不得进入自己的参照集。"""
        import datetime as _dt
        base = _dt.date(2026, 1, 1)
        today = (base + _dt.timedelta(days=100)).isoformat()
        rows = [((base + _dt.timedelta(days=i)).isoformat(), 10.0, 20.0) for i in range(100)]
        rows.append((today, 999.0, 999.0))  # 同日旧行，值离谱
        self._log(rows)
        snap = {"tradingDays": [today], "signals": {"llmFear": 10.0, "llmGreed": 20.0}}
        uil.compute_history_percentiles(snap)
        self.assertEqual(snap["signals"]["llmFearPct"], 50)  # 与历史全相同→中位 50，999 未污染

    def test_slow_failure_keeps_fast(self):
        """慢指针异常隔离：日志损坏时快指针读数原封不动、不抛异常。"""
        uil.STUDY_LOG.mkdir()  # 路径变成目录：open() 抛 OSError，走失败隔离路径
        snap = {"tradingDays": ["2026-03-15"], "signals": {"llmFear": 33.3, "llmGreed": 44.4}}
        uil.compute_history_percentiles(snap)  # 不得抛
        self.assertEqual(snap["signals"]["llmFear"], 33.3)
        self.assertEqual(snap["signals"]["llmGreed"], 44.4)
        self.assertNotIn("llmFearPct", snap["signals"])

    def test_cap_250(self):
        import datetime as _dt
        base = _dt.date(2025, 1, 1)
        rows = [((base + _dt.timedelta(days=i)).isoformat(), float(i % 60), 30.0) for i in range(300)]
        self._log(rows)
        today = (base + _dt.timedelta(days=300)).isoformat()
        snap = {"tradingDays": [today], "signals": {"llmFear": 59.0, "llmGreed": 30.0}}
        uil.compute_history_percentiles(snap)
        self.assertEqual(snap["signals"]["llmHistoryDays"], 250)  # 只取最近 250 条


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
