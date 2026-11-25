# -*- coding: utf-8 -*-
"""update_index 的 LLM 覆盖层入口（叠加层新文件，原作者代码零改动）

用法：python update_index_llm.py   （替代 update_index.py，流程完全一样）
GUI（gui.py）改调本文件 run_update()，与 CLI 同一条评分链路。

规则：羊驼接管，词典兜底——
1. sentiment_score：羊驼打过分的帖，方向分直接用羊驼的（量纲换算 -3..+3 → 词典 -3..+6）；
   羊驼判 ai_marketing → 方向分归 0（营销文不进情绪）；
   羊驼没打过的帖 → 词典分兜底（新帖首日词典先行，次日羊驼接管）。
2. contains_nonnegated：羊驼打过分的帖，fomo/panic 以羊驼判定为准；
   没打过的帖 → 词典词表兜底。

实验依据：Kimi vs 12B 方向一致率 76.5%，ai_marketing 召回 7/8。
前置：先跑 llm_overlay.py 生成 data/llm_scores_ollama.json。
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import update_index as ui

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "forum_cache.json"
STORE = ROOT / "data" / "llm_scores_ollama.json"

# ---- 羊驼评分索引（2026-11-25 修复 P1-4：弃用标题单键，改多维键，禁止静默合并）----
# LLM_BY_ID:      帖子 id → 评分条目
# LLM_BY_TS:      (标题, source) → 评分条目（仅当该组合唯一对应一个 id）
# LLM_BY_TITLE:   标题 → 评分条目（仅当该标题全局唯一对应一个 id）
# _AMBIGUOUS:     标题或(标题,source) 对应多个不同 id 的集合 → 一律词典兜底
# _LAST_BY_TEXT:  sentiment_score 解析结果暂存，供同帖紧随其后的 contains_nonnegated 复用
LLM_BY_ID = {}
LLM_BY_TS = {}
LLM_BY_TITLE = {}
_AMBIGUOUS = set()
_LAST_BY_TEXT = {}
_logged_ambiguous = set()

_sentinel = object()

_orig_sentiment = ui.sentiment_score
_orig_contains = ui.contains_nonnegated
_orig_load_cache = ui.load_forum_cache
_orig_fetch_source = ui.fetch_source


def load_llm_scores():
    """按帖子 id 从 STORE 取评分，再用 CACHE 原文建立 标题/source 多维索引。

    同一标题（或同一标题+source）若对应多个不同 id，标记为歧义，
    该文本一律走词典兜底——宁可漏接羊驼分，不可错配到别的帖子。
    """
    LLM_BY_ID.clear(); LLM_BY_TS.clear(); LLM_BY_TITLE.clear()
    _AMBIGUOUS.clear(); _LAST_BY_TEXT.clear(); _logged_ambiguous.clear()
    if not CACHE.exists() or not STORE.exists():
        return
    try:
        scores = {str(x["id"]): x for x in json.loads(STORE.read_text(encoding="utf-8"))}
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError) as e:
        # 评分文件损坏/不可读：明确告警并降级为纯词典，绝不把异常甩给上层崩掉 GUI
        print(f"LLM 覆盖层：评分或缓存文件读取失败，本轮纯词典兜底：{e}", flush=True)
        return
    title_ids = {}
    ts_ids = {}
    for src in cache.values():
        if not isinstance(src, dict):
            continue
        for p in src.get("posts", []):
            if not isinstance(p, dict) or not p.get("title"):
                continue
            pid = str(p.get("id") or "")
            e = scores.get(pid)
            if not e:
                continue
            LLM_BY_ID[pid] = e
            title = str(p["title"])
            ts = (title, str(p.get("source") or ""))
            title_ids.setdefault(title, set()).add(pid)
            ts_ids.setdefault(ts, set()).add(pid)
    for title, ids in title_ids.items():
        if len(ids) == 1:
            LLM_BY_TITLE[title] = LLM_BY_ID[next(iter(ids))]
        else:
            _AMBIGUOUS.add(title)
    for (title, source), ids in ts_ids.items():
        # 注意：即使标题全局歧义，(标题, source) 唯一时也要收——
        # TS 索引正是消解标题歧义的手段，不能因为标题歧义就整体放弃
        if len(ids) == 1:
            LLM_BY_TS[(title, source)] = LLM_BY_ID[next(iter(ids))]


def resolve_llm_entry(text, source=None):
    """按 (标题, source) → 唯一标题 的顺序解析羊驼评分；歧义或缺失返回 None。"""
    text = str(text or "")
    e = LLM_BY_TS.get((text, str(source or "")))
    if e:
        return e
    if text in _AMBIGUOUS:
        if text not in _logged_ambiguous:
            _logged_ambiguous.add(text)
            print(f"LLM 覆盖层：标题歧义（{len(text)}字），走词典兜底：{text[:30]}...", flush=True)
        return None
    return LLM_BY_TITLE.get(text)


def dedup_cache():
    """按帖子 id 去重（同一热帖连续多日采集会重复进缓存），保留最新一条。"""
    cache = _orig_load_cache()
    n_before = n_after = 0
    for key, src in cache.items():
        if not isinstance(src, dict):
            continue
        posts = src.get("posts") or []
        n_before += len(posts)
        seen, deduped = set(), []
        for p in posts:  # 保持原顺序，保首个丢重复
            pid = str(p.get("id") or "") if isinstance(p, dict) else ""
            if not pid or pid in seen:
                continue
            seen.add(pid)
            deduped.append(p)
        src["posts"] = deduped
        n_after += len(src["posts"])
    if n_before != n_after:
        print(f"LLM 覆盖层：缓存去重 {n_before} → {n_after} 条", flush=True)
    return cache


def _scale_direction(d):
    # -3..+3 → -3..+6，对齐词典量纲。
    # 口径说明（P1-5 审计要求）：此不对称映射是**有意设计**。词典本身量纲不对称
    # （看多远大于看空：正向短语累计可到 +6，负向只到 -3），若线性映射会让羊驼的
    # 强烈看空(-3)压过词典量纲上限、强烈看多(+3)够不到词典正向常态，两端点对不齐。
    # d*2 只放大正向端，负向端恒等，正好把羊驼两端点钉到词典两端点上，
    # 保证替换评分器后指数刻度不变。禁止"修正"为对称映射。
    return d * 2 if d > 0 else d


def llm_sentiment_score(text, topic=None, source=None):
    e = resolve_llm_entry(text, source)
    _LAST_BY_TEXT[str(text or "")] = e  # 同帖的 contains_nonnegated 紧随其后，按文本复用
    if not e:
        return _orig_sentiment(text, topic=topic, source=source)  # 词典兜底
    if e.get("ai_marketing"):
        return 0
    return _scale_direction(int(e["direction"]))


def llm_contains(text, words):
    text = str(text or "")
    e = _LAST_BY_TEXT.get(text, _sentinel)
    if e is _sentinel:
        e = resolve_llm_entry(text)  # 兜底路径：只在标题全局唯一时接管
    if not e:
        return _orig_contains(text, words)  # 词典兜底
    if words is ui.FOMO_WORDS:
        return bool(e.get("fomo"))
    if words is ui.PANIC_WORDS:
        return bool(e.get("panic"))
    return _orig_contains(text, words)


def _refresh_now():
    """P1-2 修复：NOW 是 update_index 的模块级常量（导入时绑定）。

    GUI 长期运行时点击更新，若直接用模块常量等于用启动时间打分。这里在
    每轮更新前把 ui.NOW 刷成当前时间；同时包一层 fetch_source，因为
    `fetch_source(spec, as_of=NOW)` 的默认值在 def 时就绑死了旧 NOW，
    模块属性刷新对它无效，必须显式传 as_of。
    """
    ui.NOW = datetime.now(ui.CN_TZ)


def _fresh_fetch_source(spec, as_of=None):
    return _orig_fetch_source(spec, as_of if as_of is not None else datetime.now(ui.CN_TZ))


def apply_overlays():
    """打全部 monkeypatch（CLI/GUI 共用）。幂等，可重复调用。"""
    load_llm_scores()
    n = len(LLM_BY_ID)
    print(f"LLM 覆盖层：载入羊驼评分 {n} 条（唯一标题 {len(LLM_BY_TITLE)}，歧义标题 {len(_AMBIGUOUS)}）", flush=True)
    if n == 0:
        print("警告：无羊驼分数，本轮纯词典（先跑 llm_overlay.py）", flush=True)
    ui.sentiment_score = llm_sentiment_score
    ui.contains_nonnegated = llm_contains
    ui.load_forum_cache = dedup_cache
    ui.fetch_source = _fresh_fetch_source
    _refresh_now()


PLATFORM_ORDER = ("eastmoney", "sina", "ths", "taoguba")
EMOTION_WINDOW_DAYS = 15  # 情绪滑动窗口：只统计最近15个自然日的已打分帖（2026-11-25 用户定）
EMOTION_FALLBACK_DAYS = (15, 30, 60, None)  # 窗口样本不足时逐级放宽兜底


def _snapshot_as_of(snapshot):
    """情绪窗口的日期基准：快照 tradingDays[0]（市场日）。缺失时退回当天。

    审计修订 A2：原实现用 datetime.now() 切窗口，历史重跑时窗口随系统日期漂移、
    结果不可复现。现在一律以快照自身日期为基准。"""
    td = (snapshot or {}).get("tradingDays") or []
    if td:
        try:
            from datetime import date
            return date.fromisoformat(str(td[0])[:10])
        except ValueError:
            pass
    return datetime.now(ui.CN_TZ).date()


def _recent_posts(cache, as_of=None):
    """按发布日期过滤缓存帖子。返回 {窗口天数 or None: [post,...]}，None=全期。

    as_of：窗口截止日（含），传 None 用当天——仅供测试；生产路径必须传快照日期。"""
    from datetime import timedelta
    base = as_of or datetime.now(ui.CN_TZ).date()
    out = {}
    for days in EMOTION_FALLBACK_DAYS:
        if days is None:
            out[None] = [p for src in cache.values() if isinstance(src, dict)
                         for p in src.get("posts", []) if isinstance(p, dict)]
            continue
        cutoff = (base - timedelta(days=days)).isoformat()
        upper = base.isoformat()  # 独立验收修订：窗口必须有上限，未来时间戳的帖子不得污染历史重跑
        out[days] = [p for src in cache.values() if isinstance(src, dict)
                     for p in src.get("posts", [])
                     if isinstance(p, dict) and cutoff <= str(p.get("publishTime") or "")[:10] <= upper]
    return out


def compute_llm_emotion(snapshot):
    """聚合羊驼情绪强度 → snapshot.signals.llmFear / llmGreed（0-100）。

    口径（2026-11-25 定稿）：
    - 滑动窗口：只统计最近 15 个自然日发布的已打分帖（防历史 corpus 稀释读数）；
      窗口总样本 <30 时按 15/30/60/全期逐级放宽兜底
    - 单帖 fear/greed 各 0-3，只收带这两字段的条目；排除 ai_marketing
    - 平台内均值 → 四平台固定各 25%，无分平台按中性 1.5 占位（缺平台留中性不转权）
    - 指标 = 四平台均值 ÷ 3 × 100；全无有效帖 → 不写键，显示端回落旧合成
    """
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    post2plat = {}
    for src in cache.values():
        if not isinstance(src, dict):
            continue
        plat = str(src.get("platform") or "")
        for p0 in src.get("posts", []):
            if isinstance(p0, dict):
                post2plat[str(p0.get("id") or "")] = plat
    by_platform = {k: [] for k in PLATFORM_ORDER}
    diag = []
    as_of = _snapshot_as_of(snapshot)
    chosen_window = "all"
    for days, posts in _recent_posts(cache, as_of).items():
        bp = {k: [] for k in PLATFORM_ORDER}
        scored = 0
        for p0 in posts:
            e = LLM_BY_ID.get(str(p0.get("id") or ""))
            if not e:
                continue
            scored += 1
            if e.get("ai_marketing") or "fear" not in e or "greed" not in e:
                continue
            plat = post2plat.get(str(p0.get("id") or ""))
            if plat in bp:
                bp[plat].append((int(e["fear"]), int(e["greed"])))
        valid = sum(len(v) for v in bp.values())
        diag.append(f"{days}天:总{len(posts)}/已评分{scored}/有效{valid}")
        by_platform = bp
        chosen_window = "all" if days is None else str(days)
        if valid >= 30:
            break
    print("LLM 覆盖层[情绪诊断]: " + " | ".join(diag), flush=True)
    fear_acc = greed_acc = used = 0
    for plat in PLATFORM_ORDER:
        vals = by_platform[plat]
        if vals:
            n = len(vals)
            fear_acc += sum(v[0] for v in vals) / n
            greed_acc += sum(v[1] for v in vals) / n
            used += 1
        else:
            fear_acc += 1.5  # 中性占位
            greed_acc += 1.5
    if used == 0:
        return  # 无有效帖，不写键，显示端回落
    signals = snapshot.setdefault("signals", {})
    signals["llmFear"] = round(fear_acc / 4 / 3 * 100, 1)
    signals["llmGreed"] = round(greed_acc / 4 / 3 * 100, 1)
    # 覆盖质量字段（审计 A6）：权重不变，但覆盖必须可见，防"单平台30帖"伪装成"四平台覆盖"
    platform_n = {plat: len(by_platform[plat]) for plat in PLATFORM_ORDER}
    signals["llmValidPostCount"] = sum(platform_n.values())
    signals["llmPlatformCount"] = sum(1 for n in platform_n.values() if n > 0)
    signals["llmPlatformSamples"] = platform_n
    signals["llmWindowDays"] = chosen_window


def collect_llm_sample(snapshot, n_dir=8, n_emo=8, n_mkt=4, n_calm=4):
    """抽羊驼评分样本进 data.js，供手机端人工抽检模型质量（2026-11-25）。

    确定性选取（同输入必得同输出）：强方向8 + 强情绪8 + 营销文4 + 平静帖4，
    类别间不重复。只取近30自然日内发布、带 fear/greed 的条目——全 corpus 抽样
    会被历史极端帖占据，失去"质检当天打分"的意义。
    注意：样本含公开帖子标题，随 data.js 公开。
    """
    from datetime import timedelta
    from llm_overlay import PLATFORM_NAMES
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    cutoff = (_snapshot_as_of(snapshot) - timedelta(days=30)).isoformat()
    cands = []
    for src in cache.values():
        if not isinstance(src, dict):
            continue
        plat = str(src.get("platform") or "")
        for p0 in src.get("posts", []):
            if not isinstance(p0, dict) or not p0.get("title"):
                continue
            if str(p0.get("publishTime") or "")[:10] < cutoff:
                continue  # 30天窗口外：老帖不进抽检
            e = LLM_BY_ID.get(str(p0.get("id") or ""))
            if e and "fear" in e and "greed" in e:
                cands.append((e, str(p0["title"])[:46], PLATFORM_NAMES.get(plat, plat)))
    used, picked = set(), []

    def take(seq, k):
        got = 0
        for e, t, pl in seq:
            if got >= k:
                break
            if id(e) in used:
                continue
            used.add(id(e))
            got += 1
            picked.append({"title": t, "platform": pl, "direction": int(e["direction"]),
                           "fear": int(e["fear"]), "greed": int(e["greed"]),
                           "persona": str(e.get("persona", "none")),
                           "marketing": int(bool(e.get("ai_marketing"))),
                           "reason": str(e.get("reason", ""))})

    take(sorted(cands, key=lambda x: -abs(int(x[0]["direction"]))), n_dir)
    take(sorted(cands, key=lambda x: -(int(x[0]["fear"]) + int(x[0]["greed"]))), n_emo)
    take([c for c in cands if c[0].get("ai_marketing")], n_mkt)
    take(sorted((c for c in cands if not c[0].get("ai_marketing")),
                key=lambda x: int(x[0]["fear"]) + int(x[0]["greed"])), n_calm)
    if picked:
        snapshot["llmSample"] = picked


def compute_llm_sector_emotion(snapshot, weight=0.2, min_n=3):
    """羊驼情绪注入板块热度榜（2026-11-25 用户授权）。

    - 帖子→板块：sector_catalog 主归类（一家公司只属一个板块），按 sourceCode 映射
    - 板块情绪温度 = 50 +（贪婪−恐惧）÷2，0-100，低=恐慌高=贪婪；15天窗口起算，
      板块样本 <3 时按 15/30/60/全期 逐级向前追溯至够3条；全期都不够才记 null（不参与）
    - 排名分 heat = (1-w)×原活动度 + w×板块情绪，然后对有名次板块重新排序、重编 heatRank
    - 原值存 activityHeat 供追溯；情绪存 emotion/emotionN，前端以"情"字徽章展示
    """
    from sector_catalog import SECTORS
    code2sector = {}
    for _key, sec in SECTORS.items():
        for code, _name in sec["sourceStocks"]:
            code2sector[str(code)] = str(sec["name"])
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    code_of = {}
    for src in cache.values():
        if not isinstance(src, dict):
            continue
        for p0 in src.get("posts", []):
            if isinstance(p0, dict):
                code_of[str(p0.get("id") or "")] = str(p0.get("sourceCode") or "")
    by_sector = {}
    per_window = {}
    as_of = _snapshot_as_of(snapshot)
    print(f"LLM 覆盖层[板块诊断]: 评分池{len(LLM_BY_ID)}条，code映射{len(code_of)}条", flush=True)
    for days, posts in _recent_posts(cache, as_of).items():
        acc = {}
        for p0 in posts:
            e = LLM_BY_ID.get(str(p0.get("id") or ""))
            if not e or e.get("ai_marketing") or "fear" not in e or "greed" not in e:
                continue
            sec_name = code2sector.get(code_of.get(str(p0.get("id") or ""), ""))
            if sec_name:
                acc.setdefault(sec_name, []).append((int(e["fear"]), int(e["greed"])))
        per_window[days] = acc
    # 每板块独立逐级追溯：15天不够3条→30→60→全期，够3条即用（保证冷门板块有读数）
    for days in EMOTION_FALLBACK_DAYS:
        for sec_name, vals in per_window[days].items():
            if sec_name not in by_sector and len(vals) >= min_n:
                by_sector[sec_name] = vals
    stats = {}
    for name, vals in by_sector.items():
        if len(vals) >= min_n:
            f = sum(v[0] for v in vals) / len(vals) / 3 * 100
            g = sum(v[1] for v in vals) / len(vals) / 3 * 100
            stats[name] = (round(50 + (g - f) / 2, 1), len(vals))
    sectors = snapshot.get("sectorIndices") or []
    for sct in sectors:
        if not isinstance(sct, dict):
            continue
        emo, n = stats.get(str(sct.get("name")), (None, 0))
        orig = float(sct.get("heat") or 50)
        sct["activityHeat"] = round(orig, 1)
        sct["emotion"] = emo
        sct["emotionN"] = n
        if emo is not None:
            sct["heat"] = round((1 - weight) * orig + weight * emo, 1)
    elig = [x for x in sectors if isinstance(x, dict) and x.get("heatRank") is not None]
    rest = [x for x in sectors if not (isinstance(x, dict) and x.get("heatRank") is not None)]
    elig.sort(key=lambda x: -float(x.get("heat") or 0))
    for i, x in enumerate(elig, 1):
        x["heatRank"] = i
    if elig or rest:
        snapshot["sectorIndices"] = elig + rest


STUDY_LOG = ROOT / "data" / "study_emotion.csv"


def append_study_log(snapshot):
    """研究瘦日志：写入 data/study_emotion.csv（2026-11-25 立，深夜修订）。

    修订（回应代码审计）：① 必须在 run_update 四份主文件全部保存成功后才调用——
    主流程失败时研究日志不记录未发布快照；② 按交易日 upsert：同日多次更新只保留
    最后一行，研究序列严格一日一点，防重复加权。
    与产品 history.json 解耦：history 存全量快照会随时间膨胀，研究只需要
    日期+情绪读数，一年 20KB。攒够 ~180 个交易日后跑事件研究（见 RESEARCH_PLAN.md）。
    """
    sig = snapshot.get("signals") or {}
    if "llmFear" not in sig or "llmGreed" not in sig:
        return
    day = snapshot.get("tradingDays", [""])[0] if snapshot.get("tradingDays") else datetime.now(ui.CN_TZ).date().isoformat()
    sectors = {str(x.get("name")): x.get("emotion") for x in (snapshot.get("sectorIndices") or [])
               if isinstance(x, dict) and x.get("emotion") is not None}
    import csv
    import os
    import tempfile
    rows = {}
    if STUDY_LOG.exists():
        try:
            with STUDY_LOG.open("r", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if any(r.get(k) is None for k in ("date", "fear", "greed")):
                        raise csv.Error(f"行缺关键列：{r}")
                    if r.get("date"):
                        rows[r["date"]] = r  # 按交易日 upsert，同日留最新
        except (OSError, csv.Error) as e:
            # 二轮验收修订：读取失败禁止清空重写——保留原文件全部历史，只留损坏备份
            import shutil as _sh
            backup = STUDY_LOG.with_suffix(".corrupt.csv")
            try:
                _sh.copy2(STUDY_LOG, backup)
            except OSError:
                backup = None
            raise RuntimeError(
                f"研究日志读取失败，已保留原文件{'（备份 ' + str(backup) + '）' if backup else ''}，"
                f"请人工检查后再写入：{e}")
    if not rows and STUDY_LOG.exists() and STUDY_LOG.stat().st_size > 0:
        raise RuntimeError("研究日志解析结果为空但文件非空，拒绝覆盖，请人工检查")
    rows[day] = {"date": day, "fear": sig["llmFear"], "greed": sig["llmGreed"],
                 "sectors_json": json.dumps(sectors, ensure_ascii=False, separators=(",", ":")),
                 "valid": sig.get("llmValidPostCount", ""), "platforms": sig.get("llmPlatformCount", ""),
                 "window": sig.get("llmWindowDays", "")}
    # 标准 csv.writer 保证 JSON 单元格含逗号/引号时被正确引用；临时文件+os.replace 原子替换
    fd, tmpname = tempfile.mkstemp(dir=str(STUDY_LOG.parent), prefix=".study_", suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "fear", "greed", "sectors_json", "valid", "platforms", "window"])
            w.writeheader()
            for d in sorted(rows):
                w.writerow(rows[d])
        os.replace(tmpname, STUDY_LOG)
    except OSError as e:
        os.unlink(tmpname)
        raise


def _publish_snapshot(snapshot, paths=None):
    """四文件暂存校验后依序发布（审计修订 A5；非事务，语义如实声明）。

    崩溃安全性来自顺序而非回滚：data.js 最后替换，任何时刻页面要么看旧完整版要么新完整版；
    history/historyMeta/latest 若领先 data.js 一个版本，零用户影响且下轮自愈合（见测试
    test_publish_midway_replace_failure_keeps_old 的崩溃点断言）。任一校验失败则整体不发布。
    paths 可注入供测试；默认 product 真实路径。"""
    import shutil
    import tempfile
    if paths is None:
        paths = {
            "history": ui.HISTORY_PATH, "historyMeta": ui.HISTORY_META_PATH,
            "latest": ui.LATEST_PATH, "datajs": ui.DATA_JS_PATH,
        }
    snapshot = dict(snapshot)
    snapshot["snapshotVersion"] = ui.NOW.strftime("%Y%m%d-%H%M%S")  # 统一版本标识
    tmpdir = Path(tempfile.mkdtemp(dir=str(paths["datajs"].parent), prefix=".publish_"))  # 与目标同目录，保证 os.replace 同设备
    try:
        ui.save_json(tmpdir / "history.json", snapshot["history"])
        ui.save_json(tmpdir / "history_meta.json", snapshot["historyMeta"])
        ui.save_json(tmpdir / "latest.json", snapshot)
        ui.save_text(tmpdir / "data.js", "window.INDEX_DATA = " + json.dumps(snapshot, ensure_ascii=False, indent=2) + ";\n")
        # 完整性校验：JSON 可解析 + data.js 前缀与载荷可解析
        for name in ("history.json", "history_meta.json", "latest.json"):
            json.loads((tmpdir / name).read_text(encoding="utf-8"))
        js = (tmpdir / "data.js").read_text(encoding="utf-8")
        prefix = "window.INDEX_DATA = "
        if not js.startswith(prefix) or not js.rstrip().endswith(";"):
            raise ValueError("data.js 包装格式异常，拒绝发布")
        json.loads(js[len(prefix):].rstrip().rstrip(";"))
        # 依序替换：data.js 永远最后
        for name, target in (("history.json", paths["history"]),
                             ("history_meta.json", paths["historyMeta"]),
                             ("latest.json", paths["latest"]),
                             ("data.js", paths["datajs"])):
            os.replace(tmpdir / name, target)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class UpdateSkipped(Exception):
    """更新被跳过（通常=另一个更新进程持锁）。CLI 映射 exit 2，GUI 显示为可读提示。"""


LOCK_PATH = ROOT / "data" / ".update.lock"


def _acquire_lock(path=None):
    """进程锁（二轮验收修订）：O_EXCL 原子创建——检查+创建一步到位，杜绝双进程竞态同时拿锁。

    锁文件存 PID；持有进程活着返回 False，死 PID / 损坏锁自动接管。path=None 时取当前 LOCK_PATH（延迟绑定）。"""
    if path is None:
        path = LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            os.kill(int(path.read_text(encoding="utf-8").strip()), 0)
            return False  # 持锁进程还活着
        except (ValueError, OSError):
            try:
                path.unlink()  # 死锁/坏锁：删除后重试一次
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock(path=None):
    if path is None:
        path = LOCK_PATH
    try:
        path.unlink()
    except OSError:
        pass


def run_update(progress_callback=None):
    """CLI/GUI 统一入口：打补丁 → 构建快照 → 原子落盘（latest/history/meta/data.js）。

    P1-3 修复：GUI 原先直接调 update_index.build_snapshot()，不吃 monkeypatch，
    且 build_snapshot 不落盘（写文件在 main() 里），GUI 更新纯属白算。
    本函数让两条链路合一，并把"成功"状态推迟到全部文件写完之后再发。
    """
    if not _acquire_lock():
        raise UpdateSkipped("已有更新任务在运行（.update.lock），本轮跳过，防并发互写")
    try:
        return _run_update_inner(progress_callback)
    finally:
        _release_lock()  # 任何异常路径都必须放锁（二轮验收修订）


def _run_update_inner(progress_callback=None):
    apply_overlays()
    held_ok = {}

    def _cb(event):
        # state=ok 事件由 build_snapshot 在构建完成时发出，但此刻文件还没写；
        # 先扣下，落盘完成后补发，保证页面绿灯=数据已落地
        if isinstance(event, dict) and event.get("type") == "state" and event.get("state") == "ok":
            held_ok.update(event)
            return
        if progress_callback:
            progress_callback(event)

    snapshot = ui.build_snapshot(progress_callback=_cb)
    compute_llm_emotion(snapshot)  # 羊驼情绪接管（2026-11-25）
    collect_llm_sample(snapshot)   # 羊驼抽检样本
    compute_llm_sector_emotion(snapshot)  # 羊驼情绪注入板块榜（用户授权）
    _publish_snapshot(snapshot)  # 四文件暂存校验后整体发布，data.js 最后（审计修订 A5）
    try:
        append_study_log(snapshot)  # 研究日志只在四份主文件全部落盘成功后写（审计修订）
    except Exception as e:  # 状态分离：研究日志失败只告警，产品已发布的事实不被推翻
        print(f"警告：研究日志写入失败（产品数据已正常发布）：{e}", flush=True)
    if held_ok and progress_callback:
        progress_callback(held_ok)
    return snapshot


def main():
    """CLI 返回码契约：0=更新成功发布；2=被跳过（锁冲突）；其他非零=失败。"""
    try:
        run_update()
    except UpdateSkipped as e:
        print(str(e), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
