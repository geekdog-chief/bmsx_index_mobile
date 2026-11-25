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
    scores = {str(x["id"]): x for x in json.loads(STORE.read_text(encoding="utf-8"))}
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
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


def compute_llm_emotion(snapshot):
    """聚合羊驼情绪强度 → snapshot.signals.llmFear / llmGreed（0-100）。

    口径（2026-11-25 定稿，借鉴 CNN 恐惧贪婪指数分档）：
    - 单帖 fear/greed 各 0-3，只收有这两个字段的条目（旧存档没有 → 跳过，不补零）
    - 排除 ai_marketing；平台内取均值 → 四平台固定各 25%，无分平台按中性 1.5 占位（与主指数"缺平台留中性不转权"同纪律）
    - 指标 = 四平台均值 ÷ 3 × 100；任一侧一天没有任何有效帖 → 不写该键，显示端回落旧合成
    """
    by_platform = {k: [] for k in PLATFORM_ORDER}
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    for src in cache.values():
        if not isinstance(src, dict):
            continue
        plat = str(src.get("platform") or "")
        if plat not in by_platform:
            continue
        for p0 in src.get("posts", []):
            if not isinstance(p0, dict):
                continue
            e = LLM_BY_ID.get(str(p0.get("id") or ""))
            if not e or e.get("ai_marketing"):
                continue
            if "fear" in e and "greed" in e:
                by_platform[plat].append((int(e["fear"]), int(e["greed"])))
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
        return  # 存量旧条目都没情绪字段，不写键，显示端回落
    signals = snapshot.setdefault("signals", {})
    signals["llmFear"] = round(fear_acc / 4 / 3 * 100, 1)
    signals["llmGreed"] = round(greed_acc / 4 / 3 * 100, 1)


def collect_llm_sample(snapshot, n_dir=8, n_emo=8, n_mkt=4, n_calm=4):
    """抽羊驼评分样本进 data.js，供手机端人工抽检模型质量（2026-11-25）。

    确定性选取（同输入必得同输出）：强方向8 + 强情绪8 + 营销文4 + 平静帖4，
    类别间不重复。只取带 fear/greed 的新打分条目（旧存档字段不全不进抽检）。
    注意：样本含公开帖子标题，随 data.js 公开。
    """
    from llm_overlay import PLATFORM_NAMES
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    cands = []
    for src in cache.values():
        if not isinstance(src, dict):
            continue
        plat = str(src.get("platform") or "")
        for p0 in src.get("posts", []):
            if not isinstance(p0, dict) or not p0.get("title"):
                continue
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


def run_update(progress_callback=None):
    """CLI/GUI 统一入口：打补丁 → 构建快照 → 原子落盘（latest/history/meta/data.js）。

    P1-3 修复：GUI 原先直接调 update_index.build_snapshot()，不吃 monkeypatch，
    且 build_snapshot 不落盘（写文件在 main() 里），GUI 更新纯属白算。
    本函数让两条链路合一，并把"成功"状态推迟到全部文件写完之后再发。
    """
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
    ui.save_json(ui.HISTORY_PATH, snapshot["history"])
    ui.save_json(ui.HISTORY_META_PATH, snapshot["historyMeta"])
    ui.save_json(ui.LATEST_PATH, snapshot)
    # data.js 是页面真正读取的提交点，最后替换；中途失败页面继续显示上一份完整快照
    ui.save_text(ui.DATA_JS_PATH, "window.INDEX_DATA = " + json.dumps(snapshot, ensure_ascii=False, indent=2) + ";\n")
    if held_ok and progress_callback:
        progress_callback(held_ok)
    return snapshot


def main():
    run_update()
    return 0


if __name__ == "__main__":
    sys.exit(main())
