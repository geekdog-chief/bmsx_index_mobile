# -*- coding: utf-8 -*-
"""LLM 覆盖层（叠加层新文件，不碰原作者代码）

接在 update_index.py 之后跑：读 data/forum_cache.json 里采集到的新帖子，
用本地 Ollama 打分，结果追加存 data/llm_scores_ollama.json（断点续跑，只打新帖）。

用法：python llm_overlay.py [--model gemma4:12b]
前置：Ollama 服务在跑，模型已 pull。Ollama 不在也不报错，直接跳过（不挡日常更新链）。
"""

import json, os, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE = DATA_DIR / "forum_cache.json"
STORE = DATA_DIR / "llm_scores_ollama.json"
LOG = DATA_DIR / "llm_overlay.log"
API = "http://localhost:11434/api/chat"

MODEL = "gemma4:12b"
for i, a in enumerate(sys.argv):
    if a == "--model" and i + 1 < len(sys.argv):
        MODEL = sys.argv[i + 1]

PLATFORM_NAMES = {
    "eastmoney": "东方财富股吧", "sina": "新浪股吧",
    "ths": "同花顺板块社区", "taoguba": "淘股吧",
}

SYSTEM_PROMPT = """你是一名A股社区情绪标注员。接下来我会逐个上传 JSON 文件，每个文件里有若干条股民帖子（字段：id、day、平台、source、title）。请对每条帖子输出评分，严格按以下规则：

**方向分 direction**：整数 -3 到 +3。
+3 强烈看多，+2 明确看多，+1 轻度偏多，0 中性/无关/说了一堆等于没说，-1 轻度偏空，-2 明确看空，-3 强烈看空/恐慌。
注意识别反语和讽刺："呵呵，又要涨了"、"太厉害了，又创新高（指暴跌后）"这类表面夸奖实际看空，按真实意图给负分。
注意识别 AI 生成或营销文：内容是车轱辘话、无真实观点、明显引流性质的，direction 给 0。

**情绪强度 fear / greed（各自独立，0 到 3 的整数，极其重要）**：衡量帖子作者【本人】的情绪有多强。看情绪，不看观点强弱。

- fear 恐惧：0=平静/无情绪；1=轻微担忧或谨慎（"有点慌""再拿拿看""别上头"）；2=明显害怕（"再跌要割肉""亏麻了""减仓保平安"）；3=恐慌绝望（"清仓离场""不玩了""彻底完了""天台见"）。
- greed 贪婪：0=平静；1=轻度乐观期待（"感觉到底了""可以定投一点""长期看好"）；2=追涨冲动（"梭哈""满仓干""上车""回调就是机会""干就完了"）；3=狂热失去风险意识（"十倍起步""卖房上车""ALL IN""再不上车没了""砸锅卖铁买"）。

判定纪律：
- 判的是发帖人本人的情绪，不是帖子内容暗示的情绪。冷静转述利好/利空、客观提问 → 0/0。
- 情绪与方向是两条独立轴："怕卖飞"是 fear 强但 direction 可以是正的；"坚定看多但心态平静"greed 最多 1。
- 讽刺反语按真实意图："我不慌，真的不慌"配图一片绿 → 按慌给分。
- 单纯后悔/抱怨（"早知道不买了""又被割了"）未表达怕继续跌 → 0/0；伴随怕再跌 → fear 1~2。
- 骂庄家/骂监管/骂主力的愤怒本身既不是恐惧也不是贪婪；除非滑向清仓动作 → fear。
- 一帖可以两项都非零："梭哈了心里好怕" → greed 2、fear 1。
- 纯资讯搬运、闲聊、无情绪的中性帖 → 0/0。ai_marketing 的营销文情绪一律 0/0。
- 强度锚点看【情绪词+行动倾向】：只说"担心"无行动=1，出现减仓/清仓/割肉动作=2~3；只说"看好"=1，出现梭哈/满仓/上车动作=2~3。

**标记（0 或 1）**：
- ai_marketing：AI生成或营销文。**最高优先级，先判断这条再判断其他标记。** 特征：①研报/资讯腔的编号列表、机械罗列利好；②模板化看多口号、空洞无具体股票操作的排比句；③水军短句、纯夸品牌；④诱导加群、看分析、点链接的引流话。以上任何一条命中就给1，宁可多判不可漏判。
- fomo：追涨式表达（梭哈、满仓、上车、怕踏空、最后机会）
- panic：恐慌表达（割肉、清仓、跑路、完了、跌停恐慌）
- newcomer：新手提问（能买吗、怎么办、求带、什么是、目标价）
- sarcasm：讽刺/反语（按上面定义）

**persona**（该帖作者的表达身份，四选一）：baoma / novice / retail / none（定义同词典体系）。

**reason**：一句话说明判断依据，不超过15个字。

**输出格式（极其重要）**：
- 只输出一个 JSON 数组，不要输出任何其他文字、解释、markdown代码块标记
- 每条元素格式：{"id": "原样复制", "direction": 整数, "fear": 0, "greed": 0, "fomo": 0, "panic": 0, "newcomer": 0, "sarcasm": 0, "ai_marketing": 0, "persona": "retail", "reason": "不超过15字"}
- 每个 id 恰好出现一次，顺序不限，一个都不能少
- 拿不准的一律 direction 给 0，宁可保守不可编造"""

USER_PREFIX = "请评分这个文件里的帖子，只输出JSON数组。\n\n"


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def collect_posts():
    """从 forum_cache.json 拍平出 待评分帖子列表。"""
    if not CACHE.exists():
        return []
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    posts = []
    for key, src in cache.items():
        if not isinstance(src, dict):
            continue
        platform = PLATFORM_NAMES.get(str(src.get("platform") or ""), str(src.get("platform") or ""))
        day = str(src.get("fetchedAt") or "")[:10]
        for p in src.get("posts", []):
            if not isinstance(p, dict) or not p.get("title"):
                continue
            pid = str(p.get("id") or "")
            if not pid:
                continue
            pub = str(p.get("publishTime") or "")[:10]
            title = str(p["title"])
            item = {
                "id": pid,
                "day": pub or day,
                "platform": platform,
                "source": str(p.get("source") or p.get("sourceCode") or key),
                "title": title,
            }
            if len(title) > 2000:  # P2-1: 常态传全文；仅超长帖截断并打标记
                item["title"] = title[:2000]
                item["truncated"] = 1
            posts.append(item)
    return posts


def ollama_ok():
    try:
        with urllib.request.urlopen("http://localhost:11434", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def chat(user, retries=3):
    body = {"model": MODEL, "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user}],
        "stream": False, "format": "json", "think": False,
        # num_ctx 显式设 32K：Ollama 服务端默认仅 4096，超出部分整帖静默丢弃，
        # 表现为"id 缺失"级联重试（2026-11-25 修复）。num_predict=16384 为冻结参数，勿动。
        "options": {"temperature": 0, "num_predict": 16384, "num_ctx": 32768}}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(r.read().decode("utf-8"))["message"]["content"]
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Ollama 请求失败: {last}")


def parse_scores(text):
    t = text.strip()
    if t.startswith("```"):
        t = "\n".join(l for l in t.split("\n") if not l.strip().startswith("```")).strip()
    start = t.find("[")
    if start >= 0:
        depth, instr, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if instr:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': instr = False
                continue
            if c == '"': instr = True
            elif c == "[": depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    data = json.loads(t[start:i + 1])
                    if isinstance(data, list):
                        return data
                continue
    dec = json.JSONDecoder()
    out, pos = [], 0
    while pos < len(t):
        while pos < len(t) and t[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(t):
            break
        obj, end = dec.raw_decode(t, pos)
        if isinstance(obj, dict):
            if "id" in obj and "direction" in obj:
                out.append(obj)
            else:
                for k, v in obj.items():
                    if isinstance(v, dict):
                        v = dict(v); v.setdefault("id", k); out.append(v)
        elif isinstance(obj, list):
            out.extend(x for x in obj if isinstance(x, dict))
        pos = end
    if out:
        return out
    raise ValueError("不是帖子评分的JSON")


PERSONA_CHOICES = ("baoma", "novice", "retail", "none")


def strict_int(v, lo, hi, name, pid):
    """严格整数校验：只收 int 或整数字符串，拒绝 bool、float（1.8 不许偷偷变 1）。

    fear/greed 等情绪强度字段专用——数据不能错，宁重试不截断。"""
    if isinstance(v, bool):
        raise ValueError(f"{name} 是布尔值: {pid}")
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and re_full_int(v.strip()):
        n = int(v.strip())
    else:
        raise ValueError(f"{name} 类型非法 {type(v).__name__}: {pid}")
    if not lo <= n <= hi:
        raise ValueError(f"{name} 越界 {n}: {pid}")
    return n


def re_full_int(t):
    import re as _re
    return _re.fullmatch(r"-?\d+", t) is not None


def norm(it):
    """归一化单条评分并做硬校验：direction 必须 -3..3、persona 必须四选一。

    P2-2 修复：原实现只强转类型不校验范围，模型吐 9 分或乱写 persona 也会入库。
    现在任何非法字段直接 raise ValueError，由 score_batch 重试/拆包，最终进失败
    列表——宁可漏标不可错标，非法结果绝不进 STORE。"""
    d = it["direction"]
    if isinstance(d, bool):
        raise ValueError(f"direction 是布尔值: {it.get('id')}")
    d = int(d)
    if not -3 <= d <= 3:
        raise ValueError(f"direction 越界 {d}: {it.get('id')}")
    persona = str(it.get("persona", "none")).strip().lower()
    if persona not in PERSONA_CHOICES:
        raise ValueError(f"persona 非法 {persona!r}: {it.get('id')}")
    pid = str(it["id"])
    # 旧存档条目没有 fear/greed 字段 → 缺省 0（聚合层会跳过这些条目，见 update_index_llm）
    fear = strict_int(it.get("fear", 0), 0, 3, "fear", pid)
    greed = strict_int(it.get("greed", 0), 0, 3, "greed", pid)
    return {"id": pid, "direction": d, "fear": fear, "greed": greed,
            "fomo": int(bool(it.get("fomo", 0))), "panic": int(bool(it.get("panic", 0))),
            "newcomer": int(bool(it.get("newcomer", 0))), "sarcasm": int(bool(it.get("sarcasm", 0))),
            "ai_marketing": int(bool(it.get("ai_marketing", 0))),
            "persona": persona, "reason": str(it.get("reason", ""))[:30]}


def validate_batch(arr, batch):
    """整包校验：每条 norm、id 不得重复、不得出现输入之外的 id、缺项即失败。

    返回 {id: 条目}；任何违规 raise ValueError 交给上层重试。"""
    valid_ids = {p["id"] for p in batch}
    out = {}
    for x in arr:
        e = norm(x)  # 非法字段在此抛错
        if e["id"] not in valid_ids:
            raise ValueError(f"出现输入之外的 id: {e['id']}")
        if e["id"] in out:
            raise ValueError(f"id 重复: {e['id']}")
        out[e["id"]] = e
    missing = valid_ids - set(out)
    if missing:
        raise ValueError(f"id 缺失 {len(missing)} 条")
    return out


LOCK_FILE = Path(__file__).resolve().parent / "data" / ".score.lock"


def _acquire_lock() -> bool:
    """评分进程锁（审计 A9）：两个 llm_overlay 并发互写 STORE 会丢批次。"""
    if LOCK_FILE.exists():
        try:
            os.kill(int(LOCK_FILE.read_text(encoding="utf-8").strip()), 0)
            return False
        except (ValueError, OSError):
            pass
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def main():
    if not _acquire_lock():
        print("SCORE_BUSY 已有评分任务在运行，本轮退出（防并发互写 STORE）", flush=True)
        return 2
    try:
        return _main_inner()
    finally:
        _release_lock()


def _main_inner():
    if not ollama_ok():
        log("Ollama 未运行，跳过 LLM 覆盖层（不影响指数更新）")
        return 0

    posts = collect_posts()
    seen = set()
    uniq = []
    for p in sorted(posts, key=lambda x: x["day"], reverse=True):
        if p["id"] not in seen:
            seen.add(p["id"])
            uniq.append(p)
    done = {}
    if STORE.exists():
        done = {x["id"]: x for x in json.loads(STORE.read_text(encoding="utf-8"))}
    fresh = [p for p in uniq if p["id"] not in done]
    MAX = 300  # 每轮最多打 300 条（约10分钟），其余下轮继续
    todo = fresh[:MAX]
    log(f"缓存去重后 {len(uniq)} 条，已评分 {len(done)} 条，本轮待打 {len(todo)} 条（模型 {MODEL}，未排上 {len(fresh)-len(todo)} 条下轮继续）")
    if not todo:
        print("BACKFILL_DONE")  # 补跑脚本（补跑全量.cmd）的完成哨兵，勿删
        return 0

    results = dict(done)
    failed = []

    def score_batch(batch):
        user = USER_PREFIX + json.dumps({"posts": batch}, ensure_ascii=False)
        for attempt in range(3):
            try:
                arr = parse_scores(chat(user))
                got = validate_batch(arr, batch)
                results.update(got)
                return []
            except Exception as e:
                log(f"  解析失败({attempt + 1}/3): {str(e)[:100]}")
                time.sleep(5)
        if len(batch) == 1:
            return [batch[0]["id"]]
        half = len(batch) // 2
        log(f"  拆包 {len(batch)} → {half}+{len(batch) - half}")
        return score_batch(batch[:half]) + score_batch(batch[half:])

    B = 7  # 2026-11-25 用户授权解冻：10→7。12B 一次管10条经常漏id级联拆包，
           # 烧的调用比直接喂7条还多；7条/包漏id率大降，总耗时反而回落。MAX=300 不动。
    for s in range(0, len(todo), B):
        failed.extend(score_batch(todo[s:s + B]))
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(list(results.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  进度 {min(s + B, len(todo))}/{len(todo)}")
        time.sleep(1)

    STORE.write_text(json.dumps(list(results.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"覆盖层完成：新增 {len(results) - len(done)} 条，失败 {len(failed)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
